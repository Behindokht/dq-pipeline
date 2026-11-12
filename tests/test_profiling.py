"""Tests for layer 2: profiling. Each test builds a small table with a known
problem and checks the profiler reports exactly that problem."""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from dqp.connectors.base import write_frame_as_text
from dqp.profiling import ProfileSettings, build_highlights, profile_table


@pytest.fixture
def con():
    c = duckdb.connect()
    yield c
    c.close()


def make(con, data: dict, name: str = "t") -> str:
    write_frame_as_text(con, name, pd.DataFrame(data))
    return name


def prof(con, data, **settings):
    return profile_table(con, make(con, data), ProfileSettings(**settings))


# ---------- type inference ----------

def test_types_are_inferred(con):
    p = prof(con, {
        "num": ["1", "2.5", "-3", "1e3"],
        "comma": ["1,50", "2", "1.234,5", "0,1"],
        "day": ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"],
        "flag": ["yes", "no", "YES", "no"],
        "txt": ["EUR", "USD", "EUR", "GBP"],
        "none": [None, None, None, None],
    })["columns_profile"]
    assert {k: v["inferred_type"] for k, v in p.items()} == {
        "num": "numeric", "comma": "numeric_decimal_comma", "day": "date",
        "flag": "boolean", "txt": "text", "none": "empty"}


def test_footer_rows_are_caught_as_nonconforming(con):
    """The Artprice case: a date column and a price column with notes at the bottom."""
    dates = [f"{y}-01-01 00:00:00" for y in range(2000, 2040)] + ["Contact : info@example.com"]
    prices = [str(100 + i) for i in range(40)] + ["1900-04-09 00:00:00"]
    p = prof(con, {"date": dates, "price": prices})["columns_profile"]

    assert p["date"]["inferred_type"] == "date"
    assert p["date"]["nonconforming"] == 1
    assert p["date"]["nonconforming_examples"] == [{"row": 41, "value": "Contact : info@example.com"}]
    assert p["price"]["inferred_type"] == "numeric"
    assert p["price"]["nonconforming_examples"][0]["value"] == "1900-04-09 00:00:00"


def test_mixed_column_below_threshold(con):
    p = prof(con, {"x": ["1", "2", "3", "...", "..."]})["columns_profile"]["x"]
    assert p["inferred_type"] == "mixed"
    assert p["mixed_dominant_type"] == "numeric"
    assert p["nonconforming"] == 2


def test_threshold_is_configurable(con):
    data = {"x": ["1", "2", "3", "4", "n.a."]}   # 80% numeric
    assert prof(con, data)["columns_profile"]["x"]["inferred_type"] == "mixed"
    assert prof(con, data, type_threshold=0.8)["columns_profile"]["x"]["inferred_type"] == "numeric"


# ---------- missing, duplicates, keys ----------

def test_nulls_duplicates_keys_constants(con):
    p = prof(con, {"id": ["1", "2", "3", "3"], "v": ["a", None, "b", "b"], "c": ["k", "k", "k", "k"]})
    assert p["duplicate_rows"] == 1
    assert p["candidate_keys"] == []           # id repeats because of the duplicate row
    assert p["constant_columns"] == ["c"]
    v = p["columns_profile"]["v"]
    assert (v["nulls"], v["null_share"], v["distinct_values"]) == (1, 0.25, 2)


def test_whitespace_is_counted(con):
    p = prof(con, {"x": ["a", " a", "b ", "b"]})["columns_profile"]["x"]
    assert p["whitespace_values"] == 2


# ---------- numeric ----------

def test_numeric_stats_and_outliers(con):
    values = [str(v) for v in [10, 11, 12, 13, 14, 15, 16, 17, 18, 1000]]
    n = prof(con, {"x": values})["columns_profile"]["x"]["numeric"]
    assert (n["count"], n["min"], n["max"], n["median"]) == (10, 10.0, 1000.0, 14.5)
    assert n["outliers"] == 1
    assert n["outlier_examples"] == [{"row": 10, "value": 1000.0}]


def test_decimal_comma_values_are_parsed(con):
    n = prof(con, {"x": ["1,5", "2,5", "1.000,5", "3"]})["columns_profile"]["x"]["numeric"]
    assert (n["min"], n["max"]) == (1.5, 1000.5)


def test_constant_numeric_column_has_no_outliers(con):
    n = prof(con, {"x": ["5"] * 10})["columns_profile"]["x"]["numeric"]
    assert n["outliers"] == 0


# ---------- dates ----------

@pytest.mark.parametrize("values, freq", [
    (["1998Q1", "1998Q2", "1998Q3", "1998Q4"], "quarterly"),
    (["1960M01", "1960M02", "1960M03", "1960M04"], "monthly"),
    (["2024-01", "2024-02", "2024-03", "2024-04"], "monthly"),
    (["1971-12-31", "1972-01-31", "1972-02-29", "1972-03-31"], "monthly"),   # month ends
    (["2020-01-01", "2021-01-01", "2022-01-01", "2023-01-01"], "annual"),
    (["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-06"], "daily"),      # includes a Saturday
    (["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"], "business_daily"),
])
def test_date_formats_and_frequency(con, values, freq):
    d = prof(con, {"d": values})["columns_profile"]["d"]
    assert d["inferred_type"] == "date"
    assert d["date"]["frequency"] == freq


