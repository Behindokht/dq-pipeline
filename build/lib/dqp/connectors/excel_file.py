"""Excel connector (.xlsx and legacy .xls).

Real-world workbooks are rarely a clean grid. This connector handles:
  - title / copyright lines above the data       -> header_rows points below them
  - headers spread over several rows             -> header_rows: [5, 6, 7]
  - merged header cells ("All REITs" over 6 cols) -> carried to the right
  - empty sheets, empty rows and empty columns    -> skipped, and logged
  - placeholder values such as "…"                -> na_values, become NULL

Each sheet becomes its own raw table: raw_<source name>__<sheet name>.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from dqp.config import ExcelSource, slugify
from dqp.connectors.base import (
    LoadResult,
    describe_table,
    file_sha256,
    ensure_file,
    unique_column_names,
    write_frame_as_text,
)


def build_header(grid: pd.DataFrame, header_rows: list[int]) -> list[str]:
    """Join several header rows into one name per column.

    A merged cell in Excel only stores its value in the first cell, so in every
    header row except the last we carry a label to the right until the next label:
        row 5:  All REITs  .        .      .       ->  All REITs  All REITs  All REITs ...
        row 6:  Total      .        Price  .       ->  Total      Total      Price ...
        row 7:  Return     Index    Return Index
        name:   "All REITs Total Return", "All REITs Total Index", "All REITs Price Return", ...
    """
    parts = []
    for pos, r in enumerate(header_rows):
        row = grid.iloc[r].astype("string").str.strip().replace("", pd.NA)
        if pos < len(header_rows) - 1:
            row = row.ffill()
        parts.append(row)
    stacked = pd.concat(parts, axis=1)
    names = []
    for _, cells in stacked.iterrows():
        words = []
        for c in cells.dropna():
            if not words or words[-1] != c:  # avoid "Date Date" when a label repeats
                words.append(str(c))
        names.append(" ".join(words))
    return names


def _engine(choice: str) -> str | None:
    """calamine (Rust) reads large workbooks many times faster than openpyxl."""
    if choice == "auto":
        try:
            import python_calamine  # noqa: F401
            return "calamine"
        except ImportError:
            return None
    return None if choice == "openpyxl" else choice


def load_excel(source: ExcelSource, con: duckdb.DuckDBPyConnection) -> list[LoadResult]:
    path, dl_notes = ensure_file(source)
    engine = _engine(source.engine)
    book = pd.ExcelFile(path, engine=engine)  # None: chosen from file content, not the extension
    available = book.sheet_names
    specs = source.sheet_specs(available)

    missing = [s.sheet for s in specs if s.sheet not in available]
    if missing:
        raise ValueError(f"{path}: sheets not found: {missing}; available: {available}")

    digest = file_sha256(path)
    na = ["…", "..", "n/a", "N/A", "#N/A"] + source.na_values
    results: list[LoadResult] = []
    used: dict[str, str] = {}

    for spec in specs:
        header_rows = spec.header_rows if spec.header_rows is not None else source.header_rows
        header_rows = [header_rows] if isinstance(header_rows, int) else header_rows
        base = dict(source_name=source.name, source_type="excel", location=str(path),
                    sheet=spec.sheet, sha256=digest)

        grid = pd.read_excel(book, sheet_name=spec.sheet, header=None, dtype=object, na_values=na,
                             keep_default_na=True)
        grid = grid.map(lambda v: None if isinstance(v, str) and not v.strip() else v)

        if grid.dropna(how="all").empty:
            results.append(LoadResult(table=None, rows=0, columns=0, column_names=[],
                                      status="skipped", notes=["sheet is empty"], **base))
            continue
        if header_rows[-1] >= len(grid):
            raise ValueError(f"{path} [{spec.sheet}]: header row {header_rows[-1]} is beyond "
                             f"the last row ({len(grid) - 1})")

        notes = list(dl_notes)
        if header_rows[0] > 0:
            notes.append(f"skipped {header_rows[0]} title line(s) above the header")
        if len(header_rows) > 1:
            notes.append(f"column names built from header rows {header_rows}")

        names = build_header(grid, header_rows)
        body = grid.iloc[header_rows[-1] + 1:]

        # A spacer column has no data and no label of its own in the last header row
        # (it may have inherited a carried-over group label, which doesn't count).
        own_label = grid.iloc[header_rows[-1]].notna()
        empty_cols = [i for i in range(body.shape[1])
                      if body.iloc[:, i].isna().all() and (not names[i] or not own_label.iloc[i])]
        if empty_cols:
            keep = [i for i in range(body.shape[1]) if i not in empty_cols]
            body, names = body.iloc[:, keep], [names[i] for i in keep]
            notes.append(f"dropped {len(empty_cols)} empty unnamed column(s)")

        before = len(body)
        body = body.dropna(how="all")
        if len(body) < before:
            notes.append(f"dropped {before - len(body)} fully empty row(s)")

        names, rename_notes = unique_column_names(names)
        notes.extend(rename_notes)
        body = body.copy()
        body.columns = names

        slug = slugify(spec.sheet)
        if slug in used:
            raise ValueError(f"{path}: sheets {used[slug]!r} and {spec.sheet!r} map to the same table name")
        used[slug] = spec.sheet

        table = f"raw_{source.name}__{slug}"
        write_frame_as_text(con, table, body)
        rows, cols = describe_table(con, table)
        results.append(LoadResult(table=table, rows=rows, columns=len(cols), column_names=cols,
                                  notes=notes, **base))
    return results
