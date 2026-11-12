"""Tests for `dqp init`: the starter config must be right where it decides, and its
suggestions must work when a person accepts all of them."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from dqp.cli import main
from dqp.config import load_config
from dqp.init import InitError, build_config, detect_header_rows, detect_type
from dqp.pipeline import run


def accept_all(text: str) -> dict:
    """What a person does who agrees with every suggestion: delete '# ' from them."""
    lines = []
    for line in text.splitlines():
        rest = line[2:]
        if line.startswith("# ") and (rest.startswith(" ") or rest in ("cleaning:", "validation:", "text_columns:")):
            line = rest
        lines.append(line)
    return yaml.safe_load("\n".join(lines).replace("TODO: why these rows are not data (footer notes?)", "footer")
                          .replace("TODO: why a repeated row is a mistake here", "recorded twice"))


def run_cfg(tmp_path: Path, cfg: dict) -> dict:
    cfg["output_dir"] = str(tmp_path / "out")
    p = tmp_path / "accepted.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return run(p)


# ---------- CSV ----------

def test_dutch_csv_semicolon_and_footer(tmp_path):
    f = tmp_path / "verkoop.csv"
    rows = "".join(f"{d:02d}-01-2024;{d},50;klant {d}\n" for d in range(1, 21))
    f.write_text("Datum;Bedrag;Klant ID\n" + rows + "Bron: kassa\n", encoding="utf-8")
    text, notes = build_config(f)
    cfg = yaml.safe_load(text)
    assert cfg["sources"][0] | {"path": None} == {"name": "verkoop", "type": "csv", "path": None,
                                                  "delimiter": ";", "header": True, "encoding": "utf-8"}
    assert "drop_rows" in text and "row 21: 'Bron: kassa'" in text        # the footer is pointed out
    log = run_cfg(tmp_path, accept_all(text))
    assert log["validation"]["status"] != "failed"
    assert log["cleaning"]["stg_verkoop"]["rows_rejected"] == 1


def test_latin1_csv_is_detected(tmp_path):
    f = tmp_path / "l.csv"
    f.write_bytes("naam,prijs\nCaf\xe9,1\nTh\xe9,2\n".encode("latin-1"))
    assert yaml.safe_load(build_config(f)[0])["sources"][0]["encoding"] == "latin-1"


# ---------- Excel ----------

@pytest.fixture
def messy_book(tmp_path) -> Path:
    path = tmp_path / "index.xlsx.xlsx"              # double extension on purpose
    rows = [["Index values, monthly"], ["(c) Example data provider"], [],
            [None, "All REITs", None, None], [None, "Total", None, "Price"], ["Date", "Return", "Index", "Index"]]
    rows.append(["2024-01-31", None, 100, 100])     # first period: a level but no return yet
    for m, d in enumerate(["2024-02-29", "2024-03-31", "2024-04-30", "2024-05-31", "2024-06-30"]):
        rows.append([d, 1.5 + m, 101 + m, 100.5 + m])
    rows += [[], ["Source: example. Contact data@example.com"]]
    notes = [["About this file"], ["Returns are monthly, in percent."], ["Indexes start at 100."]]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="Index Data", index=False, header=False)
        pd.DataFrame(notes).to_excel(xw, sheet_name="Notes", index=False, header=False)
        pd.DataFrame([[None]]).to_excel(xw, sheet_name="Empty", index=False, header=False)
    return path


def test_excel_header_rows_sparse_first_row_and_sheet_choice(messy_book):
    assert detect_type(messy_book) == "excel"
    text, notes = build_config(messy_book)
    src = yaml.safe_load(text)["sources"][0]
    assert src["sheets"] == [{"sheet": "Index Data", "header_rows": [3, 4, 5]}]   # multi-row header found
    joined = " | ".join(notes)
    assert "sheet 'Notes' left out: no block of numeric or dated rows" in joined
    assert "sheet 'Empty' left out: empty sheet" in joined
    assert "6 rows x 4 columns" in joined or "7 rows x 4 columns" in joined


def test_accepting_every_excel_suggestion_runs_and_passes(tmp_path, messy_book):
    text, _ = build_config(messy_book)
    cfg = accept_all(text)
    steps = cfg["cleaning"]["index__index_data"]
    assert {"rename": {"Date": "date"}} not in steps            # a named date column is not renamed
    assert any("snake_case" in s for s in steps)
    log = run_cfg(tmp_path, cfg)
    assert log["validation"]["status"] != "failed"
    assert log["cleaning"]["stg_index__index_data"]["rows_rejected"] == 1      # the source line


@pytest.mark.parametrize("rows, expected", [
    ([["a", "b"], [1, 2], [3, 4], [5, 6]], [0]),
    ([["Title"], [], ["x", "y", "z"], [1, 2, 3], [4, 5, 6], [7, 8, 9]], [2]),
    ([["only notes"], ["more notes"]], None),
])
def test_detect_header_rows(rows, expected):
    assert detect_header_rows(pd.DataFrame(rows))[0] == expected


# ---------- suggestions ----------

def test_codes_that_look_numeric_are_suggested_as_text(tmp_path):
    f = tmp_path / "orders.csv"
    lines = [f"{489434 + i},{85123 + i},{13085 + i},{i + 1}" for i in range(40)]
    lines += ["C489449,85123A,13090,-1"]
    f.write_text("Invoice,StockCode,Customer ID,Quantity\n" + "\n".join(lines) + "\n", encoding="utf-8")
    text, _ = build_config(f)
    assert "#   orders: [Invoice, StockCode, Customer ID]" in text
    assert "Quantity" not in text.split("text_columns:")[1].split("\n\n")[0]


def test_suggested_rules_hold_on_the_current_data(tmp_path):
    f = tmp_path / "daily.csv"
    days = [d for d in pd.bdate_range("2024-01-01", "2024-03-29") if str(d.date()) != "2024-02-14"]
    f.write_text("day,amount\n" + "".join(f"{d.date()},{i}\n" for i, d in enumerate(days)), encoding="utf-8")
    text, _ = build_config(f)
    assert "no_gaps: true" not in text                             # one business day is missing today
    log = run_cfg(tmp_path, accept_all(text))
    assert not [c for c in log["checks"].values() if c["origin"] == "rule" and c["status"] in ("FAIL", "ERROR")]


def test_written_config_loads_and_is_not_overwritten(tmp_path, capsys):
    f = tmp_path / "x.csv"
    f.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    out = tmp_path / "x.yaml"
    assert main(["init", str(f), "--out", str(out)]) == 0
    load_config(out)
    assert main(["init", str(f), "--out", str(out)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["init", str(f), "--out", str(out), "--force"]) == 0


def test_a_file_without_any_table_is_explained(tmp_path):
    path = tmp_path / "notes.xlsx"
    pd.DataFrame([["Just a note"], ["Another note"]]).to_excel(path, index=False, header=False)
    with pytest.raises(InitError, match="no sheet looks like a table"):
        build_config(path)
