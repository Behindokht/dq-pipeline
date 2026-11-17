"""CSV / TSV connector. DuckDB reads the file directly, which stays fast on
files with millions of rows.

A line with fewer fields than the header (a "Source: ..." footer, a note) is
loaded as a row with empty cells rather than stopping the load: profiling and
validation then flag it, and cleaning can move it to rejected_<table>.
A file that cannot be read gets an error that says what to change."""

from __future__ import annotations

import duckdb

from dqp.config import CsvSource
from dqp.connectors.base import LoadResult, describe_table, ensure_file, file_sha256


class CsvLoadError(ValueError):
    pass


def _explain(msg: str, path, source: CsvSource) -> str:
    first = msg.splitlines()[0]
    if "Invalid unicode" in msg or "not utf-8 encoded" in msg:
        hint = (f"the file is not {source.encoding}; set `encoding: latin-1` (common for Excel exports) "
                f"or `utf-16` on this source in the config")
    elif "sniffing" in msg or "Expected Number of Columns" in msg or "Too many columns" in msg.lower():
        hint = (f"some lines have more fields than the header. Check the delimiter (now {source.delimiter!r}), "
                f"quoting, or set `skip_rows` to skip lines above the header")
    else:
        hint = "check delimiter, encoding and skip_rows for this source"
    return f"{path}: could not read CSV ({first}). Hint: {hint}."


def load_csv(source: CsvSource, con: duckdb.DuckDBPyConnection) -> list[LoadResult]:
    path, notes = ensure_file(source)
    table = f"raw_{source.name}"
    try:
        con.execute(
            f"""
            CREATE OR REPLACE TABLE "{table}" AS
            SELECT * FROM read_csv(
                ?, delim = ?, header = ?, encoding = ?, skip = ?,
                all_varchar = true, auto_detect = true, null_padding = true, strict_mode = false
            )
            """,
            [str(path), source.delimiter, source.header, source.encoding, source.skip_rows],
        )
    except duckdb.Error as exc:
        raise CsvLoadError(_explain(str(exc), path, source)) from None
    rows, names = describe_table(con, table)
    return [
        LoadResult(
            table=table,
            source_name=source.name,
            source_type="csv",
            location=str(path),
            sheet=None,
            rows=rows,
            columns=len(names),
            column_names=names,
            sha256=file_sha256(path),
            notes=notes,
        )
    ]
