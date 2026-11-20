"""Try a rule before it is added: the cleaned data is built once in memory (the same
loading, typing and cleaning as `dqp run`), then each rule is checked against it.

The GUI rule builder uses this to offer only rule kinds that fit a column's type and to
show straight away how many rows a new rule would fail, or why it cannot run."""

from __future__ import annotations

import copy

from dqp.config import DatasetConfig, Rule
from dqp.errors import sql_problem
from dqp.pipeline import prepare_staging
from dqp.validation.checks import check_rule

NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
           "UBIGINT", "FLOAT", "DOUBLE", "REAL")
KINDS = {
    "number": ["never empty", "unique", "between", "a condition (SQL)"],
    "date": ["never empty", "unique", "a condition (SQL)"],
    "true/false": ["never empty", "a condition (SQL)"],
    "text": ["never empty", "unique", "one of", "a condition (SQL)"],
}


def kind_of(sql_type: str) -> str:
    t = sql_type.upper()
    if t in NUMERIC or t.startswith("DECIMAL"):
        return "number"
    if t.startswith(("DATE", "TIMESTAMP", "TIME")):
        return "date"
    if t == "BOOLEAN":
        return "true/false"
    return "text"


class RulePreview:
    """The cleaned tables of one config, in memory. Close it when done."""

    def __init__(self, cfg: dict):
        cfg = copy.deepcopy(cfg)
        cfg.pop("validation", None)                 # rules do not change the cleaned data
        cfg["summary"] = {"provider": "none"}
        cfg.setdefault("output_dir", "unused")
        self.config = DatasetConfig.model_validate(cfg)
        self.con, self.profiles, self.targets = prepare_staging(self.config)

    def close(self) -> None:
        self.con.close()

    def tables(self) -> list[str]:
        return [t.removeprefix("stg_") for t in self.profiles if t.startswith("stg_")]

    def columns(self, table: str) -> dict[str, str]:
        """Column -> 'number' | 'date' | 'true/false' | 'text', from the real staging types."""
        rows = self.con.execute(f"DESCRIBE \"stg_{table}\"").fetchall()
        return {r[0]: kind_of(r[1]) for r in rows if r[0] != "_row"}

    def check(self, table: str, rule: dict) -> dict:
        """{'ok': bool, 'failing': int, 'checked': int|None, 'message': str} for one rule."""
        try:
            r = Rule.model_validate({"table": table, **rule})
        except ValueError as exc:
            return {"ok": False, "failing": 0, "checked": None, "message": f"the rule is not complete: {exc}"}
        res = check_rule(self.con, r, self.profiles, self.targets, "staging", 3)
        if res.status == "ERROR":
            why = res.detail.removeprefix("rule could not run: ")
            return {"ok": False, "failing": 0, "checked": None, "message": f"this rule cannot run: {sql_problem(why)}"}
        rows = self.profiles[f"stg_{table}"]["rows"]
        if res.failing:
            msg = f"{res.failing:,} of {rows:,} rows break this rule now"
        else:
            msg = f"all {rows:,} rows meet this rule now"
        return {"ok": True, "failing": res.failing, "checked": res.checked, "message": msg}
