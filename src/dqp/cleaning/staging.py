"""Layer 4: traceable cleaning, raw -> staging.

For every raw table the pipeline builds a staging table:

  raw_<t>       all text, exactly as loaded                (never changed)
  stg_<t>       typed, cleaned by the steps in the config  (+ _row lineage column)
  rejected_<t>  every row removed by a step, as it was in the raw table,
                with the step number, action and reason

Rules this module follows:
  - Every change is a logged step with a reason, row counts before and after,
    the number of values changed, and examples. The log is written to
    cleaning.json and _dqp_cleaning_log.
  - Rows are never deleted silently: removed rows move to rejected_<t>.
  - Values that could not be read as their column's type become NULL in
    staging. That is not hidden either: the automatic last step 'type_cast'
    counts them per column, with the original value and row number.
  - _row is the row number in the source table, so any staging row can be
    traced back to its line in the original file.
  - Gaps are not filled unless a fill_forward step says so, with a reason,
    and then a <column>_filled flag marks every filled value.
  - A cleaning step that cannot run (unknown column, bad SQL) stops the run:
    half-cleaned data must not move on to the mart and the summary.
"""

from __future__ import annotations

import re

import duckdb

from dqp.config import CleaningStep
from dqp.profiling.profiler import q

ROW = "_row"


class CleaningError(ValueError):
    pass


def short_name(raw_table: str) -> str:
    return raw_table.removeprefix("raw_")


def snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if not s:
        s = "column"
    if s[0].isdigit():
        s = "c_" + s
    return s


def _columns(con, table: str) -> dict[str, str]:
    """Column name -> type, without the lineage column."""
    return {r[0]: r[1] for r in con.execute(f"DESCRIBE {q(table)}").fetchall() if r[0] != ROW}


