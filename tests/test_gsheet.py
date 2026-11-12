"""Tests for the Google Sheets connector. The network is replaced by a fake that
returns a real .xlsx workbook (or a login page, or nothing), so the tests run offline;
the live connection is exercised by the CI workflow on Google's public sample sheet."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest
import yaml

from dqp.cli import main
from dqp.connectors.gsheet import GSheetError, export_url
from dqp.pipeline import run

URL = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=0"


def workbook_bytes() -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame({"deal_id": ["D-001", "D-002", "D-003"], "stage": ["won", "lost", "open"],
                      "amount": [1200, 800, 450], "close_date": ["2026-01-15", "2026-02-01", None]}
                     ).to_excel(xw, sheet_name="Deals", index=False)
        pd.DataFrame([["Pipeline export"], [None], ["owner", "region"], ["Ana", "NL"], ["Ben", "BE"],
                      ["Cleo", "DE"]]).to_excel(xw, sheet_name="Owners", index=False, header=False)
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_google(monkeypatch):
    calls = []

    def serve(payload):
        def urlopen(req, timeout=0):
            calls.append(req.full_url)
            if isinstance(payload, Exception):
                raise payload
            return _Resp(payload)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    serve.calls = calls
    return serve


def cfg_file(tmp_path: Path, **source) -> Path:
    cfg = {"dataset": "crm", "output_dir": str(tmp_path / "out"), "summary": {"provider": "none"},
           "sources": [{"name": "crm", "type": "gsheet", "url": URL, "cache_dir": str(tmp_path / "cache"),
                        **source}]}
    p = tmp_path / "crm.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return p


@pytest.mark.parametrize("url, link", [
    (URL, "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/export?format=xlsx"),
    ("https://docs.google.com/spreadsheets/d/e/2PACX-1vQabc_DEF-123/pubhtml",
     "https://docs.google.com/spreadsheets/d/e/2PACX-1vQabc_DEF-123/pub?output=xlsx"),
])
def test_export_links(url, link):
    assert export_url(url)[0] == link


def test_every_tab_becomes_a_table(tmp_path, fake_google):
    fake_google(workbook_bytes())
    log = run(cfg_file(tmp_path, sheets=["Deals", {"sheet": "Owners", "header_rows": 2}]))
    loads = {l["table"]: l for l in log["loads"]}
    assert set(loads) == {"raw_crm__deals", "raw_crm__owners"}
    assert loads["raw_crm__deals"]["rows"] == 3 and loads["raw_crm__owners"]["rows"] == 3
    assert loads["raw_crm__deals"]["source_type"] == "gsheet" and loads["raw_crm__deals"]["location"] == URL
    assert loads["raw_crm__deals"]["notes"][0].startswith("downloaded from Google Sheets")
    assert fake_google.calls == [export_url(URL)[0]]
    html = (Path(log["db_path"]).parent / "report.html").read_text()
    assert f'<a href="{URL}">Google Sheet</a>' in html


def test_a_private_sheet_gets_a_clear_message(tmp_path, fake_google):
    fake_google(b"<!doctype html><html>Sign in - Google Accounts</html>")
    with pytest.raises(GSheetError, match="Anyone with the link"):
        run(cfg_file(tmp_path))


def test_offline_uses_the_last_copy_and_says_so(tmp_path, fake_google):
    fake_google(workbook_bytes())
    run(cfg_file(tmp_path))                                       # first run: online, keeps a copy
    fake_google(urllib.error.URLError("no network"))
    log = run(cfg_file(tmp_path))
    assert log["status"] == "success"
    assert "could not be reached" in log["loads"][0]["notes"][0] and "used the copy downloaded" in log["loads"][0]["notes"][0]


def test_offline_without_a_copy_fails_clearly(tmp_path, fake_google):
    fake_google(urllib.error.URLError("no network"))
    with pytest.raises(GSheetError, match="could not download"):
        run(cfg_file(tmp_path))


def test_init_from_a_sheet_link(tmp_path, fake_google, monkeypatch):
    fake_google(workbook_bytes())
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "crm.yaml"
    assert main(["init", URL, "--name", "crm", "--out", str(out)]) == 0
    cfg = yaml.safe_load(out.read_text())
    src = cfg["sources"][0]
    assert src["type"] == "gsheet" and src["url"] == URL and "path" not in src
    assert src["sheets"] == [{"sheet": "Deals", "header_rows": 0}, {"sheet": "Owners", "header_rows": 2}]
    assert "#   crm__deals: [deal_id]" not in out.read_text()                # D-001 are text already
    cfg["output_dir"] = str(tmp_path / "out")
    out.write_text(yaml.safe_dump(cfg))
    assert run(out)["status"] == "success"
