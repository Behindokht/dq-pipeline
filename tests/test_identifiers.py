"""Column names are data too. A spreadsheet can call a column `order`, `n`, `_row` or
`semi;colon`; every layer has to carry that name through the generated SQL without
crashing, dropping the column or producing a rule that cannot run."""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb
import pytest
import yaml

from dqp.export import export_run
from dqp.init import build_config
from dqp.pipeline import run
from dqp.sql import ident, ident_if_needed
from test_init import accept_all

NAMES = ["n", "x", "r", "d", "row", "rowid", "_row", "order", "group", "select", "from", "table", "user",
         "end", "case", "column", "has space", 'quo"te', "semi;colon", "123start", "Count", ""]


def write_csv(path: Path, name: str) -> None:
    """The awkward column holds numbers, next to a date and a text column, with a
    footer note and a duplicate row so that cleaning steps with SQL are suggested."""
    rows = [["date", name, "label"]]
    rows += [[f"2026-01-{d:02d}", str(d * 10), f"item {d}"] for d in range(1, 21)]
    rows.append(rows[-1])
    rows.append(["Exported from the till on 21-01-2026", "", ""])
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


@pytest.mark.parametrize("name", NAMES, ids=lambda n: repr(n))
def test_any_column_name_survives_the_whole_pipeline(tmp_path, name):
    src = tmp_path / "awkward.csv"
    write_csv(src, name)
    text, _ = build_config(src, "awkward")
    cfg = accept_all(text)
    cfg["output_dir"] = str(tmp_path / "out")
    cfg["summary"] = {"provider": "none"}
    path = tmp_path / "awkward.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    log = run(path)
    assert log["status"] == "success", log.get("error")
    broken = [(c["name"], c["detail"]) for c in log["checks"].values() if c["status"] in ("ERROR", "FAIL")]
    assert not broken, broken

    con = duckdb.connect(log["db_path"], read_only=True)
    try:
        cols = [r[0] for r in con.execute('DESCRIBE "stg_awkward"').fetchall()]
        data = [c for c in cols if c != "_row"]
        values = con.execute(f'SELECT {ident(data[1])} FROM "stg_awkward" ORDER BY "_row"').fetchall()
    finally:
        con.close()
    assert cols.count("_row") == 1 and len(data) == 3, cols       # date, the awkward one, label + lineage
    assert [v[0] for v in values][:3] in ([10, 20, 30], ["10", "20", "30"])    # the column's own values, not row numbers
    assert log["cleaning"]["stg_awkward"]["rows_rejected"] == 2                 # footer + duplicate

    for fmt in ("csv", "xlsx"):
        export_run(Path(cfg["output_dir"]), fmt, "all", tmp_path / f"export_{fmt}")


def test_a_source_column_called__row_is_renamed_and_said(tmp_path):
    src = tmp_path / "lineage.csv"
    write_csv(src, "_row")
    cfg = {"dataset": "lineage", "output_dir": str(tmp_path / "out"), "summary": {"provider": "none"},
           "sources": [{"name": "lineage", "type": "csv", "path": str(src)}]}
    path = tmp_path / "lineage.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    log = run(path)
    load = log["loads"][0]
    assert load["columns"] == 3
    assert any("_row" in n and "renamed" in n for n in load["notes"]), load["notes"]


@pytest.mark.parametrize("name, quoted", [
    ("amount", "amount"), ("order_id", "order_id"),
    ("order", '"order"'), ("select", '"select"'), ("table", '"table"'), ("user", '"user"'),
    ("Amount", '"Amount"'), ("has space", '"has space"'), ('quo"te', '"quo""te"'),
    ("123start", '"123start"'), ("ünï", '"ünï"'), ("", '""'),
])
def test_names_in_written_sql_are_quoted_when_needed(name, quoted):
    assert ident_if_needed(name) == quoted
    assert ident(name) == '"' + name.replace('"', '""') + '"'


def test_only_one_quoting_helper_remains():
    root = Path(__file__).resolve().parents[1] / "src" / "dqp"
    defs = [f"{p.relative_to(root)}:{line.strip()}" for p in root.rglob("*.py")
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.startswith("def ") and any(f"def {n}(" in line for n in
                                              ("q", "_q", "quote_ident", "sql_name", "_sqlname", "_sqlname_any"))]
    assert defs == [], defs


hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from dqp.cleaning.staging import snake  # noqa: E402


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.text(st.characters(blacklist_categories=("Cc", "Cs", "Zl", "Zp")), max_size=24))
def test_generated_column_names_survive(tmp_path_factory, name):
    # collisions with the other two columns are WP3 (column names), not this test
    assume(name.strip().lower() not in ("date", "label") and snake(name) not in ("date", "label"))
    test_any_column_name_survives_the_whole_pipeline(tmp_path_factory.mktemp("h"), name)
