"""Tests for layer 3: validation. Each test writes a small CSV with a known
problem, runs the full pipeline, and checks the right check reports it."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml
from pydantic import ValidationError

from dqp.cli import main
from dqp.config import load_config
from dqp.pipeline import run

CSV = """id,date,amount,currency,flag
1,2024-01-31,10.5,EUR,0
2,2024-02-29,-3,USD,1
3,2024-03-31,7,EUR,0
3,2024-05-31,abc,GBP,2
"""
# row 2: negative amount | row 4: duplicate id, text in amount, flag 2, April missing


def setup(tmp_path: Path, rules: list | None = None, generic: dict | None = None, csv: str = CSV) -> Path:
    data = tmp_path / "pay.csv"
    data.write_text(csv, encoding="utf-8")
    cfg = {"dataset": "pay", "output_dir": str(tmp_path / "out"),
           "sources": [{"name": "pay", "type": "csv", "path": str(data)}],
           "validation": {"rules": rules or [], "generic": generic or {}}}
    p = tmp_path / "pay.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def checks(log: dict, origin: str | None = None, stage: str = "staging") -> list[dict]:
    """Checks of one stage (default: after cleaning, the result that counts)."""
    return [c for c in log["checks"].values()
            if c["stage"] == stage and (origin is None or c["origin"] == origin)]


def one_rule(tmp_path, rule: dict) -> dict:
    log = run(setup(tmp_path, [{"table": "pay", **rule}]))
    (c,) = checks(log, "rule")
    return c


# ---------- typed views ----------

def test_typed_view_casts_and_keeps_row_numbers(tmp_path):
    log = run(setup(tmp_path))
    con = duckdb.connect(log["db_path"], read_only=True)
    types = dict(con.execute("SELECT column_name, column_type FROM (DESCRIBE typed_pay)").fetchall())
    assert types["amount"] == "DOUBLE" and types["date"] == "DATE" and types["currency"] == "VARCHAR"
    # the untypable 'abc' is NULL in the view but still in the raw table
    assert con.execute("SELECT amount FROM typed_pay WHERE _row = 4").fetchall() == [(None,)]
    assert con.execute("SELECT amount FROM raw_pay WHERE rowid = 3").fetchall() == [("abc",)]


# ---------- generic checks ----------

def test_generic_checks_find_type_problems_and_gaps(tmp_path):
    log = run(setup(tmp_path))
    by_kind = {}
    for c in checks(log, "generic"):
        by_kind.setdefault(c["kind"], []).append(c)

    amount = next(c for c in by_kind["values_fit_type"] if "'amount'" in c["name"])
    assert (amount["status"], amount["failing"]) == ("WARN", 1)
    assert amount["examples"] == [{"row": 4, "value": "abc"}]

    (gaps,) = by_kind["no_missing_periods"]
    assert (gaps["status"], gaps["failing"]) == ("WARN", 1)      # April 2024
    assert by_kind["table_not_empty"][0]["status"] == "PASS"


def test_generic_severity_can_be_changed_or_switched_off(tmp_path):
    log = run(setup(tmp_path, generic={"values_fit_type": "fail", "no_missing_periods": "off"}))
    kinds = {c["kind"]: c["status"] for c in checks(log, "generic") if c["status"] != "PASS"}
    assert kinds == {"values_fit_type": "FAIL"}
    assert log["validation"]["status"] == "failed"


def test_unknown_generic_check_is_a_config_error(tmp_path):
    with pytest.raises(ValidationError, match="unknown generic checks"):
        load_config(setup(tmp_path, generic={"no_typos": "warn"}))


def test_business_day_gaps_are_info_not_warn(tmp_path):
    csv = "d,v\n2024-01-01,1\n2024-01-02,1\n2024-01-03,1\n2024-01-05,1\n"   # Thu 4 Jan missing
    log = run(setup(tmp_path, csv=csv))
    (gap,) = [c for c in checks(log) if c["kind"] == "no_missing_periods"]
    assert (gap["status"], gap["failing"]) == ("INFO", 1)


# ---------- rule kinds ----------

def test_expr_rule(tmp_path):
    c = one_rule(tmp_path, {"expr": "amount >= 0"})
    assert (c["status"], c["failing"], c["checked"]) == ("FAIL", 1, 3)   # 'abc' row is not evaluated
    assert c["examples"] == [{"_row": 2, "amount": -3.0}]
    assert "1 rows not evaluated" in c["detail"]


def test_unique_rule(tmp_path):
    c = one_rule(tmp_path, {"unique": ["id"]})
    assert (c["status"], c["failing"]) == ("FAIL", 1)
    assert c["examples"][0]["rows"] == [3, 4]


def test_not_null_rule_shows_the_raw_value(tmp_path):
    c = one_rule(tmp_path, {"not_null": ["amount"]})
    assert (c["status"], c["failing"]) == ("FAIL", 1)
    assert c["examples"] == [{"_row": 4, "raw amount": "abc"}]


def test_between_rule(tmp_path):
    c = one_rule(tmp_path, {"between": {"column": "amount", "min": 0, "max": 10}})
    assert c["failing"] == 2                       # -3 and 10.5
    assert [e["_row"] for e in c["examples"]] == [1, 2]


def test_allowed_rule_on_numbers(tmp_path):
    c = one_rule(tmp_path, {"allowed": {"column": "flag", "values": [0, 1]}})
    assert (c["failing"], c["examples"]) == (1, [{"_row": 4, "flag": 2.0}])


def test_allowed_rule_on_text(tmp_path):
    c = one_rule(tmp_path, {"allowed": {"column": "currency", "values": ["EUR", "USD"]}})
    assert c["examples"] == [{"_row": 4, "currency": "GBP"}]


def test_row_count_rule(tmp_path):
    assert one_rule(tmp_path, {"row_count": {"min": 10}})["status"] == "FAIL"


def test_date_coverage_rule(tmp_path):
    c = one_rule(tmp_path, {"date_coverage": {"column": "date", "start": "2023-12-31",
                                               "end": "2024-06-30", "no_gaps": True}})
    assert c["status"] == "FAIL"
    assert c["failing"] == 3          # starts late, ends early, 1 missing month
    assert "starts 2024-01-31" in c["detail"] and "1 monthly period(s) missing" in c["detail"]


def test_sql_rule_with_table_placeholder(tmp_path):
    c = one_rule(tmp_path, {"sql": "SELECT _row, amount FROM {table} WHERE amount < 0"})
    assert (c["status"], c["failing"], c["examples"]) == ("FAIL", 1, [{"_row": 2, "amount": -3.0}])


def test_severity_warn(tmp_path):
    assert one_rule(tmp_path, {"expr": "amount >= 0", "severity": "warn"})["status"] == "WARN"


def test_tolerance_max_failing(tmp_path):
    assert one_rule(tmp_path, {"expr": "amount >= 0", "max_failing": 1})["status"] == "PASS"


def test_quoted_column_names_in_expressions(tmp_path):
    csv = 'Price ($/t),Crude oil, WTI\n1,2\n'.replace("Crude oil, WTI", '"Crude oil, WTI"')
    log = run(setup(tmp_path, [{"table": "pay", "expr": '"Price ($/t)" > 0 AND "Crude oil, WTI" > 0'}], csv=csv))
    (c,) = checks(log, "rule")
    assert c["status"] == "PASS"


# ---------- broken rules don't break the run ----------

def test_rule_with_unknown_column_is_an_error_not_a_crash(tmp_path):
    log = run(setup(tmp_path, [{"table": "pay", "expr": "amuont > 0"}, {"table": "pay", "expr": "amount > -100"}]))
    statuses = [c["status"] for c in checks(log, "rule")]
    assert statuses == ["ERROR", "PASS"]
    assert log["validation"]["status"] == "failed"


def test_rule_on_unknown_table_is_an_error(tmp_path):
    log = run(setup(tmp_path, [{"table": "payments", "unique": ["id"]}]))
    (c,) = checks(log, "rule")
    assert c["status"] == "ERROR" and "not found" in c["detail"]


def test_rule_needs_exactly_one_kind(tmp_path):
    with pytest.raises(ValidationError, match="exactly one"):
        load_config(setup(tmp_path, [{"table": "pay", "expr": "x > 0", "unique": ["id"]}]))
    with pytest.raises(ValidationError, match="exactly one"):
        load_config(setup(tmp_path, [{"table": "pay", "name": "nothing"}]))


def test_table_can_be_named_with_or_without_prefix(tmp_path):
    log = run(setup(tmp_path, [{"table": t, "row_count": {"min": 1}} for t in ("pay", "raw_pay", "typed_pay")]))
    assert [c["status"] for c in checks(log, "rule")] == ["PASS"] * 3


# ---------- outputs ----------

def test_check_ids_are_unique_and_results_stored(tmp_path):
    log = run(setup(tmp_path, [{"table": "pay", "expr": "amount >= 0"}, {"table": "pay", "expr": "amount >= 0"}]))
    ids = list(log["checks"])
    assert len(ids) == len(set(ids))
    assert "staging.pay.rule.amount_0_2" in ids
    assert "raw.pay.generic.table_has_rows" in ids
    con = duckdb.connect(log["db_path"], read_only=True)
    assert con.execute("SELECT COUNT(*) FROM _dqp_checks").fetchall()[0][0] == len(ids)
    assert (Path(log["db_path"]).parent / "validation.json").exists()


def test_cli_strict_exit_code(tmp_path, capsys):
    failing = setup(tmp_path, [{"table": "pay", "expr": "amount >= 0"}])
    assert main(["run", str(failing)]) == 0
    assert main(["run", "--strict", str(failing)]) == 2
    out = capsys.readouterr().out
    assert "FAIL" in out and "_row=2" in out


# ---------- edge cases found in the review ----------

def test_rule_that_evaluates_nothing_warns_instead_of_passing(tmp_path):
    # 'early' and 'late' are numeric but never filled in the same row, so
    # "early > late" can't be evaluated anywhere: that must not be a PASS
    csv = "id,early,late\n1,5,\n2,6,\n3,,7\n4,,8\n"
    log = run(setup(tmp_path, [{"table": "pay", "expr": "early > late", "name": "never comparable"},
                               {"table": "pay", "expr": "early > 0", "name": "comparable"}], csv=csv))
    r = {c["name"]: c for c in checks(log, "rule")}
    assert r["comparable"]["status"] == "PASS"
    assert r["never comparable"]["status"] == "WARN" and r["never comparable"]["checked"] == 0
    assert "tested nothing" in r["never comparable"]["detail"]


def test_rule_on_unreadable_column_explains_why(tmp_path):
    csv = "d,v\n2024-01,n.a.\n2024-02,n.a.\n"
    log = run(setup(tmp_path, [{"table": "pay", "expr": "v > 0"}], csv=csv))
    (c,) = checks(log, "rule")
    assert c["status"] == "ERROR"
    assert "Hint: 'v' is text (2 non-empty value(s), none readable as a number or date)" in c["detail"]


def test_unquoted_off_in_yaml_switches_a_check_off(tmp_path):
    p = setup(tmp_path)
    p.write_text(p.read_text().replace("generic: {}", "generic:\n    no_missing_periods: off"), encoding="utf-8")
    assert "no_missing_periods: off" in p.read_text()
    log = run(p)
    assert not [c for c in log["checks"].values() if c["kind"] == "no_missing_periods"]


def test_date_coverage_with_timestamps_compares_calendar_days(tmp_path):
    csv = "t,v\n2024-01-01 07:45:00,1\n2024-01-02 09:00:00,2\n2024-01-03 18:30:00,3\n"
    log = run(setup(tmp_path, [{"table": "pay", "date_coverage": {"column": "t", "start": "2024-01-01",
                                                                   "end": "2024-01-03"}}], csv=csv))
    (c,) = checks(log, "rule")
    assert c["status"] == "PASS", c["detail"]
