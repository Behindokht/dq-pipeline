"""Tests for layer 4: cleaning (raw -> staging -> mart), rejected rows,
lineage, gap documentation, and validation before vs after cleaning."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
import yaml
from pydantic import ValidationError

from dqp.cleaning import CleaningError, missing_periods, snake
from dqp.config import load_config
from dqp.pipeline import run

CSV = """Date,Price (EUR),Note
2024-01,10, a
2024-02,11,b
2024-02,11,b
2024-03,n.a.,c
2024-05,14,d
Source: example bank,,
"""
# a duplicate row, a text value in a number column, a missing month (April), a footer line


def setup(tmp_path: Path, cleaning: dict | None = None, marts: list | None = None,
          rules: list | None = None, csv: str = CSV) -> Path:
    data = tmp_path / "prices.csv"
    data.write_text(csv, encoding="utf-8")
    cfg = {"dataset": "prices", "output_dir": str(tmp_path / "out"),
           "sources": [{"name": "prices", "type": "csv", "path": str(data)}],
           "cleaning": cleaning or {}, "marts": marts or [], "validation": {"rules": rules or []}}
    p = tmp_path / "prices.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return p


def db(log):
    return duckdb.connect(log["db_path"], read_only=True)


def steps(log, table="stg_prices"):
    return [e for e in log["cleaning_log"] if e["table"] == table]


FOOTER = {"drop_rows": {"where": "Date IS NULL", "reason": "footer line under the data"}}


# ---------- staging basics ----------

def test_staging_is_typed_and_keeps_source_row_numbers(tmp_path):
    log = run(setup(tmp_path))
    con = db(log)
    types = dict(con.execute("SELECT column_name, column_type FROM (DESCRIBE stg_prices)").fetchall())
    assert types == {"_row": "BIGINT", "Date": "DATE", "Price (EUR)": "DOUBLE", "Note": "VARCHAR"}
    assert con.execute("SELECT COUNT(*) FROM stg_prices").fetchall()[0][0] == 6   # nothing removed without a step
    # raw is never changed
    assert con.execute("SELECT \"Price (EUR)\" FROM raw_prices WHERE rowid = 3").fetchall() == [("n.a.",)]


def test_values_that_cannot_be_typed_are_logged_not_hidden(tmp_path):
    log = run(setup(tmp_path, {"prices": [FOOTER]}))
    (cast,) = [e for e in steps(log) if e["action"] == "type_cast"]
    assert cast["values_changed"] == 1
    assert cast["examples"] == [{"_row": 4, "column": "Price (EUR)", "raw value": "n.a."}]
    # and validation after cleaning still reports it
    (c,) = [c for c in log["checks"].values()
            if c["stage"] == "staging" and c["kind"] == "values_fit_type" and "Price" in c["name"]]
    assert (c["status"], c["failing"]) == ("WARN", 1)


def test_replace_to_null_is_intentional_not_a_cast_loss(tmp_path):
    csv = "d,v\n2024-01,1\n2024-02,-999\n2024-03,3\n"
    log = run(setup(tmp_path, {"prices": [{"replace": {"column": "v", "values": {"-999": None},
                                                       "reason": "-999 is the source's missing-value code"}}]}, csv=csv))
    (rep,) = [e for e in steps(log) if e["action"] == "replace"]
    assert rep["values_changed"] == 1 and rep["examples"] == [{"_row": 2, "from": "-999", "to": None}]
    assert not [e for e in steps(log) if e["action"] == "type_cast"]


# ---------- rows move to rejected, never vanish ----------

def test_drop_rows_moves_raw_rows_to_rejected_with_reason(tmp_path):
    log = run(setup(tmp_path, {"prices": [FOOTER]}))
    con = db(log)
    rej = con.execute("SELECT _step, _action, _reason, _row, Date FROM rejected_prices").fetchall()
    assert rej == [(1, "drop_rows", "footer line under the data", 6, "Source: example bank")]
    (drop,) = [e for e in steps(log) if e["action"] == "drop_rows"]
    assert (drop["rows_before"], drop["rows_after"], drop["rows_removed"]) == (6, 5, 1)
    assert log["cleaning"]["stg_prices"] == {"rows_raw": 6, "rows_staging": 5, "rows_rejected": 1}


def test_drop_duplicates_keeps_first(tmp_path):
    log = run(setup(tmp_path, {"prices": [FOOTER, {"drop_duplicates": {"columns": ["Date"],
                                                                       "reason": "one price per month"}}]}))
    con = db(log)
    assert con.execute("SELECT _row FROM rejected_prices WHERE _action = 'drop_duplicates'").fetchall() == [(3,)]
    assert con.execute("SELECT COUNT(*) FROM stg_prices WHERE Date = DATE '2024-02-01'").fetchall()[0][0] == 1


def test_exact_duplicates_without_columns(tmp_path):
    log = run(setup(tmp_path, {"prices": [{"drop_duplicates": {}}]}))
    (e,) = [e for e in steps(log) if e["action"] == "drop_duplicates"]
    assert e["rows_removed"] == 1


# ---------- column steps and lineage ----------

def test_snake_case_rename_and_lineage(tmp_path):
    log = run(setup(tmp_path, {"prices": [{"snake_case": True}, {"rename": {"price_eur": "price"}}, FOOTER | {
        "drop_rows": {"where": "date IS NULL", "reason": "footer line"}}]}))
    con = db(log)
    cols = [r[0] for r in con.execute("DESCRIBE stg_prices").fetchall()]
    assert cols == ["_row", "date", "price", "note"]
    info = json.loads((Path(log["db_path"]).parent / "cleaning.json").read_text())["tables"]["stg_prices"]
    assert info["lineage"] == {"date": "Date", "price": "Price (EUR)", "note": "Note"}


def test_trim_whitespace_counts_changes(tmp_path):
    log = run(setup(tmp_path, {"prices": [{"trim_whitespace": True}]}))
    (e,) = [e for e in steps(log) if e["action"] == "trim_whitespace"]
    assert (e["values_changed"], e["columns"]) == (1, ["Note"])   # ' a' -> 'a'


def test_drop_columns(tmp_path):
    log = run(setup(tmp_path, {"prices": [{"drop_columns": {"columns": ["Note"], "reason": "free text, not used"}}]}))
    assert "Note" not in [r[0] for r in db(log).execute("DESCRIBE stg_prices").fetchall()]


def test_fill_forward_is_flagged_and_limited(tmp_path):
    csv = "d,v\n2024-01,1\n2024-02,\n2024-03,\n2024-04,4\n"
    log = run(setup(tmp_path, {"prices": [{"fill_forward": {"column": "v", "order_by": "d", "limit": 1,
                                                            "reason": "price unchanged when not reported"}}]}, csv=csv))
    rows = db(log).execute("SELECT v, v_filled FROM stg_prices ORDER BY d").fetchall()
    assert rows == [(1.0, False), (1.0, True), (None, False), (4.0, False)]   # only 1 filled: limit
    (e,) = [e for e in steps(log) if e["action"] == "fill_forward"]
    assert e["values_changed"] == 1


# ---------- config and errors ----------

def test_reason_is_required(tmp_path):
    with pytest.raises(ValidationError):
        load_config(setup(tmp_path, {"prices": [{"drop_rows": {"where": "Date IS NULL"}}]}))


def test_step_needs_exactly_one_kind(tmp_path):
    with pytest.raises(ValidationError, match="exactly one"):
        load_config(setup(tmp_path, {"prices": [{"snake_case": True, "trim_whitespace": True}]}))


def test_unknown_column_in_a_step_stops_the_run(tmp_path):
    with pytest.raises(CleaningError, match="unknown column"):
        run(setup(tmp_path, {"prices": [{"rename": {"Prijs": "price"}}]}))


def test_bad_sql_in_a_step_stops_the_run(tmp_path):
    with pytest.raises(CleaningError, match="step 1"):
        run(setup(tmp_path, {"prices": [{"drop_rows": {"where": "Datum IS NULL", "reason": "typo on purpose"}}]}))


def test_cleaning_for_unknown_table_is_an_error(tmp_path):
    with pytest.raises(CleaningError, match="unknown table"):
        run(setup(tmp_path, {"price": [{"snake_case": True}]}))


# ---------- marts ----------

def test_mart_is_built_and_validated(tmp_path):
    log = run(setup(tmp_path, {"prices": [FOOTER]},
                    marts=[{"name": "monthly", "sql": 'SELECT _row, Date AS month, "Price (EUR)" AS price FROM stg_prices'}],
                    rules=[{"table": "monthly", "row_count": {"min": 5, "max": 5}},
                           {"table": "monthly", "expr": "price > 0"}]))
    con = db(log)
    assert con.execute("SELECT COUNT(*) FROM mart_monthly").fetchall()[0][0] == 5
    rules = [c for c in log["checks"].values() if c["origin"] == "rule"]
    assert [c["status"] for c in rules] == ["PASS", "PASS"]


def test_mart_without_row_column_gets_numbered(tmp_path):
    log = run(setup(tmp_path, marts=[{"name": "n", "sql": "SELECT COUNT(*) AS rows FROM stg_prices"}]))
    assert db(log).execute("SELECT _row, rows FROM mart_n").fetchall() == [(1, 6)]


def test_broken_mart_stops_the_run(tmp_path):
    with pytest.raises(CleaningError, match="mart 'x'"):
        run(setup(tmp_path, marts=[{"name": "x", "sql": "SELECT * FROM stg_nope"}]))


# ---------- gaps are documented, not filled ----------

def test_missing_periods_are_listed(tmp_path):
    log = run(setup(tmp_path, {"prices": [FOOTER]}))
    cov = log["coverage"]["stg_prices"]
    assert (cov["frequency"], cov["missing_periods"]) == ("monthly", ["2024-04"])
    assert db(log).execute("SELECT missing_periods FROM _dqp_gaps").fetchall() == [('["2024-04"]',)]


def test_internal_value_gaps_vs_late_start(tmp_path):
    csv = "d,early,late\n2024-01,1,\n2024-02,,\n2024-03,3,30\n2024-04,4,40\n"
    cov = run(setup(tmp_path, csv=csv))["coverage"]["stg_prices"]["columns"]
    assert cov["early"]["internal_missing_dates"] == ["2024-02-01"]          # a hole: documented
    assert (cov["late"]["first"], cov["late"]["internal_missing"]) == ("2024-03-01", 0)   # just starts later


@pytest.mark.parametrize("dates, freq, expected", [
    (["2000-01-01", "2000-07-01"], "quarterly", ["2000Q2"]),
    (["2020-01-01", "2023-01-01"], "annual", ["2021", "2022"]),
    (["2024-01-05", "2024-01-09"], "business_daily", ["2024-01-08"]),   # weekend skipped
])
def test_missing_period_labels(dates, freq, expected):
    import pandas as pd
    assert missing_periods(pd.Series(pd.to_datetime(dates)), freq) == expected


# ---------- before vs after cleaning ----------

def test_raw_rule_fails_but_cleaned_data_passes(tmp_path):
    rule = {"name": "every row has a date", "not_null": ["Date"]}
    log = run(setup(tmp_path, {"prices": [FOOTER]},
                    rules=[{"table": "prices", "stage": "raw", **rule}, {"table": "prices", **rule}]))
    by_stage = {c["stage"]: c for c in log["checks"].values() if c["origin"] == "rule"}
    assert by_stage["raw"]["status"] == "FAIL"
    assert by_stage["raw"]["examples"] == [{"_row": 6, "raw Date": "Source: example bank"}]
    assert by_stage["staging"]["status"] == "PASS"
    assert log["validation"]["stages"]["raw"]["status"] == "failed"
    assert log["validation"]["status"] != "failed"     # the run is judged on the cleaned data


@pytest.mark.parametrize("name, expected", [
    ("Global Index (USD)", "global_index_usd"), ("Crude oil, WTI ($/bbl)", "crude_oil_wti_bbl"),
    ("2nd quarter", "c_2nd_quarter"), ("  ", "column"), ("already_snake", "already_snake"),
])
def test_snake(name, expected):
    assert snake(name) == expected
