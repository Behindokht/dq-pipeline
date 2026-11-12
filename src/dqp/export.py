"""Export the cleaned data of a finished run.

    dqp export outputs/my_sales                     CSV, every staging table and mart
    dqp export outputs/my_sales --format xlsx       one workbook, a sheet per table
    dqp export outputs/my_sales --format parquet --tables marts

Writes to outputs/<dataset>/export/ (or --out) plus export_manifest.json, which
records the run, its validation result and the rows of every file.

Data that failed validation after cleaning is not exported unless --allow-failed
is given: the same rule as the summary, failed data does not move on silently.
The _row column (line number in the source file) is dropped unless
--keep-row-numbers is given.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

from dqp.profiling.profiler import q

FORMATS = ("csv", "xlsx", "parquet")
EXCEL_MAX_ROWS = 1_048_575          # Excel's limit minus the header row


class ExportError(ValueError):
    pass


def _tables(con, which: str, include_rejected: bool) -> list[str]:
    names = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()]
    prefixes = {"staging": ("stg_",), "marts": ("mart_",), "all": ("stg_", "mart_")}[which]
    if include_rejected:
        prefixes = prefixes + ("rejected_",)
    return [n for n in names if n.startswith(prefixes)]


def export_run(run_dir: str | Path, fmt: str = "csv", tables: str = "all", out: str | Path | None = None,
               allow_failed: bool = False, keep_row_numbers: bool = False,
               include_rejected: bool = False) -> dict:
    run_dir = Path(run_dir)
    log_file = run_dir / "run_log.json"
    if not log_file.exists():
        raise ExportError(f"{run_dir}: no finished run here (run_log.json missing); run `dqp run <config>` first")
    log = json.loads(log_file.read_text(encoding="utf-8"))
    if log.get("status") != "success":
        raise ExportError(f"{run_dir}: the last run did not finish ({log.get('error', 'unknown error')})")
    status = log["validation"]["status"]
    if status == "failed" and not allow_failed:
        raise ExportError(f"{run_dir}: validation after cleaning failed, so the data is not exported. "
                          f"Open report.html to see why, or pass --allow-failed to export it anyway")
    if fmt not in FORMATS:
        raise ExportError(f"unknown format {fmt!r}; use one of {FORMATS}")

    db = run_dir / f"{log['dataset']}.duckdb"
    out = Path(out) if out else run_dir / "export"
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db), read_only=True)
    try:
        names = _tables(con, tables, include_rejected)
        if not names:
            raise ExportError(f"{run_dir}: no {tables} tables to export")
        files = []
        if fmt == "xlsx":
            files = _to_excel(con, names, out / f"{log['dataset']}.xlsx", keep_row_numbers)
        else:
            for t in names:
                cols = _columns(con, t, keep_row_numbers)
                target = out / f"{t}.{fmt}"
                opts = "(HEADER, DELIMITER ',')" if fmt == "csv" else "(FORMAT PARQUET)"
                con.execute(f"COPY (SELECT {cols} FROM {q(t)}) TO '{_sql_path(target)}' {opts}")
                rows = con.execute(f"SELECT COUNT(*) FROM {q(t)}").fetchall()[0][0]
                files.append({"table": t, "file": target.name, "rows": rows})
    finally:
        con.close()

    manifest = {"dataset": log["dataset"], "run_id": log["run_id"], "run_started_at": log["started_at"],
                "validation_after_cleaning": status, "exported_despite_failure": status == "failed",
                "format": fmt, "row_numbers_kept": keep_row_numbers, "files": files}
    (out / "export_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["out"] = str(out)
    return manifest


def _columns(con, table: str, keep_row_numbers: bool) -> str:
    cols = [r[0] for r in con.execute(f"DESCRIBE {q(table)}").fetchall()]
    if not keep_row_numbers:
        cols = [c for c in cols if c != "_row"]
    return ", ".join(q(c) for c in cols)


def _sql_path(p: Path) -> str:
    return str(p).replace("'", "''")


def _to_excel(con, names: list[str], target: Path, keep_row_numbers: bool) -> list[dict]:
    import pandas as pd

    files, used = [], set()
    too_big = [t for t in names if con.execute(f"SELECT COUNT(*) FROM {q(t)}").fetchall()[0][0] > EXCEL_MAX_ROWS]
    if too_big:
        raise ExportError(f"{', '.join(too_big)} has more rows than an Excel sheet can hold "
                          f"({EXCEL_MAX_ROWS + 1:,}); export as csv or parquet instead")
    with pd.ExcelWriter(target, engine="openpyxl") as xw:
        for t in names:
            df = con.execute(f"SELECT {_columns(con, t, keep_row_numbers)} FROM {q(t)}").df()
            sheet = re.sub(r"[\[\]:*?/\\]", "_", t)[:31]          # Excel sheet-name rules
            base, i = sheet, 2
            while sheet in used:
                sheet, i = f"{base[:28]}_{i}", i + 1
            used.add(sheet)
            df.to_excel(xw, sheet_name=sheet, index=False)
            files.append({"table": t, "file": f"{target.name} [{sheet}]", "rows": len(df)})
    return files
