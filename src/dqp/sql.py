"""Identifiers and literals in generated SQL. Column names come from spreadsheets and
can be anything: a keyword (`order`), a quote, a semicolon, nothing at all."""

from __future__ import annotations

import re
from functools import lru_cache

import duckdb


def ident(name: str) -> str:
    """Always quoted: for SQL the pipeline runs itself."""
    return '"' + str(name).replace('"', '""') + '"'


@lru_cache(maxsize=1)
def _keywords() -> frozenset[str]:
    con = duckdb.connect()
    try:
        return frozenset(r[0].lower() for r in con.execute("SELECT keyword_name FROM duckdb_keywords()").fetchall())
    finally:
        con.close()


def ident_if_needed(name: str) -> str:
    """Bare when a plain lower-case name that is not a keyword (so configs written for
    people stay readable: `amount >= 0`), quoted otherwise (`"order" >= 0`)."""
    name = str(name)
    if re.fullmatch(r"[a-z_][a-z0-9_]*", name) and name not in _keywords():
        return name
    return ident(name)


def text(value: str) -> str:
    """A string literal."""
    return "'" + str(value).replace("'", "''") + "'"
