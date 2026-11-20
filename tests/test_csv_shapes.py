"""Odd but legal CSV files: a line break inside a quoted cell, a file with one column,
a file with a header and no rows. Each must load, profile and run, or say clearly why not."""

from __future__ import annotations

import duckdb
import pytest
import yaml

from dqp.init import InitError, build_config, detect_csv
from dqp.pipeline import run
from test_init import accept_all


def run_file(tmp_path, text, name="shape", raw=None):
    src = tmp_path / f"{name}.csv"
    if raw is not None:
        src.write_bytes(raw)
    else:
        src.write_text(text, encoding="utf-8", newline="")
    cfg_text, notes = build_config(src, name)
    cfg = {k: v for k, v in accept_all(cfg_text).items() if v is not None}
    cfg["output_dir"] = str(tmp_path / "out")
    cfg["summary"] = {"provider": "none"}
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    log = run(path)
    return log, notes


def test_a_quoted_line_break_stays_inside_its_cell(tmp_path):
    log, _ = run_file(tmp_path, 'id,note,amount\n1,"line one\nline two",10\n2,plain,20\n3,"a, b",30\n')
    assert log["status"] == "success", log.get("error")
    assert log["loads"][0]["rows"] == 3 and log["loads"][0]["columns"] == 3
    con = duckdb.connect(log["db_path"], read_only=True)
    try:
        notes = [r[0] for r in con.execute('SELECT note FROM "raw_shape" ORDER BY id').fetchall()]
    finally:
        con.close()
    assert notes == ["line one\nline two", "plain", "a, b"]


def test_a_quoted_line_break_with_crlf_and_a_short_line(tmp_path):
    text = 'id,note,amount\r\n1,"x\r\ny",10\r\n2,only two\r\n'
    log, _ = run_file(tmp_path, text)
    assert log["status"] == "success", log.get("error")
    assert log["loads"][0]["rows"] == 2


def test_a_file_with_one_column(tmp_path):
    log, _ = run_file(tmp_path, "amount\n10\n20\n30\n")
    assert log["status"] == "success", log.get("error")
    assert log["loads"][0]["columns"] == 1 and log["loads"][0]["rows"] == 3
    assert log["validation"]["status"] == "passed"


def test_a_single_column_with_commas_inside_quotes(tmp_path):
    log, _ = run_file(tmp_path, 'city\n"Rotterdam, NL"\n"Delft, NL"\n')
    assert log["loads"][0]["columns"] == 1 and log["loads"][0]["rows"] == 2


def test_a_single_line_file_is_a_header_without_rows(tmp_path):
    log, notes = run_file(tmp_path, "id,amount,date\n")
    assert log["status"] == "success"
    assert log["loads"][0]["rows"] == 0
    assert any("no data rows" in n for n in notes)
    assert log["validation"]["status"] == "failed"               # "table has rows" is the honest verdict


def test_a_binary_file_is_refused_in_words(tmp_path):
    src = tmp_path / "junk.csv"
    src.write_bytes(b"\x00\x01\x02\x03binary\x00\x00")
    with pytest.raises(InitError, match="binary"):
        detect_csv(src)


def test_an_empty_file_is_refused_in_words(tmp_path):
    src = tmp_path / "empty.csv"
    src.write_text("", encoding="utf-8")
    with pytest.raises(InitError, match="empty"):
        detect_csv(src)


def test_an_uncommented_empty_section_is_read_as_empty(tmp_path):
    from dqp.config import load_config
    src = tmp_path / "a.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    path = tmp_path / "a.yaml"
    path.write_text(f"dataset: a\nsources:\n  - name: a\n    type: csv\n    path: {src}\n"
                    "validation:\ncleaning:\ntext_columns:\nmarts:\n", encoding="utf-8")
    cfg, _ = load_config(path)
    assert cfg.validation.rules == [] and cfg.cleaning == {} and cfg.marts == []
