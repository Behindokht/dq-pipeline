"""Layer 2: automatic profiling.

Profiling describes the data; it does not judge it (that is validation, layer 3).
It works directly on the raw all-text tables, so it sees the data exactly as it
arrived, before any cleaning.

For every column it works out:
  - what type the values look like (numeric, date, boolean, text), and which values
    do NOT fit that type: this is how a footer note at the bottom of a price column,
    or a number Excel turned into a 1900 date, gets caught
  - missing values, distinct values, stray whitespace
  - numeric: min / max / mean / quantiles and outliers (IQR rule)
  - dates: format, frequency (daily ... annual), coverage, duplicates and gaps

For every table: rows, columns, exact duplicate rows, empty and constant columns,
candidate key columns and the main date column.

Heavy counting is done in DuckDB SQL, so the same code works on large tables.
Every number lands in a nested dict whose path is its fact ID, e.g.
  tables.raw_artprice__data.columns.column_1.nonconforming
The AI layer (layer 5) is only allowed to quote numbers through these IDs.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pandas as pd

# Date formats recognised in text values. Order matters only for reporting.
DATE_FORMATS: dict[str, str] = {
    "iso_datetime": r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$",
    "iso_date": r"^\d{4}-\d{2}-\d{2}$",
    "year_month": r"^\d{4}-(0[1-9]|1[0-2])$",
    "year_quarter": r"^\d{4}-?Q[1-4]$",
    "year_m_month": r"^\d{4}M(0[1-9]|1[0-2])$",        # World Bank style: 1960M01
    # day-month-year or month-day-year with / . or - : 31/01/2024, 31.01.2024, 31-01-2024 (Dutch), 01/31/2024
    "slash_date": r"^\d{1,2}(/\d{1,2}/|\.\d{1,2}\.|-\d{1,2}-)\d{4}$",   # same separator twice
}
DECIMAL_COMMA = r"^-?\d{1,3}(\.\d{3})*,\d+$|^-?\d+,\d+$"   # 1,50  or  1.234,50
BOOLEAN_VALUES = ("true", "false", "yes", "no", "y", "n")


@dataclass
class ProfileSettings:
    type_threshold: float = 0.95     # share of values that must fit a type to call the column that type
    outlier_iqr_k: float = 3.0       # outlier if below Q1 - k*IQR or above Q3 + k*IQR
    max_examples: int = 3            # example values kept per finding
    top_values: int = 5              # most frequent values kept for text columns
    row_expr: str = "rowid + 1"      # how example rows are numbered; "_row" for cleaned tables,
                                     # so row numbers always point at the line in the source file


def q(name: str) -> str:
    """Quote an identifier for SQL (column names here contain spaces, $, commas ...)."""
    return '"' + name.replace('"', '""') + '"'


def lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ---------------------------------------------------------------- expressions

def _matches_date(v: str) -> str:
    return "(" + " OR ".join(f"regexp_full_match({v}, {lit(p)})" for p in DATE_FORMATS.values()) + ")"


def _date_expr(v: str, slash_month_first: bool) -> str:
    """SQL that turns a text value in any recognised format into a DATE (NULL if not a date)."""
    slash_fmt = "%m/%d/%Y" if slash_month_first else "%d/%m/%Y"
    f = DATE_FORMATS
    return f"""CASE
        WHEN regexp_full_match({v}, {lit(f['iso_datetime'])}) THEN TRY_CAST(substr({v}, 1, 10) AS DATE)
        WHEN regexp_full_match({v}, {lit(f['iso_date'])}) THEN TRY_CAST({v} AS DATE)
        WHEN regexp_full_match({v}, {lit(f['year_month'])}) THEN TRY_CAST({v} || '-01' AS DATE)
        WHEN regexp_full_match({v}, {lit(f['year_quarter'])})
             THEN make_date(CAST(substr({v}, 1, 4) AS INTEGER), (CAST(right({v}, 1) AS INTEGER) - 1) * 3 + 1, 1)
        WHEN regexp_full_match({v}, {lit(f['year_m_month'])})
             THEN make_date(CAST(substr({v}, 1, 4) AS INTEGER), CAST(substr({v}, 6, 2) AS INTEGER), 1)
        WHEN regexp_full_match({v}, {lit(f['slash_date'])})
             THEN CAST(try_strptime(replace(replace({v}, '.', '/'), '-', '/'), {lit(slash_fmt)}) AS DATE)
    END"""


def _number_expr(v: str, decimal_comma: bool) -> str:
    if decimal_comma:  # 1.234,50 -> 1234.50 ; plain values like 7 still parse
        return f"COALESCE(TRY_CAST({v} AS DOUBLE), TRY_CAST(replace(replace({v}, '.', ''), ',', '.') AS DOUBLE))"
    return f"TRY_CAST({v} AS DOUBLE)"


def _fits(kind: str, v: str, slash_month_first: bool = False) -> str:
    """SQL condition: does trimmed value v fit the inferred type?"""
    if kind == "numeric":
        return f"TRY_CAST({v} AS DOUBLE) IS NOT NULL"
    if kind == "numeric_decimal_comma":
        return f"(TRY_CAST({v} AS DOUBLE) IS NOT NULL OR regexp_full_match({v}, {lit(DECIMAL_COMMA)}))"
    if kind == "date":
        return f"({_date_expr(v, slash_month_first)}) IS NOT NULL"
    if kind == "boolean":
        return f"lower({v}) IN ({', '.join(lit(b) for b in BOOLEAN_VALUES)})"
    return "TRUE"


# ---------------------------------------------------------------- columns

def _infer_type(c: dict, threshold: float) -> tuple[str, float, str | None]:
    """Pick the type most values fit. Returns (type, share that fits, dominant date format)."""
    n = c["non_null"]
    if n == 0:
        return "empty", 0.0, None
    date_counts = {k: c[f"fmt_{k}"] for k in DATE_FORMATS}
    dominant_fmt = max(date_counts, key=date_counts.get)
    shares = {
        "date": sum(date_counts.values()) / n,
        "numeric": c["numeric"] / n,
        "numeric_decimal_comma": (c["numeric"] + c["decimal_comma"]) / n if c["decimal_comma"] else 0.0,
        "boolean": c["boolean"] / n,
    }
    # a plain number column is 'numeric', not 'numeric_decimal_comma'
    if shares["numeric"] >= threshold:
        shares["numeric_decimal_comma"] = 0.0
    best = max(shares, key=shares.get)
    if shares[best] >= threshold:
        return best, shares[best], dominant_fmt if best == "date" else None
    if shares[best] >= 0.5:
        return "mixed", shares[best], dominant_fmt if best == "date" else None
    return "text", 1.0, None


def profile_column(con, table: str, column: str, n_rows: int, s: ProfileSettings) -> dict:
    v = f"trim({q(column)})"
    fmt_counts = ",\n".join(
        f"COUNT(*) FILTER (WHERE regexp_full_match({v}, {lit(p)})) AS fmt_{k}" for k, p in DATE_FORMATS.items())
    base = con.execute(f"""
        SELECT COUNT({q(column)}) AS non_null,
               COUNT(DISTINCT {q(column)}) AS distinct_values,
               COUNT(*) FILTER (WHERE {q(column)} <> {v}) AS whitespace,
               COUNT(*) FILTER (WHERE TRY_CAST({v} AS DOUBLE) IS NOT NULL) AS numeric,
               COUNT(*) FILTER (WHERE TRY_CAST({v} AS DOUBLE) IS NULL
                                  AND regexp_full_match({v}, {lit(DECIMAL_COMMA)})) AS decimal_comma,
               COUNT(*) FILTER (WHERE lower({v}) IN ({', '.join(lit(b) for b in BOOLEAN_VALUES)})) AS boolean,
               COUNT(*) FILTER (WHERE regexp_full_match({v}, {lit(DATE_FORMATS['slash_date'])})
                                  AND TRY_CAST(regexp_extract({v}, '^(\\d+)', 1) AS INTEGER) > 12) AS slash_day_first,
               COUNT(*) FILTER (WHERE regexp_full_match({v}, {lit(DATE_FORMATS['slash_date'])})
                                  AND TRY_CAST(regexp_extract({v}, '^\\d+[/.-](\\d+)', 1) AS INTEGER) > 12) AS slash_month_first,
               {fmt_counts}
        FROM {q(table)}
    """).fetchdf().iloc[0].to_dict()
    c = {k: int(val) for k, val in base.items()}

    kind, share, date_fmt = _infer_type(c, s.type_threshold)
    out: dict = {
        "inferred_type": kind,
        "type_share": round(share, 4),
        "rows": n_rows,
        "non_null": c["non_null"],
        "nulls": n_rows - c["non_null"],
        "null_share": round((n_rows - c["non_null"]) / n_rows, 4) if n_rows else 0.0,
        "distinct_values": c["distinct_values"],
        "whitespace_values": c["whitespace"],
        "nonconforming": 0,
        "nonconforming_examples": [],
    }
    slash_month_first = c["slash_month_first"] > 0 and c["slash_day_first"] == 0

    # Values that do not fit the column's type. For 'mixed' we report against the dominant type.
    check_kind = kind
    if kind == "mixed":
        check_kind = max(("date", "numeric", "boolean"),
                         key=lambda k: {"date": sum(c[f"fmt_{f}"] for f in DATE_FORMATS),
                                        "numeric": c["numeric"], "boolean": c["boolean"]}[k])
        out["mixed_dominant_type"] = check_kind
    if check_kind in ("numeric", "numeric_decimal_comma", "date", "boolean"):
        cond = f"{q(column)} IS NOT NULL AND NOT ({_fits(check_kind, v, slash_month_first)})"
        out["nonconforming"] = con.execute(f"SELECT COUNT(*) FROM {q(table)} WHERE {cond}").fetchall()[0][0]
        out["nonconforming_examples"] = [
            {"row": int(r), "value": val} for r, val in con.execute(
                f"SELECT {s.row_expr} AS r, {q(column)} FROM {q(table)} WHERE {cond} ORDER BY r LIMIT {s.max_examples}"
            ).fetchall()]

    if kind in ("numeric", "numeric_decimal_comma"):
        out["numeric"] = _numeric_stats(con, table, column, v, kind == "numeric_decimal_comma", s)
    elif kind == "date":
        used = {k: c[f"fmt_{k}"] for k in DATE_FORMATS if c[f"fmt_{k}"]}
        out["date"] = _date_stats(con, table, column, v, slash_month_first, s)
        out["date"]["format"] = date_fmt
        # timestamps with a real time of day must stay timestamps when typed: casting
        # them to DATE would silently merge lines that differ only by their time
        out["date"]["has_time"] = bool(con.execute(
            f"SELECT COUNT(*) FROM {q(table)} WHERE regexp_full_match({v}, {lit(DATE_FORMATS['iso_datetime'])}) "
            f"AND NOT regexp_matches({v}, '[ T]00:00(:00(\\.0+)?)?$')").fetchall()[0][0])
        out["date"]["slash_month_first"] = slash_month_first
        out["date"]["formats_used"] = used
        if "slash_date" in used and c["slash_day_first"] == 0 and c["slash_month_first"] == 0:
            out["date"]["ambiguous_day_month"] = True   # every value has day and month <= 12
    elif kind in ("text", "boolean", "mixed"):
        out["top_values"] = [
            {"value": val, "count": int(n)} for val, n in con.execute(
                f"SELECT {q(column)}, COUNT(*) n FROM {q(table)} WHERE {q(column)} IS NOT NULL "
                f"GROUP BY 1 ORDER BY n DESC, 1 LIMIT {s.top_values}").fetchall()]
        lengths = con.execute(f"SELECT MIN(length({q(column)})), MAX(length({q(column)})) FROM {q(table)}").fetchall()[0]
        out["length_min"], out["length_max"] = lengths
    return out


def _numeric_stats(con, table, column, v, decimal_comma, s) -> dict:
    x = _number_expr(v, decimal_comma)
    src = f"(SELECT {s.row_expr} AS row, {x} AS x FROM {q(table)}) t WHERE x IS NOT NULL AND isfinite(x)"
    r = con.execute(f"""
        SELECT COUNT(*), MIN(x), MAX(x), AVG(x), STDDEV_SAMP(x),
               quantile_cont(x, [0.01, 0.25, 0.5, 0.75, 0.99]),
               COUNT(*) FILTER (WHERE x = 0), COUNT(*) FILTER (WHERE x < 0)
        FROM {src}""").fetchall()[0]
    n, mn, mx, mean, std, qs, zeros, negatives = r
    stats = {"count": n, "min": mn, "max": mx, "mean": mean, "std": std,
             "p01": None, "p25": None, "median": None, "p75": None, "p99": None,
             "zeros": zeros, "negatives": negatives, "outliers": 0, "outlier_examples": []}
    if not n:
        return stats
    stats.update(dict(zip(["p01", "p25", "median", "p75", "p99"], qs)))
    iqr = stats["p75"] - stats["p25"]
    if iqr > 0:
        lo, hi = stats["p25"] - s.outlier_iqr_k * iqr, stats["p75"] + s.outlier_iqr_k * iqr
        stats["outlier_bounds"] = [lo, hi]
        stats["outliers"] = con.execute(f"SELECT COUNT(*) FROM {src} AND (x < {lo} OR x > {hi})").fetchall()[0][0]
        stats["outlier_examples"] = [
            {"row": int(row), "value": val} for row, val in con.execute(
                f"SELECT row, x FROM {src} AND (x < {lo} OR x > {hi}) "
                f"ORDER BY abs(x - {stats['median']}) DESC LIMIT {s.max_examples}").fetchall()]
    return {k: (round(val, 6) if isinstance(val, float) else val) for k, val in stats.items()}


FREQUENCIES = [  # (name, median gap range in days)
    ("daily", 0.5, 1.5), ("weekly", 6, 8), ("monthly", 27, 32),
    ("quarterly", 85, 95), ("annual", 360, 370),
]


def _period_index(d: pd.Series, freq: str) -> pd.Series:
    if freq == "monthly":
        return d.dt.year * 12 + d.dt.month
    if freq == "quarterly":
        return d.dt.year * 4 + (d.dt.month - 1) // 3
    if freq == "annual":
        return d.dt.year
    if freq == "weekly":
        return (d - pd.Timestamp("1970-01-05")).dt.days // 7
    return (d - pd.Timestamp("1970-01-01")).dt.days   # daily


def _date_stats(con, table, column, v, slash_month_first, s) -> dict:
    expr = _date_expr(v, slash_month_first)
    total, distinct = con.execute(
        f"SELECT COUNT(d), COUNT(DISTINCT d) FROM (SELECT {expr} AS d FROM {q(table)})").fetchall()[0]
    dates = pd.Series(pd.to_datetime([r[0] for r in con.execute(
        f"SELECT DISTINCT {expr} AS d FROM {q(table)} WHERE d IS NOT NULL ORDER BY d").fetchall()]))

    out = {"min": None, "max": None, "distinct_dates": distinct, "duplicate_dates": total - distinct,
           "duplicate_examples": [], "frequency": "unknown", "expected_periods": None,
           "missing_periods": None, "largest_gap": None}
    if out["duplicate_dates"]:
        out["duplicate_examples"] = [
            {"date": str(d), "count": int(n)} for d, n in con.execute(
                f"SELECT d, COUNT(*) n FROM (SELECT {expr} AS d FROM {q(table)}) WHERE d IS NOT NULL "
                f"GROUP BY d HAVING n > 1 ORDER BY n DESC, d LIMIT {s.max_examples}").fetchall()]
    if dates.empty:
        return out
    out["min"], out["max"] = str(dates.iloc[0].date()), str(dates.iloc[-1].date())
    if len(dates) < 3:
        return out

    median_gap = dates.diff().dt.days.median()
    freq = next((name for name, lo, hi in FREQUENCIES if lo <= median_gap <= hi), "irregular")
    if freq == "daily" and not (dates.dt.day_of_week >= 5).any():
        freq = "business_daily"
    out["frequency"] = freq
    out["median_gap_days"] = float(median_gap)
    if freq == "irregular":
        return out

    if freq == "business_daily":
        expected = int(len(pd.bdate_range(dates.iloc[0], dates.iloc[-1])))
        steps = pd.Series([len(pd.bdate_range(a, b)) - 1 for a, b in zip(dates[:-1], dates[1:])])
        out["note"] = "public holidays count as missing business days"
    else:
        idx = _period_index(dates, freq).drop_duplicates().reset_index(drop=True)
        expected = int(idx.iloc[-1] - idx.iloc[0] + 1)
        steps = idx.diff().iloc[1:].reset_index(drop=True)

    distinct_periods = len(dates) if freq == "business_daily" else len(_period_index(dates, freq).unique())
    out["expected_periods"] = expected
    out["missing_periods"] = max(expected - distinct_periods, 0)
    if len(steps) and steps.max() > 1:
        i = int(steps.idxmax())
        out["largest_gap"] = {"missing_periods": int(steps.max() - 1),
                              "after": str(dates.iloc[i].date()), "before": str(dates.iloc[i + 1].date())}
    return out


# ---------------------------------------------------------------- tables

def profile_table(con: duckdb.DuckDBPyConnection, table: str, s: ProfileSettings | None = None) -> dict:
    s = s or ProfileSettings()
    # _row is lineage (row number in the source file), not data
    columns = [r[0] for r in con.execute(f"DESCRIBE {q(table)}").fetchall() if r[0] != "_row"]
    n_rows = con.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchall()[0][0]

    cols = {c: profile_column(con, table, c, n_rows, s) for c in columns}

    col_list = ", ".join(q(c) for c in columns)
    dup = con.execute(f"""SELECT COALESCE(SUM(n - 1), 0), COUNT(*) FROM
        (SELECT {col_list}, COUNT(*) n FROM {q(table)} GROUP BY ALL HAVING COUNT(*) > 1)""").fetchall()[0]

    date_cols = [c for c, p in cols.items() if p["inferred_type"] == "date"]
    main_date = max(date_cols, key=lambda c: (cols[c]["non_null"], -columns.index(c)), default=None)

    return {
        "row_expr": s.row_expr,
        "rows": n_rows,
        "columns": len(columns),
        "duplicate_rows": int(dup[0]),
        "duplicate_row_groups": int(dup[1]),
        "empty_columns": [c for c, p in cols.items() if p["inferred_type"] == "empty"],
        "constant_columns": [c for c, p in cols.items() if p["distinct_values"] == 1],
        "candidate_keys": [c for c, p in cols.items() if n_rows and p["nulls"] == 0 and p["distinct_values"] == n_rows],
        "main_date_column": main_date,
        "type_counts": pd.Series([p["inferred_type"] for p in cols.values()]).value_counts().to_dict(),
        "columns_profile": cols,
    }
