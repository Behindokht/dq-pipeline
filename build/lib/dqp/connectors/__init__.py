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

CONNECTORS: dict[str, Callable[..., list[LoadResult]]] = {
    "csv": load_csv,
    "excel": load_excel,
    "gsheet": load_gsheet,
}


def load_source(source, con: duckdb.DuckDBPyConnection) -> list[LoadResult]:
    try:
        loader = CONNECTORS[source.type]
    except KeyError:
        raise ValueError(f"no connector for source type {source.type!r}") from None
    return loader(source, con)


__all__ = ["CONNECTORS", "LoadResult", "load_source"]
