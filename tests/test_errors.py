"""When something goes wrong, a person reads one plain sentence (what happened, what to do);
the traceback is there for a bug report but never in the way. And the rule builder only
lets a rule in that can run, showing at once how many rows it would fail."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dqp.cleaning import CleaningError
from dqp.cli import main
from dqp.errors import explain, sql_problem
from dqp.init import analyse
from dqp.pipeline import run

EXAMPLE = Path(__file__).resolve().parents[1] / "src" / "dqp" / "gui" / "examples" / "shop_orders.csv"


def broken_workbooks(tmp_path):
    a = tmp_path / "broken.xlsx"
    a.write_bytes(b"PK\x03\x04garbage garbage")
    b = tmp_path / "locked.xlsx"                     # what a password-protected workbook looks like: OLE2
    b.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1garbage")
    return a, b


def test_a_damaged_or_locked_workbook_gets_a_sentence(tmp_path):
    for f in broken_workbooks(tmp_path):
        with pytest.raises(Exception) as exc:
            analyse(f)
        sentence, details = explain(exc.value)
        assert "workbook could not be opened" in sentence and "password" in sentence
        assert "Traceback" in details


def bad_cleaning(tmp_path, where):
    src = tmp_path / "ok.csv"
    src.write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    cfg = {"dataset": "ok", "output_dir": str(tmp_path / "out"), "summary": {"provider": "none"},
           "sources": [{"name": "ok", "type": "csv", "path": str(src)}],
           "cleaning": {"ok": [{"drop_rows": {"where": where, "reason": "rows that are not data"}}]}}
    path = tmp_path / "ok.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


@pytest.mark.parametrize("where, says", [("amount >", "not complete or not valid"),
                                         ("nosuch > 1", "no column called 'nosuch'")])
def test_a_cleaning_step_that_cannot_run(tmp_path, where, says):
    with pytest.raises(CleaningError) as exc:
        run(bad_cleaning(tmp_path, where))
    sentence, _ = explain(exc.value)
    assert sentence.startswith("Cleaning step 1 (drop rows) on table 'ok' could not be applied")
    assert says in sentence


def test_other_known_and_unknown_errors():
    assert explain(FileNotFoundError("input file not found: x.csv"))[0] == "Input file not found: x.csv."
    sentence, _ = explain(RuntimeError("boom"))
    assert "unexpected" in sentence and "RuntimeError: boom" in sentence
    assert sql_problem("Binder Error: Cannot compare values of type VARCHAR and type DECIMAL(2,1)") \
        == "it compares a text with a number"
    # DuckDB versions word an unfinished condition differently; the sentence is the same
    for msg in ('Parser Error: syntax error at or near ")"', "Parser Error: syntax error at end of input"):
        assert sql_problem(msg).startswith("the SQL is not complete or not valid")


def test_cli_prints_the_sentence_and_the_traceback_only_with_v(tmp_path, capsys):
    broken, _ = broken_workbooks(tmp_path)
    assert main(["init", str(broken), "--out", str(tmp_path / "c.yaml")]) == 1
    err = capsys.readouterr().err
    assert "workbook could not be opened" in err and "Traceback" not in err and "-v" in err
    assert main(["init", str(broken), "--out", str(tmp_path / "c.yaml"), "-v"]) == 1
    assert "Traceback" in capsys.readouterr().err

    assert main(["run", str(bad_cleaning(tmp_path, "nosuch > 1"))]) == 1
    err = capsys.readouterr().err
    assert "no column called 'nosuch'" in err and "Traceback" not in err


# ------------------------------------------------------------------ rule preview

def example_preview():
    from dqp.preview import RulePreview
    an = analyse(EXAMPLE)
    acc = [s for s in an.suggestions if s.section != "rules"]
    for s in acc:
        if s.needs_reason:
            next(iter(s.config.values()))["reason"] = "the till adds an export note and double scans"
    return an, acc, RulePreview(an.config(acc))


def test_the_preview_knows_column_types_and_tries_rules():
    _, _, pv = example_preview()
    try:
        cols = pv.columns("shop_orders")
        assert cols["qty"] == "number" and cols["order_date"] == "date" and cols["customer_id"] == "text"
        assert pv.check("shop_orders", {"expr": "qty >"})["ok"] is False
        bad = pv.check("shop_orders", {"between": {"column": "product", "min": 1, "max": 5}})
        assert not bad["ok"] and "compares a text with a number" in bad["message"]
        good = pv.check("shop_orders", {"between": {"column": "qty", "min": 0, "max": 5}})
        assert good["ok"] and good["failing"] > 0 and "rows break this rule now" in good["message"]
    finally:
        pv.close()


def test_the_preview_count_is_the_count_of_the_real_run(tmp_path):
    an, acc, pv = example_preview()
    rule = {"between": {"column": "qty", "min": 0, "max": 5}}
    try:
        now = pv.check("shop_orders", rule)["failing"]
    finally:
        pv.close()
    from dqp.init import Suggestion
    cfg = an.config(acc + [Suggestion("rules", "shop_orders", rule)])
    cfg["output_dir"], cfg["summary"] = str(tmp_path / "out"), {"provider": "none"}
    path = tmp_path / "shop.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    log = run(path)
    real = [c for c in log["checks"].values() if c["origin"] == "rule" and c["kind"] == "between"]
    assert [c["failing"] for c in real] == [now]
