"""Column names after tidying: no two may become the same, non-Latin names stay readable,
and the names `init` predicts are the names the pipeline really produces."""

from __future__ import annotations

import duckdb
import pytest
import yaml

from dqp.cleaning import snake
from dqp.cleaning.staging import snake_map
from dqp.init import build_config
from dqp.pipeline import run
from test_init import accept_all


def staging_columns(tmp_path, csv_text, name="names"):
    src = tmp_path / f"{name}.csv"
    src.write_text(csv_text, encoding="utf-8")
    text, _ = build_config(src, name)
    cfg = accept_all(text)
    cfg["output_dir"] = str(tmp_path / "out")
    cfg["summary"] = {"provider": "none"}
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    log = run(path)
    assert log["status"] == "success", log.get("error")
    broken = [(c["name"], c["detail"]) for c in log["checks"].values() if c["status"] in ("ERROR", "FAIL")]
    assert not broken, broken
    con = duckdb.connect(log["db_path"], read_only=True)
    try:
        cols = [r[0] for r in con.execute(f'DESCRIBE "stg_{name}"').fetchall() if r[0] != "_row"]
    finally:
        con.close()
    return cfg, log, cols


def test_names_that_tidy_to_the_same_name_stay_apart(tmp_path):
    _, log, cols = staging_columns(tmp_path, "Order ID,order_id,Order-ID\n1,2,3\n4,5,6\n")
    assert cols == ["order_id_2", "order_id", "order_id_3"], cols     # the already tidy name keeps its place
    assert any("order_id" in e.get("details", "") for e in log["cleaning_log"] if e["action"] == "snake_case")


def test_a_name_already_tidy_keeps_its_name(tmp_path):
    assert snake_map(["Order ID", "order_id"]) == {"Order ID": "order_id_2", "order_id": "order_id"}


def test_persian_headers_stay_readable(tmp_path):
    _, _, cols = staging_columns(tmp_path, "تاریخ,مبلغ,نام\n2026-01-01,10,علی\n2026-01-02,20,رضا\n")
    assert cols == ["تاریخ", "مبلغ", "نام"], cols


@pytest.mark.parametrize("name, expected", [
    ("Straße Größe", "straße_größe"), ("Prix (€)", "prix"), ("日付", "日付"), ("  ", "column"),
    ("2nd", "c_2nd"), ("Ünï Cödé", "ünï_cödé"), ("a--b__c", "a_b_c"),
])
def test_snake_keeps_letters_of_any_script(name, expected):
    assert snake(name) == expected


def test_headers_with_nothing_readable_get_distinct_names(tmp_path):
    _, _, cols = staging_columns(tmp_path, "€,$,%\n1,2,3\n")
    assert len(set(cols)) == 3, cols


MIXED = "Order ID,order_id,Order-ID,تاریخ,مبلغ (ریال),Amount,AMOUNT,2nd,2ND,€\n" + \
        "".join(f"{i},{i},{i},2026-01-{i:02d},{i * 10},{i},{i},{i},{i},{i}\n" for i in range(1, 8))


def test_init_predicts_the_names_the_pipeline_produces(tmp_path):
    from dqp.init import analyse
    src = tmp_path / "mixed.csv"
    src.write_text(MIXED, encoding="utf-8")
    an = analyse(src, "mixed")
    predicted = list(an.final_names["raw_mixed"].values())
    _, _, cols = staging_columns(tmp_path, MIXED, "mixed")
    assert cols == predicted, (cols, predicted)
