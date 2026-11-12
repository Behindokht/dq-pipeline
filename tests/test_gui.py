"""The browser interface, driven headless with Streamlit's AppTest: the same clicks a
person makes, without a browser. The GUI only builds a config and calls the pipeline,
so these tests check the wiring: suggestions in, the right config out, the result shown."""

from __future__ import annotations

import io
import shutil
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from dqp.gui.helpers import portable  # noqa: E402
from dqp.pipeline import run  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "dqp" / "gui" / "app.py"


def start() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception
    return at


def analyse_example(at: AppTest) -> AppTest:
    at.radio(key="how").set_value("Try the example").run()
    at.button(key="analyse").click().run()
    assert not at.exception and at.session_state.step == 1
    return at


def fill_reasons(at: AppTest) -> None:
    for box in at.text_input:
        if box.key and box.key.startswith("reason:"):
            box.set_value("the till adds an export note and double scans")
    at.run()


def test_example_from_start_to_result():
    at = analyse_example(start())
    assert any("86 rows" in m.value for m in at.markdown)                 # the preview step
    at.button(key="next1").click().run()
    labels = [c.label for c in at.checkbox]
    assert "shop_orders: set aside rows that are not data" in labels
    assert "shop_orders: Customer ID" in labels                            # an ID kept as text
    assert at.button(key="next2").disabled                                 # a reason is still missing
    fill_reasons(at)
    assert not at.button(key="next2").disabled
    at.button(key="next2").click().run()
    at.button(key="next3").click().run()
    at.button(key="run").click().run()
    assert not at.exception
    log = at.session_state.result
    assert log["status"] == "success"
    assert log["cleaning"]["stg_shop_orders"] == {"rows_raw": 86, "rows_staging": 83, "rows_rejected": 3}
    assert log["validation"]["status"] == "passed_with_warnings"          # Sundays: the shop is closed
    assert any("Passed with warnings" in m.value for m in at.markdown)
    reasons = {e["reason"] for e in log["cleaning_log"] if e["action"] in ("drop_rows", "drop_duplicates")}
    assert reasons == {"the till adds an export note and double scans"}   # the person's words, in the log


def test_choices_survive_going_back_and_forth():
    at = analyse_example(start())
    at.button(key="next1").click().run()
    at.checkbox(key="use:cleaning:shop_orders:trim_whitespace").uncheck().run()
    fill_reasons(at)
    at.button(key="next2").click().run()
    at.button(key="back3").click().run()                                    # back to cleaning
    assert at.checkbox(key="use:cleaning:shop_orders:trim_whitespace").value is False
    assert at.text_input(key="reason:cleaning:shop_orders:drop_duplicates").value.startswith("the till")


def test_rules_follow_the_column_names_that_are_kept():
    at = analyse_example(start())
    at.button(key="next1").click().run()
    at.checkbox(key="use:cleaning:shop_orders:snake_case").uncheck().run()
    fill_reasons(at)
    at.button(key="next2").click().run()
    assert "shop_orders: Unit Price is never negative" in [c.label for c in at.checkbox]
    at.selectbox(key="r_col").set_value("Order No")
    at.selectbox(key="r_kind").set_value("unique").run()
    at.button(key="r_add").click().run()
    at.button(key="next3").click().run()
    at.button(key="run").click().run()
    log = at.session_state.result
    assert log["status"] == "success", log.get("error")
    names = {c["name"] for c in log["checks"].values() if c["stage"] == "staging"}
    assert any("Order No" in n for n in names) and any("Unit Price" in n for n in names)


def test_portable_config_reruns_from_the_command_line(tmp_path, monkeypatch):
    at = analyse_example(start())
    at.button(key="next1").click().run()
    fill_reasons(at)
    for k in ("next2", "next3", "run"):
        at.button(key=k).click().run()
    gui_log, cfg = at.session_state.result, at.session_state.result_cfg
    an = at.session_state.an

    # what the "Settings for next time" button hands out, used as the README says
    text = portable(cfg, an)
    (tmp_path / "data").mkdir()
    shutil.copy(an.local_path, tmp_path / "data" / an.local_path.name)
    (tmp_path / "shop.yaml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cli_log = run(tmp_path / "shop.yaml")
    assert cli_log["validation"]["summary"] == gui_log["validation"]["summary"]
    assert cli_log["cleaning"] == gui_log["cleaning"]


def test_a_private_sheet_gets_a_clear_message(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: Resp(b"<html>Sign in - Google Accounts</html>"))
    at = start()
    at.radio(key="how").set_value("Google Sheets link").run()
    at.text_input(key="url").set_value("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQ/edit").run()
    at.button(key="analyse").click().run()
    assert at.session_state.step == 0
    assert "Anyone with the link" in at.error[0].value




def test_a_sheet_link_and_a_header_fix(monkeypatch):
    from test_gsheet import workbook_bytes

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp(workbook_bytes()))
    url = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQ/edit"
    at = start()
    at.radio(key="how").set_value("Google Sheets link").run()
    at.text_input(key="url").set_value(url).run()
    at.text_input(key="name").set_value("crm").run()
    at.button(key="analyse").click().run()
    an = at.session_state.an
    assert at.session_state.step == 1 and an.source["type"] == "gsheet" and an.source["url"] == url
    assert at.text_input(key="sheet_hr1").value == "3"                 # Owners: title, blank, then the header
    at.text_input(key="sheet_hr1").set_value("1").run()                  # a person says otherwise
    at.button(key="reread").click().run()
    assert not at.exception
    assert at.session_state.an.source["sheets"][1] == {"sheet": "Owners", "header_rows": 0}
    assert "Pipeline export" in at.session_state.an.profiles["raw_crm__owners"]["columns_profile"]
