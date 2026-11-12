"""Layer 3: validation.

Two kinds of checks, both returning PASS / WARN / FAIL (or INFO, ERROR):

  generic  - run on every table automatically, built from the profile:
             table_not_empty, no_duplicate_rows, values_fit_type,
             no_empty_columns, no_duplicate_dates, no_missing_periods
  rules    - written by the user in the dataset config (see config.Rule)

Every check reports how many rows failed, how many were checked, and example
rows with their row number in the raw table, so a FAIL can be traced to the
exact line in the source file.

A rule with a mistake in it (unknown column, bad SQL) does not stop the run:
it gets status ERROR with the database message, and counts as a failure.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import duckdb

from dqp.config import Rule, ValidationConfig
from dqp.profiling.profiler import q

GENERIC_DEFAULTS = {
    "table_not_empty": "fail",
    "no_duplicate_rows": "warn",
    "values_fit_type": "warn",
    "no_empty_columns": "warn",
    "no_duplicate_dates": "warn",
    "no_missing_periods": "warn",
}
TYPED_KINDS = ("numeric", "numeric_decimal_comma", "date", "boolean", "mixed")


@dataclass
class CheckResult:
    check_id: str
    table: str
    name: str
    origin: str            # generic | rule
    kind: str              # e.g. values_fit_type, expr, unique
    severity: str          # what a failure means: fail | warn | info
    stage: str = "staging" # raw (data as it arrived) | staging (cleaned tables and marts)
    status: str = "PASS"   # PASS | WARN | FAIL | INFO | ERROR
    failing: int = 0
    checked: int | None = None
    detail: str = ""
    examples: list = field(default_factory=list)
    description: str = ""

    def settle(self, max_failing: int = 0) -> "CheckResult":
        if self.status != "ERROR":
            self.status = self.severity.upper() if self.failing > max_failing else "PASS"
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Target:
    """Where the data of one validated table lives.
    view:    typed table/view the checks query; always has a _row column
    raw:     raw table the rows came from (None for marts), to show original values
    lineage: column in view -> column in raw (cleaning may rename or add columns)"""
    name: str
    view: str
    raw: str | None = None
    lineage: dict | None = None

    def raw_column(self, col: str) -> str | None:
        return col if self.lineage is None else self.lineage.get(col)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def _lit(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _rows(con, sql: str, limit: int) -> list[dict]:
    cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {limit}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _count(con, sql: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM ({sql})").fetchall()[0][0]


# ---------------------------------------------------------------- generic

def generic_checks(con, target: Target, prof: dict, cfg: ValidationConfig, max_examples: int,
                   stage: str) -> list[CheckResult]:
    raw_table = target.name
    sev = {**GENERIC_DEFAULTS, **cfg.generic}
    out: list[CheckResult] = []

    def add(kind: str, name: str, failing: int, detail: str, examples=None, checked=None, severity=None):
        s = severity or sev[kind]
        if sev[kind] == "off":
            return
        out.append(CheckResult(check_id="", table=raw_table, name=name, origin="generic", stage=stage, kind=kind,
                               severity=s, failing=failing, checked=checked, detail=detail,
                               examples=(examples or [])[:max_examples]).settle())

    rows = prof["rows"]
    add("table_not_empty", "table has rows", int(rows == 0), f"{rows} rows")

    dup_examples = []
    if prof["duplicate_rows"]:
        cols = ", ".join(q(c) for c in prof["columns_profile"])
        dup_examples = [{"rows": r[0]} for r in con.execute(
            f"SELECT list(_row ORDER BY _row) FROM {q(target.view)} GROUP BY {cols} "
            f"HAVING COUNT(*) > 1 LIMIT {max_examples}").fetchall()]
    add("no_duplicate_rows", "no exact duplicate rows", prof["duplicate_rows"],
        f"{prof['duplicate_rows']} extra copies in {prof['duplicate_row_groups']} group(s)", dup_examples, rows)

    for col, c in prof["columns_profile"].items():
        if c["inferred_type"] not in TYPED_KINDS:
            continue
        expected = c.get("mixed_dominant_type", c["inferred_type"]).replace("numeric_decimal_comma", "numeric")
        add("values_fit_type", f"values in {col!r} are {expected}", c["nonconforming"],
            f"{c['nonconforming']} of {c['non_null']} non-empty values are not {expected}"
            + (f" (column is mixed: {c['type_share']:.0%} fit)" if c["inferred_type"] == "mixed" else ""),
            c["nonconforming_examples"], c["non_null"])

    empty = prof["empty_columns"]
    add("no_empty_columns", "no completely empty columns", len(empty),
        f"{len(empty)} empty column(s)" + (f": {', '.join(empty[:5])}" if empty else ""),
        [{"column": c} for c in empty], prof["columns"])

    date_col = prof["main_date_column"]
    if date_col:
        d = prof["columns_profile"][date_col]["date"]
        add("no_duplicate_dates", f"no repeated dates in {date_col!r}", d["duplicate_dates"],
            f"{d['duplicate_dates']} repeated date(s)", d["duplicate_examples"], rows)
        if d.get("missing_periods") is not None:
            gap = d.get("largest_gap")
            # For business-day data, public holidays show up as gaps: report, don't warn.
            holiday_sev = "info" if d["frequency"] == "business_daily" and "no_missing_periods" not in cfg.generic else None
            add("no_missing_periods", f"no gaps in {date_col!r} ({d['frequency']})", d["missing_periods"],
                f"{d['missing_periods']} of {d['expected_periods']} {d['frequency']} periods missing "
                f"between {d['min']} and {d['max']}" + (f"; {d['note']}" if d.get("note") else ""),
                [gap] if gap else [], d["expected_periods"], severity=holiday_sev)
    return out


# ---------------------------------------------------------------- rules

def _hint_untyped(rule: Rule, prof: dict) -> str:
    """A rule comparing a column that holds no readable numbers/dates fails with a
    database type error; say which column and why, in plain words."""
    text = rule.expr or rule.sql or (rule.between.column if rule.between else "") or ""
    cols = prof["columns_profile"]
    bad = [c for c in _columns_in(text, list(cols)) if cols[c]["inferred_type"] in ("text", "empty")]
    if not bad:
        return ""
    parts = [f"{c!r} is {cols[c]['inferred_type']} ({cols[c]['non_null']} non-empty value(s), none readable as a "
             f"number or date)" for c in bad]
    return ". Hint: " + "; ".join(parts) + ", so it cannot be compared with a number or date"


def _columns_in(expr: str, columns: list[str]) -> list[str]:
    """Columns mentioned in an expression, to show their values in examples."""
    found = []
    for c in columns:
        if f'"{c}"' in expr or re.search(rf"(?<![\w\"]){re.escape(c)}(?![\w\"])", expr):
            found.append(c)
    return found


def run_rule(con, rule: Rule, target: Target, prof: dict, max_examples: int) -> CheckResult:
    raw_table = target.name
    view = q(target.view)
    columns = list(prof["columns_profile"])
    r = CheckResult(check_id="", table=raw_table, name=rule.name or "", origin="rule", stage=rule.stage, kind=rule.kind,
                    severity=rule.severity, description=rule.description)
    rows = prof["rows"]

    if rule.expr is not None:
        cols = _columns_in(rule.expr, columns)
        show = ", ".join(["_row"] + [q(c) for c in cols])
        failing_sql = f"SELECT {show} FROM {view} WHERE NOT ({rule.expr})"
        r.name = r.name or rule.expr
        r.checked = _count(con, f"SELECT 1 FROM {view} WHERE ({rule.expr}) IS NOT NULL")
        r.failing = _count(con, failing_sql)
        r.examples = _rows(con, failing_sql + " ORDER BY _row", max_examples)
        r.detail = f"{r.failing} of {r.checked} rows break the condition"
        if r.checked < rows:
            r.detail += f" ({rows - r.checked} rows not evaluated: missing values)"

    elif rule.unique is not None:
        keys = ", ".join(q(c) for c in rule.unique)
        groups = f"SELECT {keys}, COUNT(*) AS n, list(_row ORDER BY _row) AS rows FROM {view} GROUP BY {keys} HAVING COUNT(*) > 1"
        r.name = r.name or f"unique ({', '.join(rule.unique)})"
        r.failing = con.execute(f"SELECT COALESCE(SUM(n - 1), 0) FROM ({groups})").fetchall()[0][0]
        r.checked = rows
        r.examples = _rows(con, groups + " ORDER BY n DESC", max_examples)
        r.detail = f"{r.failing} row(s) repeat a key that already occurred"

    elif rule.not_null is not None:
        cond = " OR ".join(f"{q(c)} IS NULL" for c in rule.not_null)
        failing_sql = f"SELECT _row, {', '.join(q(c) for c in rule.not_null)} FROM {view} WHERE {cond}"
        r.name = r.name or f"always filled: {', '.join(rule.not_null)}"
        r.checked = rows
        r.failing = _count(con, failing_sql)
        r.examples = _rows(con, failing_sql + " ORDER BY _row", max_examples)
        raw_cols = [(c, target.raw_column(c)) for c in rule.not_null]
        raw_cols = [(c, rc) for c, rc in raw_cols if rc is not None]
        if target.raw and raw_cols:   # show what the value was in the source file, if there was one
            raw_examples = []
            for e in r.examples:
                vals = con.execute(f"SELECT {', '.join(q(rc) for _, rc in raw_cols)} FROM {q(target.raw)} "
                                   f"WHERE rowid + 1 = ?", [e["_row"]]).fetchall()
                vals = vals[0] if vals else [None] * len(raw_cols)
                raw_examples.append({"_row": e["_row"], **{f"raw {c}": v for (c, _), v in zip(raw_cols, vals)}})
            r.examples = raw_examples
        r.detail = f"{r.failing} row(s) missing a value or holding one that is not a valid {', '.join(rule.not_null)}"

    elif rule.between is not None:
        b = rule.between
        conds = ([f"{q(b.column)} < {b.min!r}"] if b.min is not None else []) + \
                ([f"{q(b.column)} > {b.max!r}"] if b.max is not None else [])
        failing_sql = f"SELECT _row, {q(b.column)} FROM {view} WHERE {' OR '.join(conds)}"
        r.name = r.name or f"{b.column} between {b.min} and {b.max}"
        r.checked = _count(con, f"SELECT 1 FROM {view} WHERE {q(b.column)} IS NOT NULL")
        r.failing = _count(con, failing_sql)
        r.examples = _rows(con, failing_sql + " ORDER BY _row", max_examples)
        r.detail = f"{r.failing} of {r.checked} values outside [{b.min}, {b.max}]"

    elif rule.allowed is not None:
        a = rule.allowed
        failing_sql = (f"SELECT _row, {q(a.column)} FROM {view} WHERE {q(a.column)} IS NOT NULL "
                       f"AND {q(a.column)} NOT IN ({', '.join(_lit(v) for v in a.values)})")
        r.name = r.name or f"{a.column} in {a.values}"
        r.checked = _count(con, f"SELECT 1 FROM {view} WHERE {q(a.column)} IS NOT NULL")
        r.failing = _count(con, failing_sql)
        r.examples = _rows(con, failing_sql + " ORDER BY _row", max_examples)
        r.detail = f"{r.failing} of {r.checked} values not in the allowed list"

    elif rule.row_count is not None:
        rc = rule.row_count
        ok = (rc.min is None or rows >= rc.min) and (rc.max is None or rows <= rc.max)
        r.name = r.name or f"row count between {rc.min} and {rc.max}"
        r.failing, r.checked = int(not ok), 1
        r.detail = f"{rows} rows (expected {rc.min if rc.min is not None else '-'} to {rc.max if rc.max is not None else '-'})"

    elif rule.date_coverage is not None:
        dc = rule.date_coverage
        r.name = r.name or f"{dc.column} coverage"
        dmin, dmax = con.execute(f"SELECT MIN({q(dc.column)}), MAX({q(dc.column)}) FROM {view}").fetchall()[0]
        problems = []
        # compare calendar dates: a timestamp on the start day is on time
        if dc.start and (dmin is None or str(dmin)[:10] > dc.start[:10]):
            problems.append(f"starts {dmin}, expected on or before {dc.start}")
        if dc.end and (dmax is None or str(dmax)[:10] < dc.end[:10]):
            problems.append(f"ends {dmax}, expected on or after {dc.end}")
        missing = 0
        if dc.no_gaps:
            d = prof["columns_profile"].get(dc.column, {}).get("date")
            if not d:
                raise ValueError(f"{dc.column!r} is not a date column, so gaps cannot be checked")
            missing = d.get("missing_periods") or 0
            if missing:
                problems.append(f"{missing} {d['frequency']} period(s) missing")
                if d.get("largest_gap"):
                    r.examples = [d["largest_gap"]]
        r.failing = len(problems) - (1 if missing else 0) + missing
        r.checked = 1
        r.detail = f"{dmin} to {dmax}" + ("; " + "; ".join(problems) if problems else "")

    elif rule.sql is not None:
        sql = rule.sql.replace("{table}", view).replace("{raw}", q(target.raw or target.view))
        r.name = r.name or "custom SQL check"
        r.failing = _count(con, sql)
        r.examples = _rows(con, sql, max_examples)
        r.detail = f"{r.failing} failing row(s) returned by the query"

    r.settle(rule.max_failing)
    if rule.kind in ("expr", "between", "allowed") and rows and not r.checked:
        # every value was missing or unreadable: a PASS here would mean "tested nothing"
        r.status = "WARN" if r.severity != "info" else "INFO"
        r.detail += "; no row could be evaluated (values missing or unreadable), so the rule tested nothing"
    return r


PREFIXES = ("raw_", "typed_", "stg_", "mart_")


def resolve_table(name: str, tables: list[str], stage: str) -> str | None:
    """Accept a table by its short name ('artprice__data') or any prefixed form."""
    base = name
    for p in PREFIXES:
        if base.startswith(p):
            base = base.removeprefix(p)
            break
    order = ([f"raw_{base}"] if stage == "raw" else [name, f"stg_{base}", f"mart_{base}"])
    for candidate in order:
        if candidate in tables:
            return candidate
    return None


def _short_id(table: str) -> str:
    for p in ("raw_", "stg_"):
        if table.startswith(p):
            return table.removeprefix(p)
    return table


def run_validation(con: duckdb.DuckDBPyConnection, profiles: dict, targets: dict[str, Target],
                   cfg: ValidationConfig, stage: str, max_examples: int = 3) -> dict:
    """Run the generic checks on every table in `profiles`, and the rules for this stage.
    stage 'raw' validates the data as it arrived; 'staging' the cleaned tables and marts."""
    tables = list(profiles)
    results: list[CheckResult] = []

    for t in tables:
        results.extend(generic_checks(con, targets[t], profiles[t], cfg, max_examples, stage))

    for rule in (r for r in cfg.rules if r.stage == stage):
        t = resolve_table(rule.table, tables, stage)
        if t is None:
            results.append(CheckResult(check_id="", table=rule.table, name=rule.name or rule.kind, origin="rule",
                                       stage=stage, kind=rule.kind, severity=rule.severity, status="ERROR",
                                       detail=f"table {rule.table!r} not found at stage {stage!r}; tables: {tables}"))
            continue
        try:
            results.append(run_rule(con, rule, targets[t], profiles[t], max_examples))
        except (duckdb.Error, ValueError) as exc:
            msg = str(exc).splitlines()[0]
            msg += _hint_untyped(rule, profiles[t])
            results.append(CheckResult(check_id="", table=t, name=rule.name or rule.kind, origin="rule",
                                       stage=stage, kind=rule.kind, severity=rule.severity, status="ERROR",
                                       detail=f"rule could not run: {msg}", description=rule.description))

    checks: dict[str, dict] = {}
    for r in results:
        base = f"{stage}.{_short_id(r.table)}.{r.origin}.{_slug(r.name) or r.kind}"
        cid, i = base, 2
        while cid in checks:
            cid, i = f"{base}_{i}", i + 1
        r.check_id = cid
        checks[cid] = r.to_dict()

    summary = {s: sum(c["status"] == s for c in checks.values()) for s in ("PASS", "WARN", "FAIL", "INFO", "ERROR")}
    status = ("failed" if summary["FAIL"] or summary["ERROR"]
              else "passed_with_warnings" if summary["WARN"] else "passed")
    return {"stage": stage, "status": status, "summary": summary, "checks": checks}