def test_missing_quarters_and_largest_gap(con):
    quarters = ["2000Q1", "2000Q2", "2001Q2", "2001Q3", "2001Q4", "2002Q1"]   # 2000Q3..2001Q1 missing
    d = prof(con, {"q": quarters})["columns_profile"]["q"]["date"]
    assert (d["expected_periods"], d["missing_periods"]) == (9, 3)
    assert d["largest_gap"] == {"missing_periods": 3, "after": "2000-04-01", "before": "2001-04-01"}


def test_business_days_skip_weekends(con):
    # Fri 5 Jan -> Mon 8 Jan is NOT a gap; Mon 8 -> Wed 10 is one missing business day
    d = prof(con, {"d": ["2024-01-04", "2024-01-05", "2024-01-08", "2024-01-10", "2024-01-11"]})
    d = d["columns_profile"]["d"]["date"]
    assert d["frequency"] == "business_daily"
    assert d["missing_periods"] == 1


def test_a_weekday_without_rows_is_a_closed_day_not_a_gap(con):
    # a shop open Monday to Saturday for four weeks, one Wednesday missing
    days = [d for d in pd.date_range("2026-02-02", "2026-02-28") if d.day_of_week != 6]
    days.remove(pd.Timestamp("2026-02-11"))
    d = prof(con, {"d": [str(x.date()) for x in days]})["columns_profile"]["d"]["date"]
    assert d["frequency"] == "daily" and d["closed_days"] == ["Sun"]
    assert d["missing_periods"] == 1 and d["expected_periods"] == 24       # Sundays are not expected
    assert "no rows on any Sun" in d["note"]                              # said, not hidden


def test_two_weeks_are_too_few_to_call_a_day_closed(con):
    days = [d for d in pd.date_range("2026-02-02", "2026-02-14") if d.day_of_week != 6]
    d = prof(con, {"d": [str(x.date()) for x in days]})["columns_profile"]["d"]["date"]
    assert "closed_days" not in d and d["missing_periods"] == 1


def test_whole_numbers_are_typed_as_integers(con):
    from dqp.validation.typed import create_typed_view

    p = prof(con, {"qty": ["1", "2", "-3", "4"], "price": ["1.50", "2", "3", "4"]})
    assert p["columns_profile"]["qty"]["numeric"]["integers"] is True
    assert p["columns_profile"]["price"]["numeric"]["integers"] is False
    view = create_typed_view(con, "t", p)
    types = {r[0]: r[1] for r in con.execute(f'DESCRIBE "{view}"').fetchall()}
    assert types["qty"] == "BIGINT" and types["price"] == "DOUBLE"


def test_duplicate_dates(con):
    p = prof(con, {"d": ["2024-01", "2024-02", "2024-02", "2024-03"], "v": ["1", "2", "3", "4"]})
    d = p["columns_profile"]["d"]["date"]
    assert d["duplicate_dates"] == 1
    assert d["duplicate_examples"] == [{"date": "2024-02-01", "count": 2}]
    assert p["main_date_column"] == "d"


def test_slash_dates_day_first_detected(con):
    d = prof(con, {"d": ["31/01/2024", "29/02/2024", "31/03/2024"]})["columns_profile"]["d"]["date"]
    assert (d["min"], d["max"]) == ("2024-01-31", "2024-03-31")
    assert "ambiguous_day_month" not in d


def test_ambiguous_slash_dates_are_flagged(con):
    p = prof(con, {"d": ["01/02/2024", "01/03/2024", "01/04/2024"]})
    assert p["columns_profile"]["d"]["date"]["ambiguous_day_month"] is True
    assert any("day-first or month-first" in h["text"] for h in build_highlights("t", p))


# ---------- highlights ----------

def test_highlights_point_to_their_facts(con):
    p = prof(con, {"price": ["1", "2", "3"] * 10 + ["see note"], "x": ["a"] * 31})
    hs = build_highlights("t", p)
    nonconf = [h for h in hs if "don't look numeric" in h["text"]]
    assert len(nonconf) == 1
    assert nonconf[0]["level"] == "attention"
    assert nonconf[0]["facts"] == ["tables.t.columns_profile.price.nonconforming"]


def test_clean_table_has_no_attention_highlights(con):
    p = prof(con, {"m": [f"2024-{m:02d}" for m in range(1, 13)], "v": [str(i) for i in range(12)]})
    assert [h for h in build_highlights("t", p) if h["level"] == "attention"] == []


def test_column_names_with_special_characters(con):
    p = prof(con, {'Crude oil, WTI ($/bbl)': ["1", "2"], 'say "hi"': ["a", "b"]})
    assert set(p["columns_profile"]) == {'Crude oil, WTI ($/bbl)', 'say "hi"'}


@pytest.mark.parametrize("values", [
    ["31-01-2024", "29-02-2024", "31-03-2024"],      # Dutch
    ["31.01.2024", "29.02.2024", "31.03.2024"],      # German
])
def test_day_first_dates_with_dash_or_dot(con, values):
    d = prof(con, {"d": values})["columns_profile"]["d"]
    assert d["inferred_type"] == "date"
    assert (d["date"]["min"], d["date"]["max"], d["date"]["frequency"]) == ("2024-01-31", "2024-03-31", "monthly")


def test_mixed_separators_are_not_a_date(con):
    assert prof(con, {"d": ["31-01/2024", "29-02/2024"]})["columns_profile"]["d"]["inferred_type"] == "text"
