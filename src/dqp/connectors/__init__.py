"""Connector registry.

Adding a new source type means: write one load_<type>(source, con) function,
add its config model in config.py, and register it here. No other layer changes.
"""

from __future__ import annotations

from typing import Callable

import duckdb

from dqp.connectors.base import LoadResult
from dqp.connectors.csv_file import load_csv
from dqp.connectors.excel_file import load_excel
from dqp.connectors.gsheet import load_gsheet
from dqp.sql import ident

CONNECTORS: dict[str, Callable[..., list[LoadResult]]] = {
    "csv": load_csv,
    "excel": load_excel,
    "gsheet": load_gsheet,
}


# Column names the pipeline uses itself: the row number (rowid, a DuckDB built-in),
# the lineage column of cleaned tables and the bookkeeping columns of rejected_ tables.
RESERVED = ("rowid", "_row", "_step", "_action", "_reason")


def load_source(source, con: duckdb.DuckDBPyConnection) -> list[LoadResult]:
    try:
        loader = CONNECTORS[source.type]
    except KeyError:
        raise ValueError(f"no connector for source type {source.type!r}") from None
    results = loader(source, con)
    for r in results:
        if r.table:
            _rename_reserved(con, r)
    return results


def _rename_reserved(con: duckdb.DuckDBPyConnection, r: LoadResult) -> None:
    """A source column with a name the pipeline uses itself is renamed, never dropped,
    and the load log says so. Names are compared without case, as SQL does."""
    names = [row[0] for row in con.execute(f"DESCRIBE {ident(r.table)}").fetchall()]
    taken = {n.lower() for n in names}
    for name in names:
        if name.lower() not in RESERVED:
            continue
        new, k = f"{name}_source", 2
        while new.lower() in taken:
            new, k = f"{name}_source_{k}", k + 1
        con.execute(f"ALTER TABLE {ident(r.table)} RENAME COLUMN {ident(name)} TO {ident(new)}")
        taken.add(new.lower())
        r.column_names = [new if c == name else c for c in r.column_names]
        r.notes.append(f"column {name!r} renamed to {new!r}: the pipeline uses {name!r} itself")


__all__ = ["CONNECTORS", "LoadResult", "load_source"]
