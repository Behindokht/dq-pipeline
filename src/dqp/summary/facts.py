"""The fact pack: every number the summary is allowed to use.

Facts are built only from what the pipeline computed and checked: run status,
validation results, the cleaning log, the gap documentation and, when the
cleaned data passed validation, headline numbers from the cleaned tables.

Each fact has a stable ID, a value and a description:
    final.fail                 -> 0          "checks that FAIL after cleaning"
    clean1.rows_removed        -> 2          "rows removed by this step"
    key.monthly_series.cu_usd.latest -> 9871.3

The summary writer (template or LLM) never writes a number itself: it writes
{{fact.id}} and the renderer inserts the value. Text facts (a check name, a
cleaning reason) are inserted the same way, so wording that contains digits
always comes from the pipeline, never from the model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from dqp.profiling.profiler import q

NUMERIC = ("DOUBLE", "BIGINT", "INTEGER", "HUGEINT", "FLOAT", "DECIMAL", "SMALLINT")
MAX_CHECKS = 25
MAX_CLEANING = 25
MAX_GAP_COLUMNS = 10


@dataclass
class Fact:
    id: str
    value: object
    kind: str            # number | percent | text | date | status
    desc: str
    required: bool = False   # the summary must mention it (failures, removed rows)

    def to_dict(self) -> dict:
        return asdict(self)


class FactPack:
    def __init__(self):
        self.facts: dict[str, Fact] = {}

    def add(self, id: str, value, kind: str, desc: str, required: bool = False) -> str:
        if id in self.facts:
            raise ValueError(f"duplicate fact id {id}")
        self.facts[id] = Fact(id, value, kind, desc, required)
        return id

    def __getitem__(self, id: str) -> Fact:
        return self.facts[id]

    def __contains__(self, id: str) -> bool:
        return id in self.facts

    def required(self) -> list[str]:
        return [f.id for f in self.facts.values() if f.required]

    def to_dict(self) -> dict:
        return {k: f.to_dict() for k, f in self.facts.items()}


def _short(table: str) -> str:
    for p in ("raw_", "stg_", "mart_"):
        if table.startswith(p):
            return table.removeprefix(p) if p != "mart_" else table
    return table


def build_facts(con, config, log: dict, checks: dict, cleaning: dict, coverage: dict,
                stg_profiles: dict) -> FactPack:
    fp = FactPack()
    v = log["validation"]
    validated = v["status"] != "failed"

    # --- the run
    fp.add("dataset.name", config.dataset, "text", "dataset name")
    if config.description:
        fp.add("dataset.description", " ".join(config.description.split()), "text", "dataset description")
    loaded = [l for l in log["loads"] if l["table"]]
    fp.add("dataset.tables", len(loaded), "number", "tables loaded from the sources")
    fp.add("dataset.raw_rows", sum(l["rows"] for l in loaded), "number", "rows loaded in total")
    fp.add("dataset.sources", ", ".join(sorted({l["location"].split("/")[-1] for l in loaded})), "text",
           "source files")

    for stage, label in (("raw", "arrival"), ("staging", "final")):
        s = v["stages"][stage]
        what = "on arrival (raw data)" if stage == "raw" else "after cleaning (the result that counts)"
        fp.add(f"{label}.status", s["status"].replace("_", " "), "status", f"validation result {what}",
               required=True)
        for k, n in s["summary"].items():
            # a non-zero count of problems must be stated, even when there are too many to list one by one
            must = n > 0 and (k in ("FAIL", "ERROR") or (stage == "staging" and k == "WARN"))
            fp.add(f"{label}.{k.lower()}", n, "number", f"checks with status {k} {what}", required=must)
        fp.add(f"{label}.checks", sum(s["summary"].values()), "number", f"checks run {what}")

    # --- checks that did not pass (arrival: FAIL/ERROR/WARN; final: everything not PASS)
    flagged = [c for c in checks.values()
               if c["status"] in ("FAIL", "ERROR", "WARN") or (c["stage"] == "staging" and c["status"] == "INFO")]
    order = {"ERROR": 0, "FAIL": 1, "WARN": 2, "INFO": 3}
    flagged.sort(key=lambda c: (c["stage"] != "staging", order[c["status"]]))
    for k, c in enumerate(flagged[:MAX_CHECKS], start=1):
        stage = "after cleaning" if c["stage"] == "staging" else "on arrival"
        must = c["status"] in ("FAIL", "ERROR") or (c["stage"] == "staging" and c["status"] == "WARN")
        fp.add(f"check{k}.status", c["status"], "status", f"status of a check {stage}", required=must)
        fp.add(f"check{k}.stage", stage, "text", "when the check ran")
        fp.add(f"check{k}.table", c["table"], "text", "table checked")
        fp.add(f"check{k}.name", c["name"], "text", "what the check tests")
        fp.add(f"check{k}.failing", c["failing"], "number", "rows or items that failed the check")
        fp.add(f"check{k}.detail", c["detail"], "text", "the check's own result line")
    if len(flagged) > MAX_CHECKS:
        fp.add("checks.not_listed", len(flagged) - MAX_CHECKS, "number",
               "flagged checks not listed here (see validation.json)", required=True)

    # --- cleaning: steps that changed something
    changes = [e for e in cleaning["log"] if e["rows_removed"] or e["values_changed"]]
    fp.add("clean.changes", len(changes), "number", "cleaning steps that removed rows or changed values")
    rejected = sum(t["rows_rejected"] for t in cleaning["tables"].values())
    fp.add("clean.rejected_rows", rejected, "number", "rows moved to rejected_ tables (kept, not deleted)",
           required=rejected > 0)
    fp.add("clean.marts", len(cleaning["marts"]), "number", "mart tables built")
    for k, e in enumerate(changes[:MAX_CLEANING], start=1):
        fp.add(f"clean{k}.table", e["table"], "text", "table cleaned")
        fp.add(f"clean{k}.action", e["action"], "text", "cleaning action")
        fp.add(f"clean{k}.reason", e["reason"], "text", "why (from the config)")
        fp.add(f"clean{k}.rows_removed", e["rows_removed"], "number", "rows removed by this step",
               required=bool(e["rows_removed"]))
        fp.add(f"clean{k}.values_changed", e["values_changed"], "number", "values changed by this step",
               required=bool(e["values_changed"]))

    # --- gaps: documented, not filled
    for t, cov in coverage.items():
        st = _short(t)
        holes = {c: x for c, x in cov["columns"].items() if x["internal_missing"]}
        if cov["missing_period_count"] or holes:
            fp.add(f"gap.{st}.table", t, "text", "table with documented gaps")
        if cov["missing_period_count"]:
            fp.add(f"gap.{st}.missing_periods", cov["missing_period_count"], "number",
                   f"{cov['frequency']} periods missing from the date column of {t}", required=True)
            fp.add(f"gap.{st}.missing_list", ", ".join(cov["missing_periods"][:8])
                   + (" ..." if cov["missing_period_count"] > 8 else ""), "text", f"missing periods in {t}")
        if holes:
            fp.add(f"gap.{st}.columns_with_holes", len(holes), "number",
                   f"columns of {t} with values missing inside their own date range", required=True)
            for k, (col, x) in enumerate(list(holes.items())[:MAX_GAP_COLUMNS], start=1):
                fp.add(f"gap.{st}.col{k}.name", col, "text", "column with a gap")
                fp.add(f"gap.{st}.col{k}.missing", x["internal_missing"], "number", "values missing inside its range")
                fp.add(f"gap.{st}.col{k}.dates", ", ".join(x["internal_missing_dates"][:4])
                       + (" ..." if x["internal_missing"] > 4 else ""), "text", "the missing dates")

    # --- headline numbers: only from data that passed validation
    show = config.report.show_values
    fp.add("key.available", ("yes" if validated else "no") if show else "hidden", "text",
           "whether headline numbers are given (only when validation after cleaning did not fail)")
    if validated and show:
        for table, cols in _key_columns(con, config, cleaning, stg_profiles).items():
            _key_numbers(con, fp, table, cols, stg_profiles[table])
    return fp


def _key_columns(con, config, cleaning: dict, profiles: dict) -> dict[str, list[str]]:
    tables = list(profiles)
    chosen: dict[str, list[str]] = {}
    if config.summary.key_columns:
        for name, cols in config.summary.key_columns.items():
            t = next((c for c in (name, f"stg_{name}", f"mart_{name}") if c in tables), None)
            if t is None:
                raise ValueError(f"summary.key_columns: table {name!r} not found; tables: {tables}")
            have = {r[0] for r in con.execute(f"DESCRIBE {q(t)}").fetchall()}
            missing = [c for c in cols if c not in have]
            if missing:
                raise ValueError(f"summary.key_columns: {t} has no column(s) {missing}")
            chosen[t] = cols
        return chosen
    for t in cleaning["marts"]:
        nums = [r[0] for r in con.execute(f"DESCRIBE {q(t)}").fetchall()
                if r[0] != "_row" and r[1].split("(")[0] in NUMERIC]
        if nums:
            chosen[t] = nums[:5]
    return chosen


def _key_numbers(con, fp: FactPack, table: str, cols: list[str], prof: dict) -> None:
    date_col = prof.get("main_date_column")
    st = _short(table)
    fp.add(f"key.{st}.table", table, "text", "table the headline numbers come from")
    for col in cols:
        base = f"key.{st}.{col}"
        c = q(col)
        fp.add(f"{base}.name", col, "text", "column")
        if not date_col:
            mn, mx, avg = con.execute(f"SELECT MIN({c}), MAX({c}), AVG({c}) FROM {q(table)}").fetchall()[0]
            fp.add(f"{base}.min", mn, "number", f"lowest {col}")
            fp.add(f"{base}.max", mx, "number", f"highest {col}")
            fp.add(f"{base}.mean", avg, "number", f"average {col}")
            continue
        d = q(date_col)
        rows = con.execute(f"""
            WITH v AS (SELECT {d} AS d, {c} AS x FROM {q(table)} WHERE {c} IS NOT NULL)
            SELECT (SELECT x FROM v ORDER BY d DESC LIMIT 1), (SELECT d FROM v ORDER BY d DESC LIMIT 1),
                   (SELECT x FROM v ORDER BY x ASC, d LIMIT 1), (SELECT d FROM v ORDER BY x ASC, d LIMIT 1),
                   (SELECT x FROM v ORDER BY x DESC, d LIMIT 1), (SELECT d FROM v ORDER BY x DESC, d LIMIT 1),
                   (SELECT d FROM v ORDER BY d LIMIT 1)""").fetchall()[0]
        latest, latest_d, mn, mn_d, mx, mx_d, first_d = rows
        if latest is None:
            continue
        fp.add(f"{base}.latest", latest, "number", f"latest value of {col}")
        fp.add(f"{base}.latest_date", str(latest_d), "date", f"date of the latest {col}")
        fp.add(f"{base}.first_date", str(first_d), "date", f"first date with a {col} value")
        fp.add(f"{base}.min", mn, "number", f"lowest {col}")
        fp.add(f"{base}.min_date", str(mn_d), "date", f"date of the lowest {col}")
        fp.add(f"{base}.max", mx, "number", f"highest {col}")
        fp.add(f"{base}.max_date", str(mx_d), "date", f"date of the highest {col}")
        prev = con.execute(f"""SELECT {c}, {d} FROM {q(table)} WHERE {c} IS NOT NULL
                               AND {d} <= ?::DATE - INTERVAL 1 YEAR ORDER BY {d} DESC LIMIT 1""",
                           [latest_d]).fetchall()
        if prev and prev[0][0]:
            fp.add(f"{base}.change_1y_pct", (latest / prev[0][0] - 1) * 100, "percent",
                   f"change of {col} over the year to its latest date")
            fp.add(f"{base}.change_1y_from", str(prev[0][1]), "date", "date the one-year change is measured from")