def _count(con, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchall()[0][0]


def _short(value, width: int = 60):
    if isinstance(value, str) and len(value) > width:
        return value[: width - 3] + "..."
    return value


def build_staging(con: duckdb.DuckDBPyConnection, raw_table: str, typed_view: str,
                  steps: list[CleaningStep], max_examples: int = 3) -> dict:
    t = short_name(raw_table)
    stg, rej = f"stg_{t}", f"rejected_{t}"
    log: list[dict] = []

    con.execute(f"CREATE OR REPLACE TABLE {q(stg)} AS SELECT * FROM {q(typed_view)} ORDER BY {ROW}")
    con.execute(f"""CREATE OR REPLACE TABLE {q(rej)} AS
        SELECT NULL::INTEGER AS _step, NULL::VARCHAR AS _action, NULL::VARCHAR AS _reason,
               rowid + 1 AS {ROW}, * FROM {q(raw_table)} LIMIT 0""")

    raw_cols = list(_columns(con, raw_table))
    lineage: dict[str, str | None] = {c: c for c in raw_cols}   # staging column -> raw column
    set_to_null: dict[str, set[str]] = {}                       # values a replace step nulled on purpose

    types = _columns(con, stg)
    typed = {k: sum(v == k for v in types.values()) for k in ("DOUBLE", "DATE", "BOOLEAN", "VARCHAR")}
    log.append(_entry(stg, 0, "load_typed", "cast each column to the type profiling inferred",
                      rows_before=_count(con, raw_table), rows_after=_count(con, stg),
                      details=f"{typed['DOUBLE']} numeric, {typed['DATE']} date, {typed['BOOLEAN']} boolean, "
                              f"{typed['VARCHAR']} text column(s)"))

    for i, step in enumerate(steps, start=1):
        before = _count(con, stg)
        try:
            entry = _apply(con, stg, rej, raw_table, step, i, lineage, set_to_null, max_examples)
        except duckdb.Error as exc:
            raise CleaningError(f"{stg}, cleaning step {i} ({step.kind}): {str(exc).splitlines()[0]}") from None
        entry.update(table=stg, step=i, action=step.kind, rows_before=before, rows_after=_count(con, stg))
        entry["rows_removed"] = before - entry["rows_after"]
        log.append(entry)

    losses = _cast_losses(con, stg, raw_table, lineage, set_to_null, max_examples)
    if losses:
        total = sum(v["count"] for v in losses.values())
        n = _count(con, stg)
        log.append(_entry(stg, len(steps) + 1, "type_cast",
                          f"values that could not be read as their column's type are NULL in {stg}; "
                          f"the original text is kept in {raw_table}",
                          rows_before=n, rows_after=n, values_changed=total, columns=list(losses),
                          details="; ".join(f"{c}: {v['count']} not {v['type']}" for c, v in losses.items()),
                          examples=[e for v in losses.values() for e in v["examples"]][:max_examples]))

    return {"raw_table": raw_table, "staging_table": stg, "rejected_table": rej,
            "rows_raw": _count(con, raw_table), "rows_staging": _count(con, stg), "rows_rejected": _count(con, rej),
            "lineage": lineage, "cast_losses": losses, "log": log}


def _entry(table, step, action, reason, rows_before=None, rows_after=None, values_changed=0,
           columns=None, details="", examples=None) -> dict:
    return {"table": table, "step": step, "action": action, "reason": " ".join(str(reason).split()),
            "rows_before": rows_before, "rows_after": rows_after,
            "rows_removed": (rows_before - rows_after) if rows_before is not None else 0,
            "values_changed": values_changed, "columns": columns or [], "details": details,
            "examples": examples or []}


def _require(con, stg: str, cols: list[str]) -> None:
    have = _columns(con, stg)
    missing = [c for c in cols if c not in have]
    if missing:
        raise CleaningError(f"{stg}: unknown column(s) {missing}; columns are {list(have)}")


def _reject(con, stg, rej, raw_table, ids_sql: str, step: int, action: str, reason: str, max_examples: int) -> list:
    """Move the rows whose _row is returned by ids_sql from staging to rejected (as raw text)."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE _ids AS {ids_sql}")
    con.execute(f"""INSERT INTO {q(rej)} SELECT ?, ?, ?, r.rowid + 1, r.* FROM {q(raw_table)} r
                    WHERE r.rowid + 1 IN (SELECT {ROW} FROM _ids)""", [step, action, " ".join(reason.split())])
    con.execute(f"DELETE FROM {q(stg)} WHERE {ROW} IN (SELECT {ROW} FROM _ids)")
    con.execute("DROP TABLE _ids")
    cur = con.execute(f"SELECT * EXCLUDE (_step, _action, _reason) FROM {q(rej)} WHERE _step = ? "
                      f"ORDER BY {ROW} LIMIT {max_examples}", [step])
    names = [d[0] for d in cur.description]
    # show the row number plus the first few non-empty raw values: enough to recognise the row
    examples = []
    for r in cur.fetchall():
        rec = dict(zip(names, r))
        shown = {ROW: rec.pop(ROW)}
        for k, v in rec.items():
            if v is not None and len(shown) < 4:
                shown[k] = _short(v)
        examples.append(shown)
    return examples


def _apply(con, stg, rej, raw_table, step: CleaningStep, i: int, lineage: dict, set_to_null: dict,
           max_examples: int) -> dict:
    kind = step.kind

    if kind == "rename":
        _require(con, stg, list(step.rename))
        for old, new in step.rename.items():
            con.execute(f"ALTER TABLE {q(stg)} RENAME COLUMN {q(old)} TO {q(new)}")
            lineage[new] = lineage.pop(old)
        return _entry(stg, i, kind, "column names", columns=list(step.rename.values()),
                      details=", ".join(f"{o} -> {n}" for o, n in step.rename.items()))

    if kind == "snake_case":
        current = list(_columns(con, stg))
        mapping, used = {}, set()
        for c in current:
            new, k = snake(c), 2
            while new in used:
                new, k = f"{snake(c)}_{k}", k + 1
            used.add(new)
            if new != c:
                mapping[c] = new
        for old, new in mapping.items():
            con.execute(f"ALTER TABLE {q(stg)} RENAME COLUMN {q(old)} TO {q(new)}")
            lineage[new] = lineage.pop(old)
        shown = list(mapping.items())[:6]
        return _entry(stg, i, kind, "consistent lower_snake_case column names, easy to use in SQL",
                      columns=list(mapping.values()),
                      details=f"{len(mapping)} column(s) renamed" + (": " + ", ".join(f"{o!r} -> {n}" for o, n in shown)
                                                                     + (" ..." if len(mapping) > 6 else "") if mapping else ""))

    if kind == "trim_whitespace":
        changed, cols = 0, []
        for c, typ in _columns(con, stg).items():
            if typ != "VARCHAR":
                continue
            n = con.execute(f"UPDATE {q(stg)} SET {q(c)} = trim({q(c)}) WHERE {q(c)} <> trim({q(c)})").fetchall()[0][0]
            if n:
                changed += n
                cols.append(c)
        return _entry(stg, i, kind, "leading/trailing spaces are formatting, not data",
                      values_changed=changed, columns=cols, details=f"{changed} value(s) trimmed in {len(cols)} column(s)")

    if kind == "drop_rows":
        d = step.drop_rows
        examples = _reject(con, stg, rej, raw_table, f"SELECT {ROW} FROM {q(stg)} WHERE {d.where}",
                           i, kind, d.reason, max_examples)
        return _entry(stg, i, kind, d.reason, details=f"rows where {d.where}", examples=examples)

    if kind == "drop_duplicates":
        d = step.drop_duplicates
        cols = d.columns or list(_columns(con, stg))
        _require(con, stg, cols)
        part = ", ".join(q(c) for c in cols)
        ids = (f"SELECT {ROW} FROM (SELECT {ROW}, row_number() OVER (PARTITION BY {part} ORDER BY {ROW}) AS rn "
               f"FROM {q(stg)}) WHERE rn > 1")
        examples = _reject(con, stg, rej, raw_table, ids, i, kind, d.reason, max_examples)
        return _entry(stg, i, kind, d.reason, columns=d.columns or [],
                      details=("duplicates on " + ", ".join(cols) if d.columns else "exact duplicates")
                              + "; the first occurrence is kept", examples=examples)

    if kind == "replace":
        r = step.replace
        _require(con, stg, [r.column])
        typ = _columns(con, stg)[r.column]
        changed, examples = 0, []
        for old, new in r.values.items():
            match = f"{q(r.column)} = ?" if typ == "VARCHAR" else f"{q(r.column)} = TRY_CAST(? AS {typ})"
            rows = [x[0] for x in con.execute(f"SELECT {ROW} FROM {q(stg)} WHERE {match} ORDER BY {ROW}", [old]).fetchall()]
            if rows:
                con.execute(f"UPDATE {q(stg)} SET {q(r.column)} = ? WHERE {match}", [new, old])
                changed += len(rows)
                examples += [{ROW: x, "from": old, "to": new} for x in rows[:max_examples]]
            if new is None:
                set_to_null.setdefault(r.column, set()).add(str(old))
        return _entry(stg, i, kind, r.reason, values_changed=changed, columns=[r.column],
                      details=f"{changed} value(s) replaced in {r.column}", examples=examples[:max_examples])

    if kind == "drop_columns":
        d = step.drop_columns
        _require(con, stg, d.columns)
        for c in d.columns:
            con.execute(f"ALTER TABLE {q(stg)} DROP COLUMN {q(c)}")
            lineage.pop(c, None)
        return _entry(stg, i, kind, d.reason, columns=d.columns, details=f"dropped {', '.join(d.columns)}")

    if kind == "fill_forward":
        f = step.fill_forward
        _require(con, stg, [f.column, f.order_by])
        flag = f"{f.column}_filled"
        c, o = q(f.column), q(f.order_by)
        limit = f"AND k <= {f.limit}" if f.limit else ""
        con.execute(f"ALTER TABLE {q(stg)} ADD COLUMN {q(flag)} BOOLEAN DEFAULT FALSE")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _fill AS
            WITH g AS (SELECT {ROW}, {c} AS v, {o} AS o, COUNT({c}) OVER (ORDER BY {o}, {ROW}) AS grp FROM {q(stg)}),
                 h AS (SELECT {ROW}, v, grp, FIRST_VALUE(v) OVER (PARTITION BY grp ORDER BY o, {ROW}) AS fill,
                              ROW_NUMBER() OVER (PARTITION BY grp ORDER BY o, {ROW}) - 1 AS k FROM g)
            SELECT {ROW}, fill FROM h WHERE v IS NULL AND grp > 0 {limit}""")
        n = _count(con, "_fill")
        examples = [{ROW: r, "filled_with": v} for r, v in
                    con.execute(f"SELECT {ROW}, fill FROM _fill ORDER BY {ROW} LIMIT {max_examples}").fetchall()]
        con.execute(f"UPDATE {q(stg)} SET {c} = _fill.fill, {q(flag)} = TRUE FROM _fill WHERE {q(stg)}.{ROW} = _fill.{ROW}")
        con.execute("DROP TABLE _fill")
        lineage[flag] = None
        return _entry(stg, i, kind, f.reason, values_changed=n, columns=[f.column, flag],
                      details=f"{n} missing value(s) in {f.column} filled from the previous value"
                              + (f" (at most {f.limit} in a row)" if f.limit else "") + f"; marked in {flag}",
                      examples=examples)

    raise CleaningError(f"unknown cleaning step {kind!r}")   # unreachable: config validation prevents it


