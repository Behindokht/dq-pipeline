"""Tests for `dqp export`: the cleaned data leaves the pipeline as files."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from dqp.cli import main
from dqp.pipeline import run

CSV = "month,price\n2024-01,10\n2024-02,11\n2024-02,11\n2024-03,12\nSource: bank,\n"


def finished_run(tmp_path: Path, rules=None) -> Path:
    data = tmp_path / "p.csv"
    data.write_text(CSV, encoding="utf-8")
    cfg = {"dataset": "prices", "output_dir": str(tmp_path / "out"),
           "sources": [{"name": "prices", "type": "csv", "path": str(data)}],
           "cleaning": {"prices": [{"drop_rows": {"where": "month IS NULL", "reason": "footer"}},
                                   {"drop_duplicates": {}}]},
           "marts": [{"name": "monthly", "sql": "SELECT month, price FROM stg_prices"}],
           "validation": {"rules": rules or []}, "summary": {"provider": "none"}}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return Path(run(p)["db_path"]).parent


def test_csv_export_of_cleaned_tables(tmp_path):
    out = finished_run(tmp_path)
    assert main(["export", str(out)]) == 0
    stg = pd.read_csv(out / "export" / "stg_prices.csv")
    assert list(stg.columns) == ["month", "price"]                  # no _row by default
    assert len(stg) == 3                                            # footer and duplicate removed
    assert (out / "export" / "mart_monthly.csv").exists()
    m = json.loads((out / "export" / "export_manifest.json").read_text())
    assert m["validation_after_cleaning"] == "passed" and {f["table"] for f in m["files"]} == {"stg_prices", "mart_monthly"}


def test_xlsx_export_one_sheet_per_table(tmp_path):
    out = finished_run(tmp_path)
    assert main(["export", str(out), "--format", "xlsx", "--keep-row-numbers"]) == 0
    book = pd.ExcelFile(out / "export" / "prices.xlsx")
    assert book.sheet_names == ["mart_monthly", "stg_prices"]
    assert "_row" in pd.read_excel(book, "stg_prices").columns


def test_parquet_export_marts_only_and_rejected(tmp_path):
    out = finished_run(tmp_path)
    assert main(["export", str(out), "--format", "parquet", "--tables", "marts", "--include-rejected"]) == 0
    files = sorted(p.name for p in (out / "export").glob("*.parquet"))
    assert files == ["mart_monthly.parquet", "rejected_prices.parquet"]
    rej = duckdb.connect().execute(f"SELECT _action FROM '{out / 'export' / 'rejected_prices.parquet'}'").fetchall()
    assert sorted(r[0] for r in rej) == ["drop_duplicates", "drop_rows"]


def test_failed_validation_is_not_exported_without_permission(tmp_path, capsys):
    out = finished_run(tmp_path, rules=[{"table": "prices", "expr": "price < 11"}])
    assert main(["export", str(out)]) == 1
    assert "validation after cleaning failed" in capsys.readouterr().err
    assert not (out / "export").exists()
    assert main(["export", str(out), "--allow-failed"]) == 0
    m = json.loads((out / "export" / "export_manifest.json").read_text())
    assert m["exported_despite_failure"] is True


def test_export_without_a_run_explains_what_to_do(tmp_path, capsys):
    assert main(["export", str(tmp_path)]) == 1
    assert "run `dqp run <config>` first" in capsys.readouterr().err
