"""Tests for step 1: config validation, CSV and Excel connectors, run log."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from dqp.config import load_config
from dqp.pipeline import run


def write_config(tmp_path: Path, cfg: dict) -> Path:
    cfg.setdefault("output_dir", str(tmp_path / "out"))
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


# ---------- config ----------

def test_unknown_key_is_rejected(tmp_path):
    p = write_config(tmp_path, {"dataset": "x", "sources": [
        {"name": "a", "type": "csv", "path": "a.csv", "delimter": ";"}]})  # typo on purpose
    with pytest.raises(ValidationError):
        load_config(p)


def test_unknown_source_type_is_rejected(tmp_path):
    p = write_config(tmp_path, {"dataset": "x", "sources": [{"name": "a", "type": "pdf", "path": "a.pdf"}]})
    with pytest.raises(ValidationError):
        load_config(p)


def test_duplicate_source_names_are_rejected(tmp_path):
    src = {"name": "a", "type": "csv", "path": "a.csv"}
    p = write_config(tmp_path, {"dataset": "x", "sources": [src, dict(src)]})
    with pytest.raises(ValidationError, match="unique"):
        load_config(p)


def test_bad_table_name_is_rejected(tmp_path):
    p = write_config(tmp_path, {"dataset": "x", "sources": [{"name": "My Data", "type": "csv", "path": "a.csv"}]})
    with pytest.raises(ValidationError):
        load_config(p)


# ---------- CSV ----------

def test_csv_loads_as_text_without_losing_values(tmp_path):
    csv = tmp_path / "sales.csv"
    csv.write_text("id;amount;date\n001;1,50;2024-01-31\n002;;2024-02-29\n", encoding="utf-8")
    p = write_config(tmp_path, {"dataset": "sales", "sources": [
        {"name": "sales", "type": "csv", "path": str(csv), "delimiter": ";"}]})

    log = run(p)
    con = duckdb.connect(log["db_path"], read_only=True)
    rows = con.execute('SELECT id, amount, date FROM raw_sales ORDER BY id').fetchall()
    types = {r[0]: r[1] for r in con.execute("DESCRIBE raw_sales").fetchall()}

    # leading zeros and the decimal comma survive; empty becomes NULL
    assert rows == [("001", "1,50", "2024-01-31"), ("002", None, "2024-02-29")]
    assert set(types.values()) == {"VARCHAR"}


def test_missing_file_fails_and_is_logged(tmp_path):
    p = write_config(tmp_path, {"dataset": "x", "sources": [
        {"name": "a", "type": "csv", "path": str(tmp_path / "nope.csv")}]})
    with pytest.raises(FileNotFoundError):
        run(p)
    log = json.loads((tmp_path / "out" / "run_log.json").read_text())
    assert log["status"] == "failed"
    assert "nope.csv" in log["error"]


# ---------- Excel ----------

@pytest.fixture
def workbook(tmp_path) -> Path:
    path = tmp_path / "thesis.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame({"date": ["1998-01", "1998-02"], "sp500": [963.36, 1023.74]}).to_excel(
            xw, sheet_name="Prices 1998-2025", index=False)
        # a sheet with a title line above the real header, and an empty row at the end
        pd.DataFrame([["Monthly returns, source: thesis", None],
                      ["date", "ret"], ["1998-02", 0.0627], [None, None]]).to_excel(
            xw, sheet_name="Returns", index=False, header=False)
    return path


def test_excel_all_sheets_become_tables(tmp_path, workbook):
    p = write_config(tmp_path, {"dataset": "thesis", "sources": [
        {"name": "thesis", "type": "excel", "path": str(workbook)}]})  # sheets defaults to "all"
    log = run(p)
    assert [l["table"] for l in log["loads"]] == ["raw_thesis__prices_1998_2025", "raw_thesis__returns"]


def test_excel_selected_sheet(tmp_path, workbook):
    p = write_config(tmp_path, {"dataset": "thesis", "sources": [
        {"name": "thesis", "type": "excel", "path": str(workbook), "sheets": ["Prices 1998-2025"]}]})
    log = run(p)
    assert [l["table"] for l in log["loads"]] == ["raw_thesis__prices_1998_2025"]
    assert log["loads"][0]["rows"] == 2
    assert log["loads"][0]["column_names"] == ["date", "sp500"]


def test_excel_header_row_and_empty_rows(tmp_path, workbook):
    p = write_config(tmp_path, {"dataset": "thesis", "sources": [
        {"name": "ret", "type": "excel", "path": str(workbook), "sheets": ["Returns"], "header_rows": 1}]})
    log = run(p)
    load = log["loads"][0]
    assert load["column_names"] == ["date", "ret"]
    assert load["rows"] == 1  # trailing empty row dropped


def test_excel_missing_sheet_is_a_clear_error(tmp_path, workbook):
    p = write_config(tmp_path, {"dataset": "thesis", "sources": [
        {"name": "t", "type": "excel", "path": str(workbook), "sheets": ["Nope"]}]})
    with pytest.raises(ValueError, match="sheets not found"):
        run(p)


# ---------- re-runs ----------

def test_rerun_replaces_tables_and_keeps_history(tmp_path):
    csv = tmp_path / "a.csv"
    csv.write_text("x\n1\n2\n", encoding="utf-8")
    p = write_config(tmp_path, {"dataset": "a", "sources": [{"name": "a", "type": "csv", "path": str(csv)}]})
    first = run(p)
    csv.write_text("x\n1\n2\n3\n", encoding="utf-8")  # new data arrives
    second = run(p)

    con = duckdb.connect(second["db_path"], read_only=True)
    assert con.execute("SELECT COUNT(*) FROM raw_a").fetchall()[0][0] == 3  # no duplicates from re-run
    assert con.execute("SELECT COUNT(*) FROM _dqp_runs").fetchall()[0][0] == 2
    hashes = con.execute("SELECT DISTINCT sha256 FROM _dqp_loads").fetchall()
    assert len(hashes) == 2  # the changed input file is detectable
    assert first["run_id"] != second["run_id"]


# ---------- the real copper data ----------

def test_copper_config_runs_end_to_end(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo)
    cfg_dict = yaml.safe_load((repo / "configs" / "copper.yaml").read_text())
    cfg_dict["output_dir"] = str(tmp_path / "copper")
    p = tmp_path / "copper.yaml"
    p.write_text(yaml.safe_dump(cfg_dict))

    log = run(p)
    load = log["loads"][0]
    assert log["status"] == "success"
    assert load["rows"] == 800
    assert load["column_names"][:3] == ["month", "cu_usd", "cu_eur"]


# ---------- messy real-world workbooks ----------

@pytest.fixture
def messy_workbook(tmp_path) -> Path:
    """Mimics the Nareit / World Bank layouts: title lines, a 3-row header with
    merged group labels, a blank spacer column, '…' placeholders, an empty sheet,
    a duplicate header, and a double file extension."""
    path = tmp_path / "index.xlsx.xlsx"
    rows = [
        ["Index values, monthly", None, None, None, None, None],
        [None, None, None, None, None, None],
        [None, "All REITs", None, None, "Composite", None],      # group (merged in Excel)
        [None, "Total", None, None, "Total", "Total"],           # sub-group
        ["Date", "Return", "Index", None, "Return", "Return"],   # leaf; col 3 is a spacer
        ["2024-01-31", 1.5, 100, None, "…", 2.0],
        [None, None, None, None, None, None],
        ["2024-02-29", -0.5, 99.5, None, 0.7, 2.1],
    ]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="Index Data", index=False, header=False)
        pd.DataFrame([[" "]]).to_excel(xw, sheet_name="Notes", index=False, header=False)
    return path


def test_multi_row_header_merged_labels_and_spacers(tmp_path, messy_workbook):
    p = write_config(tmp_path, {"dataset": "m", "sources": [{
        "name": "reit", "type": "excel", "path": str(messy_workbook),
        "sheets": [{"sheet": "Index Data", "header_rows": [2, 3, 4]}]}]})
    load = run(p)["loads"][0]

    assert load["column_names"] == [
        "Date", "All REITs Total Return", "All REITs Total Index",
        "Composite Total Return", "Composite Total Return_2"]   # duplicate made unique
    assert load["rows"] == 2
    notes = " | ".join(load["notes"])
    assert "skipped 2 title line(s)" in notes
    assert "dropped 1 empty unnamed column(s)" in notes
    assert "dropped 1 fully empty row(s)" in notes
    assert "renamed to 'Composite Total Return_2'" in notes


def test_placeholder_values_become_null(tmp_path, messy_workbook):
    p = write_config(tmp_path, {"dataset": "m", "sources": [{
        "name": "reit", "type": "excel", "path": str(messy_workbook),
        "sheets": [{"sheet": "Index Data", "header_rows": [2, 3, 4]}]}]})
    con = duckdb.connect(run(p)["db_path"], read_only=True)
    vals = con.execute('SELECT "Composite Total Return" FROM raw_reit__index_data').fetchall()
    assert vals == [(None,), ("0.7",)]


def test_empty_sheet_is_skipped_and_logged(tmp_path, messy_workbook):
    p = write_config(tmp_path, {"dataset": "m", "sources": [{
        "name": "reit", "type": "excel", "path": str(messy_workbook), "sheets": ["Notes"]}]})
    log = run(p)
    assert log["status"] == "success"
    assert log["loads"][0]["status"] == "skipped"
    assert log["steps"][0]["tables"] == []


def test_header_row_beyond_sheet_is_a_clear_error(tmp_path, messy_workbook):
    p = write_config(tmp_path, {"dataset": "m", "sources": [{
        "name": "reit", "type": "excel", "path": str(messy_workbook),
        "sheets": [{"sheet": "Index Data", "header_rows": 50}]}]})
    with pytest.raises(ValueError, match="beyond the last row"):
        run(p)


def test_header_rows_must_increase(tmp_path):
    p = write_config(tmp_path, {"dataset": "m", "sources": [{
        "name": "x", "type": "excel", "path": "x.xlsx", "header_rows": [3, 2]}]})
    with pytest.raises(ValidationError):
        load_config(p)


# ---------- CSV edge cases found in the review ----------

def test_csv_footer_line_without_delimiters_is_loaded_not_fatal(tmp_path):
    csv = tmp_path / "f.csv"
    csv.write_text("d,v\n2024-01,1\n2024-02,2\nSource: example bank\n", encoding="utf-8")
    p = write_config(tmp_path, {"dataset": "f", "sources": [{"name": "f", "type": "csv", "path": str(csv)}]})
    log = run(p)
    con = duckdb.connect(log["db_path"], read_only=True)
    assert con.execute("SELECT d, v FROM raw_f ORDER BY rowid").fetchall()[-1] == ("Source: example bank", None)


def test_csv_wrong_encoding_gives_a_hint(tmp_path):
    from dqp.connectors.csv_file import CsvLoadError
    csv = tmp_path / "l.csv"
    csv.write_bytes("naam,prijs\nCaf\xe9,1\n".encode("latin-1"))
    p = write_config(tmp_path, {"dataset": "l", "sources": [{"name": "l", "type": "csv", "path": str(csv)}]})
    with pytest.raises(CsvLoadError, match="encoding: latin-1"):
        run(p)


# ---------- downloads ----------

def test_download_with_checksum_and_zip_extract(tmp_path, monkeypatch):
    import hashlib, io, urllib.request, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("inner/data.csv", "a,b\n1,2\n3,4\n")
    payload = buf.getvalue()

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp(payload))

    target = tmp_path / "cache" / "data.csv"
    src = {"name": "d", "type": "csv", "path": str(target),
           "download": {"url": "https://example.org/d.zip", "extract": "inner/data.csv",
                        "sha256": hashlib.sha256(payload).hexdigest()}}
    log = run(write_config(tmp_path, {"dataset": "d", "sources": [src]}))
    assert log["loads"][0]["rows"] == 2 and target.exists()
    assert "downloaded from https://example.org/d.zip, checksum verified" in log["loads"][0]["notes"]
    log = run(write_config(tmp_path, {"dataset": "d", "sources": [src]}))            # second run: cached copy
    assert "(cached copy)" in log["loads"][0]["notes"][0]


def test_download_checksum_mismatch_stops_the_run(tmp_path, monkeypatch):
    import io, urllib.request

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp(b"a,b\n1,2\n"))
    src = {"name": "d", "type": "csv", "path": str(tmp_path / "d.csv"),
           "download": {"url": "https://example.org/d.csv", "sha256": "0" * 64}}
    with pytest.raises(ValueError, match="the file changed at the source"):
        run(write_config(tmp_path, {"dataset": "d", "sources": [src]}))
    assert not (tmp_path / "d.csv").exists()
