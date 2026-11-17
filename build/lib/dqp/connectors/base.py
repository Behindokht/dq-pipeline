"""What every connector returns, plus shared helpers.

The contract: a connector reads one source and writes one or more raw tables
into DuckDB. Raw tables store every value as text, exactly as it arrived.
Converting text to numbers or dates is a staging decision, made later, where
it can be checked and logged. Nothing is coerced silently at load time.

Anything a connector does to the shape of the data (dropping empty rows,
building column names from a multi-row header, skipping an empty sheet) is
written into `notes`, so it shows up in the run log.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import duckdb
import pandas as pd


@dataclass
class LoadResult:
    table: str | None     # DuckDB table that was written; None when skipped
    source_name: str      # name from the config
    source_type: str      # csv, excel, ...
    location: str         # file path or URL
    sheet: str | None     # sheet name for spreadsheet sources
    rows: int
    columns: int
    column_names: list[str]
    sha256: str | None    # fingerprint of the input file, to detect changed data
    status: Literal["loaded", "skipped"] = "loaded"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def require_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"input file not found: {path}")
    return path


def ensure_file(source) -> tuple[Path, list[str]]:
    """Return the source's file, downloading it first if it is missing and the
    config says where to get it. Returns notes for the run log."""
    path = Path(source.path)
    dl = getattr(source, "download", None)
    if path.is_file() or dl is None:
        return require_file(path), ([f"from {dl.url} (cached copy)"] if dl else [])
    import tempfile
    import urllib.request
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "download"
        req = urllib.request.Request(dl.url, headers={"User-Agent": "dq-pipeline"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp, open(target, "wb") as f:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    f.write(chunk)
        except OSError as exc:
            raise FileNotFoundError(f"{path} is missing and downloading {dl.url} failed: {exc}") from None
        digest = file_sha256(target)
        if dl.sha256 and digest != dl.sha256:
            raise ValueError(f"download from {dl.url} has SHA-256 {digest}, expected {dl.sha256}: "
                             f"the file changed at the source, check it before updating the config")
        if dl.extract:
            with zipfile.ZipFile(target) as z:
                if dl.extract not in z.namelist():
                    raise ValueError(f"{dl.url}: zip has no member {dl.extract!r}; members: {z.namelist()}")
                with z.open(dl.extract) as src, open(path, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        out.write(chunk)
        else:
            target.replace(path)
    note = f"downloaded from {dl.url}" + (", checksum verified" if dl.sha256 else " (no checksum configured)")
    return path, [note]


def unique_column_names(names: list[str]) -> tuple[list[str], list[str]]:
    """Blank names become column_<n>; repeated names get _2, _3 ... Returns the
    names and a note for every change, so renaming is never silent."""
    out, notes, seen = [], [], {}
    for i, raw in enumerate(names, start=1):
        name = " ".join(str(raw).split()) if raw is not None else ""
        if not name:
            name = f"column_{i}"
            notes.append(f"column {i} had no header; named {name!r}")
        if name in seen:
            seen[name] += 1
            new = f"{name}_{seen[name]}"
            notes.append(f"duplicate header {name!r} in column {i}; renamed to {new!r}")
            name = new
        seen.setdefault(name, 1)
        out.append(name)
    return out, notes


def quote_ident(name: str) -> str:
    """Quote a column or table name for SQL; names from spreadsheets can contain anything."""
    return '"' + str(name).replace('"', '""') + '"'


def write_frame_as_text(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    """Store a DataFrame as an all-text raw table. Empty cells become NULL."""
    text = df.astype("string").astype(object).where(df.notna(), None)
    text.columns = [str(c) for c in text.columns]
    con.register("_incoming", text)
    try:
        cols = ", ".join(f"CAST({quote_ident(c)} AS VARCHAR) AS {quote_ident(c)}" for c in text.columns)
        con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT {cols} FROM _incoming')
    finally:
        con.unregister("_incoming")


def describe_table(con: duckdb.DuckDBPyConnection, table: str) -> tuple[int, list[str]]:
    rows = con.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchall()[0][0]
    names = [r[0] for r in con.execute(f"DESCRIBE {quote_ident(table)}").fetchall()]
    return rows, names
