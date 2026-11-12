"""Typed views: the raw all-text tables, with each column cast to the type
profiling inferred. Validation rules run on these views, so a rule like
"amount >= 0" compares numbers, not text.

  raw_payments  (all VARCHAR, exactly as loaded)
  typed_payments (VIEW: numbers as DOUBLE, dates as DATE, text unchanged, plus _row)

A value that cannot be cast (a footer note in a price column) becomes NULL in
the view. It is never silently dropped from the count: the generic check
'values_fit_type' reports it, with its row number.

_row is the 1-based row number in the raw table, so every failing row a check
reports can be found in the original file.
"""

from __future__ import annotations

import duckdb

from dqp.profiling.profiler import _date_expr, _number_expr, q


def typed_name(raw_table: str) -> str:
    return "typed_" + raw_table.removeprefix("raw_")


def cast_expr(column: str, prof: dict) -> str:
    kind = prof["inferred_type"]
    if kind == "mixed":
        kind = prof.get("mixed_dominant_type", "text")
    v = f"trim({q(column)})"
    if kind == "numeric":
        return _number_expr(v, decimal_comma=False)
    if kind == "numeric_decimal_comma":
        return _number_expr(v, decimal_comma=True)
    if kind == "date":
        return _date_expr(v, prof.get("date", {}).get("slash_month_first", False))
    if kind == "boolean":
        return f"CASE WHEN lower({v}) IN ('true','yes','y') THEN TRUE WHEN lower({v}) IN ('false','no','n') THEN FALSE END"
    return q(column)


def create_typed_view(con: duckdb.DuckDBPyConnection, raw_table: str, table_profile: dict) -> str:
    view = typed_name(raw_table)
    cols = ", ".join(f"{cast_expr(c, p)} AS {q(c)}"
                     for c, p in table_profile["columns_profile"].items())
    con.execute(f"CREATE OR REPLACE VIEW {q(view)} AS SELECT rowid + 1 AS _row, {cols} FROM {q(raw_table)}")
    return view
