"""Tests for layer 6: the HTML report."""

from __future__ import annotations

from pathlib import Path

import yaml

from dqp.pipeline import run
from dqp.report import md_to_html

CSV = """month,price,note
2024-01,101.25,ok
2024-02,102.5,ok
2024-03,103.75,<script>alert(1)</script>
2024-05,107.5,ok
2024-06,108.75,ok
2024-07,110,ok
2024-08,111.25,ok
2024-09,112.5,ok
2024-10,113.75,ok
2024-11,115,ok
2024-12,116.25,ok
2025-01,117.5,ok
2025-02,118.75,ok
2025-03,120,ok
Source: example,,
"""


def setup(tmp_path: Path, show_values: bool = True, rules=None) -> Path:
    data = tmp_path / "p.csv"
    data.write_text(CSV, encoding="utf-8")
    cfg = {"dataset": "prices", "output_dir": str(tmp_path / "out"),
           "sources": [{"name": "prices", "type": "csv", "path": str(data)}],
           "cleaning": {"prices": [{"drop_rows": {"where": "month IS NULL", "reason": "footer line"}}]},
           "validation": {"rules": rules or []},
           "summary": {"provider": "none", "key_columns": {"prices": ["price"]}},
           "report": {"show_values": show_values}}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return p


def report(tmp_path, **kw) -> str:
    log = run(setup(tmp_path, **kw))
    return (Path(log["db_path"]).parent / "report.html").read_text(encoding="utf-8")


def test_report_is_one_complete_file_with_every_section(tmp_path):
    html = report(tmp_path)
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    for section in ('id="summary"', 'id="checks"', 'id="cleaning"', 'id="gaps"', 'id="numbers"',
                    'id="columns"', 'id="lineage"'):
        assert section in html
    assert "<title>Prices Data Quality</title>" in html
    assert "<script src=" not in html            # no external JavaScript


def test_report_shows_the_story(tmp_path):
    html = report(tmp_path)
    assert "On arrival (raw data)" in html and "After cleaning (the result that counts)" in html
    assert "Reason: footer line" in html                        # cleaning step with its reason
    assert "Rejected rows (1)" in html                          # the footer row is kept, and shown
    assert "2024-04" in html                                    # the missing month is documented
    assert 'class="line"' in html and "120" in html             # headline chart and latest value


def test_data_cannot_inject_html(tmp_path):
    html = report(tmp_path, rules=[{"table": "prices", "expr": "note = 'ok'", "severity": "warn"}])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html       # shown as text in the failing example


def test_show_values_false_hides_data_values(tmp_path):
    html = report(tmp_path, show_values=False,
                  rules=[{"table": "prices", "expr": "price < 110", "severity": "warn"}])
    for value in ("117.5", "118.75", "120", "Source: example"):
        assert value not in html, value
    assert "this dataset includes licensed data" in html
    assert 'class="line"' not in html
    assert "row 6" in html                                       # first failing row (2024-07, 110) by number only


def test_all_passing_checks_start_collapsed(tmp_path):
    data = tmp_path / "ok.csv"
    data.write_text("month,price\n2024-01,1\n2024-02,2\n2024-03,3\n", encoding="utf-8")
    cfg = {"dataset": "ok", "output_dir": str(tmp_path / "out"),
           "sources": [{"name": "ok", "type": "csv", "path": str(data)}], "summary": {"provider": "none"}}
    p = tmp_path / "ok.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    html = (Path(run(p)["db_path"]).parent / "report.html").read_text()
    assert "checks passed. Show them</summary>" in html


def test_markdown_subset():
    out = md_to_html("## Head\n\nSome **bold** and `code_1`.\n\n- one <b>\n- two")
    assert "<h3>Head</h3>" in out and "<strong>bold</strong>" in out and "<code>code_1</code>" in out
    assert "<li>one &lt;b&gt;</li>" in out and out.count("<li>") == 2


# ---------- the static site ----------

def test_site_lists_each_run_and_copies_its_report(tmp_path):
    from dqp.cli import main
    log = run(setup(tmp_path))
    out_dir = Path(log["db_path"]).parent
    site = tmp_path / "site"
    assert main(["site", "--out", str(site), "--repo-url", "https://github.com/x/y", str(out_dir)]) == 0
    index = (site / "index.html").read_text()
    assert "<title>dq-pipeline Reports</title>" in index
    assert 'href="prices/"' in index and "https://github.com/x/y" in index
    assert "on arrival" in index and "after cleaning" in index
    assert (site / "prices" / "index.html").read_text() == (out_dir / "report.html").read_text()
    assert (site / ".nojekyll").exists()


def test_site_refuses_a_folder_without_a_finished_run(tmp_path, capsys):
    from dqp.cli import main
    (tmp_path / "empty").mkdir()
    assert main(["site", "--out", str(tmp_path / "s"), str(tmp_path / "empty")]) == 1
    assert "no finished run" in capsys.readouterr().err
