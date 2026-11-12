"""Google Sheets connector.

A sheet shared as "Anyone with the link can view" can be exported without logging in:
    https://docs.google.com/spreadsheets/d/<id>/export?format=xlsx
A sheet "published to the web" has its own link:
    https://docs.google.com/spreadsheets/d/e/<id>/pub?output=xlsx

The whole workbook is downloaded on every run (a sheet is live data; the SHA-256 of
each snapshot goes into the run log, so a change shows up as a new fingerprint) and
then read by the Excel connector, so tabs, title lines, multi-row headers and
missing-value markers are handled the same way.

Private sheets would need a Google login (OAuth or a service account); that is
deliberately not supported, so no credentials are ever needed or stored.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import duckdb

from dqp.config import ExcelSource, GSheetSource
from dqp.connectors.base import LoadResult
from dqp.connectors.excel_file import load_excel


class GSheetError(ValueError):
    pass


def export_url(url: str) -> tuple[str, str]:
    """(download link for the whole workbook as .xlsx, a short id for the cache file name)."""
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/e/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/e/{m.group(1)}/pub?output=xlsx", m.group(1)[:24]
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if not m:
        raise GSheetError(f"not a Google Sheets link: {url}")
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx", m.group(1)[:24]


def fetch(url: str, cache_dir: Path) -> tuple[Path, list[str]]:
    """Download the workbook; if the download fails but an earlier copy exists, use that
    copy and say so in the notes (the run log), instead of failing the run."""
    link, sid = export_url(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"gsheet_{sid}.xlsx"
    req = urllib.request.Request(link, headers={"User-Agent": "dq-pipeline"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if target.exists():
            when = datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="minutes")
            return target, [f"Google Sheets could not be reached ({exc}); used the copy downloaded {when}"]
        raise GSheetError(f"could not download the Google Sheet {url}: {exc}") from None
    if not data.startswith(b"PK"):
        raise GSheetError(
            f"{url} did not return a spreadsheet (probably a Google login page): the sheet is not public. "
            f"In Google Sheets choose Share > General access > 'Anyone with the link' (Viewer), "
            f"or File > Share > Publish to web, and use that link")
    target.write_bytes(data)
    return target, [f"downloaded from Google Sheets ({len(data):,} bytes)"]


def load_gsheet(source: GSheetSource, con: duckdb.DuckDBPyConnection) -> list[LoadResult]:
    path, notes = fetch(source.url, Path(source.cache_dir))
    as_excel = ExcelSource(type="excel", name=source.name, path=path, sheets=source.sheets,
                           header_rows=source.header_rows, na_values=source.na_values)
    results = load_excel(as_excel, con)
    for r in results:
        r.source_type, r.location = "gsheet", source.url
        r.notes = notes + r.notes
    return results
