"""Gap documentation.

The pipeline does not fill gaps silently. Instead it writes down, for every
cleaned table and mart:

  - which periods are missing from the date column (e.g. '2001-09', '2003Q2')
  - for every other column: the first and last date it has a value, how many
    values it has, and the dates *inside* that span where it is missing
    (an internal gap, like copper's October 2025 CPI hole), as opposed to a
    series that simply starts later (Bitcoin from 2010)

This goes to coverage.json and _dqp_coverage, and later into the report.
"""

from __future__ import annotations

import pandas as pd

from dqp.profiling.profiler import _period_index, open_days, q

MAX_LISTED = 12


def _label(idx: int, freq: str) -> str:
    if freq == "monthly":
        y, m = divmod(idx - 1, 12)
        return f"{y}-{m + 1:02d}"
    if freq == "quarterly":
        y, k = divmod(idx, 4)
        return f"{y}Q{k + 1}"
    if freq == "annual":
        return str(idx)
    if freq == "weekly":
        return str((pd.Timestamp("1970-01-05") + pd.Timedelta(weeks=idx)).date())
    return str((pd.Timestamp("1970-01-01") + pd.Timedelta(days=idx)).date())


def missing_periods(dates: pd.Series, freq: str, weekmask: str | None = None) -> list[str]:
    """Labels of the periods between the first and last date that have no row.
    With a weekmask (business days, or days a shop is open) only those days count."""
    if dates.empty or freq not in ("daily", "weekly", "monthly", "quarterly", "annual", "business_daily"):
        return []
    dates = pd.Series(pd.to_datetime(dates)).sort_values()
    if freq == "business_daily" or weekmask:
        have = set(dates.dt.normalize())
        days = open_days(dates.iloc[0], dates.iloc[-1], weekmask or "Mon Tue Wed Thu Fri")
        return [str(d.date()) for d in days if d not in have]
    idx = set(_period_index(dates, freq))
    return [_label(i, freq) for i in range(min(idx), max(idx) + 1) if i not in idx]


def table_coverage(con, table: str, prof: dict) -> dict:
    date_col = prof.get("main_date_column")
    out = {"table": table, "date_column": date_col, "frequency": None, "missing_periods": [],
           "missing_period_count": 0, "columns": {}}
    if not date_col:
        return out
    d = prof["columns_profile"][date_col]["date"]
    out["frequency"] = d["frequency"]
    dates = pd.Series([r[0] for r in con.execute(
        f"SELECT DISTINCT {q(date_col)} FROM {q(table)} WHERE {q(date_col)} IS NOT NULL").fetchall()])
    gaps = missing_periods(dates, d["frequency"], d.get("weekmask"))
    out["missing_period_count"] = len(gaps)
    out["missing_periods"] = gaps[:MAX_LISTED * 4]

    types = {r[0]: r[1] for r in con.execute(f"DESCRIBE {q(table)}").fetchall()}
    dc = q(date_col)
    for col, typ in types.items():
        if col in (date_col, "_row") or typ == "VARCHAR":
            continue
        c = q(col)
        first, last, n, internal, listed = con.execute(f"""
            WITH b AS (SELECT MIN({dc}) AS f, MAX({dc}) AS l FROM {q(table)} WHERE {c} IS NOT NULL)
            SELECT b.f, b.l, COUNT(t.{c}),
                   COUNT(*) FILTER (WHERE t.{c} IS NULL AND t.{dc} BETWEEN b.f AND b.l),
                   list(t.{dc} ORDER BY t.{dc}) FILTER (WHERE t.{c} IS NULL AND t.{dc} BETWEEN b.f AND b.l)
            FROM {q(table)} t, b GROUP BY b.f, b.l""").fetchall()[0] if _has_values(con, table, col) \
            else (None, None, 0, 0, [])
        out["columns"][col] = {
            "first": str(first) if first else None, "last": str(last) if last else None, "values": n,
            "internal_missing": internal,
            "internal_missing_dates": [str(x) for x in (listed or [])[:MAX_LISTED]],
        }
    return out


def _has_values(con, table, col) -> bool:
    return con.execute(f"SELECT COUNT({q(col)}) FROM {q(table)}").fetchall()[0][0] > 0