def _cast_losses(con, stg, raw_table, lineage, set_to_null, max_examples) -> dict:
    out = {}
    for c, typ in _columns(con, stg).items():
        rc = lineage.get(c)
        if rc is None or typ == "VARCHAR":
            continue
        excluded = set_to_null.get(c, set())
        excl = ("AND trim(r." + q(rc) + ") NOT IN (" + ", ".join("'" + v.replace("'", "''") + "'" for v in excluded) + ")"
                if excluded else "")
        where = (f"FROM {q(stg)} s JOIN {q(raw_table)} r ON r.rowid + 1 = s.{ROW} "
                 f"WHERE s.{q(c)} IS NULL AND r.{q(rc)} IS NOT NULL AND trim(r.{q(rc)}) <> '' {excl}")
        n = con.execute(f"SELECT COUNT(*) {where}").fetchall()[0][0]
        if n:
            ex = con.execute(f"SELECT s.{ROW}, r.{q(rc)} {where} ORDER BY s.{ROW} LIMIT {max_examples}").fetchall()
            out[c] = {"count": n, "type": {"DOUBLE": "numeric", "DATE": "a date", "BOOLEAN": "boolean"}.get(typ, typ),
                      "examples": [{ROW: r, "column": c, "raw value": _short(v)} for r, v in ex]}
    return out


def build_mart(con: duckdb.DuckDBPyConnection, name: str, sql: str) -> str:
    """Materialise a mart. Keeps _row if the query selects it (lineage to the
    source file); otherwise numbers the rows."""
    table = f"mart_{name}"
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE ({sql})").fetchall()]
        if ROW in cols:
            con.execute(f"CREATE OR REPLACE TABLE {q(table)} AS {sql}")
        else:
            con.execute(f"CREATE OR REPLACE TABLE {q(table)} AS SELECT row_number() OVER () AS {ROW}, * FROM ({sql})")
    except duckdb.Error as exc:
        raise CleaningError(f"mart {name!r} could not be built: {str(exc).splitlines()[0]}") from None
    return table
