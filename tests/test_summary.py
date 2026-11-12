"""Tests for layer 5: the summary may only use computed, validated numbers.

The LLM is replaced by fake clients with known behaviour: one that follows the
rules, ones that break them in different ways, and one whose API call fails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from dqp.config import SummaryConfig
from dqp.pipeline import run
from dqp.summary import FactPack, LLMClient, LLMError, check_draft, fmt, make_client, render

# monthly 2024-01 .. 2025-03 with 2024-04 and 2024-06 missing, price = 100 + 5 x month index,
# and a footer line under the data
CSV = """month,price
2024-01,100
2024-02,105
2024-03,110
2024-05,120
2024-07,130
2024-08,135
2024-09,140
2024-10,145
2024-11,150
2024-12,155
2025-01,160
2025-02,165
2025-03,170
Source: example,
"""


def setup(tmp_path: Path, rules=None, cleaning=None, summary=None, csv: str = CSV) -> Path:
    data = tmp_path / "p.csv"
    data.write_text(csv, encoding="utf-8")
    cfg = {"dataset": "prices", "output_dir": str(tmp_path / "out"),
           "sources": [{"name": "prices", "type": "csv", "path": str(data)}],
           "cleaning": cleaning if cleaning is not None else
           {"prices": [{"drop_rows": {"where": "month IS NULL", "reason": "footer line"}}]},
           "validation": {"rules": rules or []},
           "summary": summary or {"provider": "none", "key_columns": {"prices": ["price"]}}}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return p


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(self, *drafts):
        super().__init__("fake-1")
        self.drafts, self.prompts = list(drafts), []

    def complete(self, system, user, max_tokens=2000):
        self.prompts.append(user)
        d = self.drafts.pop(0)
        if isinstance(d, Exception):
            raise d
        return d


GOOD = """## Summary
Loaded {{dataset.raw_rows}} rows. On arrival validation {{arrival.status}}; after cleaning {{final.status}} with {{final.warn}} warning(s).
## Data quality
- `{{clean1.table}}` lost {{clean1.rows_removed}} row(s): {{clean1.reason}}. Total kept aside: {{clean.rejected_rows}}.
- Missing months: {{gap.prices.missing_periods}} ({{gap.prices.missing_list}}).
- {{check1.status}} after cleaning: {{check1.name}}.
## Key findings
- `price` latest {{key.prices.price.latest}} on {{key.prices.price.latest_date}}, {{key.prices.price.change_1y_pct}} in a year.
"""


def summary_json(log) -> dict:
    return json.loads((Path(log["db_path"]).parent / "summary.json").read_text())


# ---------- the honest path ----------

def test_llm_draft_that_follows_the_rules_is_used_and_rendered(tmp_path):
    llm = FakeLLM(GOOD)
    log = run(setup(tmp_path), llm_client=llm)
    s = summary_json(log)
    assert s["source"] == "llm" and s["model"] == "fake:fake-1"
    text = log["summary_text"]
    assert "Loaded 14 rows" in text and "lost 1 row(s): footer line" in text
    assert "Missing months: 2 (2024-04, 2024-06)" in text
    assert "WARN after cleaning: no gaps in 'month' (monthly)" in text
    assert "latest 170 on 2025-03-01, +54.5% in a year" in text       # 170 / 110 - 1
    assert "{{" not in text


def test_prompt_contains_values_and_marks_required_facts(tmp_path):
    llm = FakeLLM(GOOD)
    run(setup(tmp_path), llm_client=llm)
    prompt = llm.prompts[0]
    assert "{{final.status}} = passed" in prompt
    assert "{{clean1.rows_removed}} = 1" in prompt and "REQUIRED" in prompt


# ---------- the checker rejects, retries, falls back ----------

@pytest.mark.parametrize("bad, problem", [
    (GOOD.replace("{{dataset.raw_rows}}", "6"), "number written without a fact placeholder"),
    (GOOD.replace("{{key.prices.price.change_1y_pct}}", "about 24%"), "number written without"),
    (GOOD + "\nPrices roughly doubled.", "number word 'doubled'"),
    (GOOD.replace("{{final.status}}", "{{final.sentiment}}"), "unknown fact id"),
    (GOOD.replace("{{clean1.rows_removed}}", "some"), "required fact not mentioned: {{clean1.rows_removed}}"),
])
def test_bad_drafts_fall_back_to_the_template(tmp_path, bad, problem):
    llm = FakeLLM(bad, bad)                              # wrong twice: initial + one retry
    log = run(setup(tmp_path), llm_client=llm)
    s = summary_json(log)
    assert s["source"] == "template"
    assert "failed the number check 2 time(s)" in s["reason"]
    assert any(problem in v for v in s["attempts"][0]["violations"])
    assert s["attempts"][0]["draft"] == bad              # the rejected draft is kept for inspection
    assert "## Summary" in log["summary_text"]


def test_retry_with_feedback_can_fix_a_draft(tmp_path):
    llm = FakeLLM(GOOD.replace("{{dataset.raw_rows}}", "6"), GOOD)
    log = run(setup(tmp_path), llm_client=llm)
    s = summary_json(log)
    assert s["source"] == "llm" and len(s["attempts"]) == 2
    assert "previous draft was rejected" in llm.prompts[1] and "Loaded 6 rows" in llm.prompts[1]  # quotes the bad text


def test_api_failure_falls_back_to_the_template(tmp_path):
    log = run(setup(tmp_path), llm_client=FakeLLM(LLMError("HTTP 529: overloaded")))
    s = summary_json(log)
    assert s["source"] == "template" and "LLM call failed: HTTP 529" in s["reason"]


def test_llm_is_not_asked_when_validation_failed(tmp_path):
    llm = FakeLLM(GOOD)
    log = run(setup(tmp_path, rules=[{"table": "prices", "expr": "price < 120"}]), llm_client=llm)
    s = summary_json(log)
    assert llm.prompts == []                                    # never called
    assert s["source"] == "template" and "only runs on validated data" in s["reason"]
    text = log["summary_text"]
    assert "**FAIL** (after cleaning)" in text and "price < 120" in text
    assert "Not given: validation after cleaning failed" in text   # no headline numbers from failed data


# ---------- the template ----------

def test_template_passes_its_own_check_and_states_failures(tmp_path):
    log = run(setup(tmp_path, rules=[{"table": "prices", "expr": "price > 0", "severity": "warn"}]))
    s = summary_json(log)
    assert s["source"] == "template" and "provider is 'none'" in s["reason"]
    assert "moved 1 row(s) to rejected tables" in log["summary_text"]
    assert "2 period(s) missing from the date column: 2024-04, 2024-06" in log["summary_text"]


def test_template_without_cleaning_or_problems(tmp_path):
    csv = "month,price\n2024-01,1\n2024-02,2\n2024-03,3\n"
    log = run(setup(tmp_path, cleaning={}, csv=csv))
    assert "## Checks that did not pass" not in log["summary_text"]
    assert "validation **passed**" in log["summary_text"]


def test_outputs_written(tmp_path):
    log = run(setup(tmp_path))
    out = Path(log["db_path"]).parent
    md = (out / "summary.md").read_text()
    assert md.startswith("# Data quality report: prices") and "none was typed by the writer" in md
    facts = json.loads((out / "facts.json").read_text())["facts"]
    assert facts["final.status"]["value"] == "passed with warnings"     # the two missing months
    assert facts["clean1.rows_removed"]["required"] is True


def test_unknown_key_column_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no column"):
        run(setup(tmp_path, summary={"provider": "none", "key_columns": {"prices": ["prijs"]}}))


# ---------- checker details ----------

def facts(**kv) -> FactPack:
    fp = FactPack()
    for k, (v, kind, req) in kv.items():
        fp.add(k.replace("__", "."), v, kind, "test", req)
    return fp


def test_checker_allows_list_markers_and_known_identifiers():
    fp = facts(a__n=(3, "number", False))
    ok = check_draft("1. first\n2. `ratio_ma12` has {{a.n}} gaps", fp, identifiers={"ratio_ma12"})
    assert ok.ok, ok.violations
    bad = check_draft("`col2024` has {{a.n}} gaps", fp, identifiers={"ratio_ma12"})
    assert not bad.ok


def test_checker_requires_required_facts():
    fp = facts(a__n=(3, "number", True), b__n=(4, "number", False))
    out = check_draft("only {{b.n}}", fp)
    assert out.missing_required == ["a.n"]


@pytest.mark.parametrize("value, kind, text", [
    (1234567, "number", "1,234,567"), (14326.0, "number", "14,326"), (2590.62, "number", "2,590.6"),
    (4.4099, "number", "4.41"), (0.70991, "number", "0.7099"), (48.06, "percent", "+48.1%"),
    (-1.83, "percent", "-1.8%"), (None, "number", "n/a"), ("passed", "status", "passed"),
])
def test_number_format(value, kind, text):
    assert fmt(value, kind) == text


def test_render_replaces_every_placeholder():
    fp = facts(a__n=(1500.0, "number", False), a__s=("ok", "text", False))
    assert render("{{a.n}} rows, {{ a.s }}", fp) == "1,500 rows, ok"


# ---------- provider selection ----------

def test_provider_selection_from_environment(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MY_KEY"):
        monkeypatch.delenv(k, raising=False)
    client, why = make_client(SummaryConfig())
    assert client is None and "no API key found" in why

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    client, why = make_client(SummaryConfig())
    assert client is None and "summary.model must be set" in why       # no guessed model names
    client, _ = make_client(SummaryConfig(model="some-model"))
    assert client.provider == "openai" and client.model == "some-model"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client, _ = make_client(SummaryConfig())
    assert client.provider == "anthropic"                              # auto prefers Anthropic

    monkeypatch.setenv("MY_KEY", "k")
    client, _ = make_client(SummaryConfig(provider="openai", model="m", api_key_env="MY_KEY",
                                          base_url="http://localhost:11434/v1"))
    assert client.url == "http://localhost:11434/v1/chat/completions"
    assert make_client(SummaryConfig(provider="none"))[0] is None


# ---------- loopholes found in the review ----------

def _pack():
    fp = FactPack()
    fp.add("key.cu.latest", 14326.0, "number", "latest")
    fp.add("key.cu.min", 607.0, "number", "lowest")
    fp.add("key.cu.min_date", "1961-01-01", "date", "date of lowest")
    fp.add("final.status", "passed", "status", "result", required=True)
    return fp


@pytest.mark.parametrize("draft, problem", [
    ("Validation {{final.status}}. Copper is now at {{key.cu.min}}.", "is a min value but its sentence"),
    ("Validation {{final.status}}. Prices rose because of weak demand.", "causal claim 'because'"),
    ("Validation {{final.status}}. Prices were driven by China.", "causal claim 'driven by'"),
    ("Validation {{final.status}}. Copper fell by a third.", "number word 'third'"),
    ("Validation {{final.status}}. Copper hit its low once.", "number word 'once'"),
    ("Validation {{final.status}}. The majority of checks passed.", "number word 'majority'"),
])
def test_checker_catches_misuse_and_invented_claims(draft, problem):
    out = check_draft(draft, _pack())
    assert not out.ok and any(problem in v for v in out.violations), out.violations


def test_checker_accepts_numbers_in_their_role():
    draft = ("Validation {{final.status}}. Copper is now at {{key.cu.latest}}; "
             "its lowest was {{key.cu.min}} on {{key.cu.min_date}}.")
    assert check_draft(draft, _pack()).ok


def test_failures_beyond_the_listed_ones_must_still_be_mentioned(tmp_path):
    rules = [{"table": "prices", "name": f"rule {i}", "expr": "price < 0"} for i in range(30)]
    log = run(setup(tmp_path, rules=rules))
    facts = json.loads((Path(log["db_path"]).parent / "facts.json").read_text())["facts"]
    assert facts["checks.not_listed"]["required"] is True
    assert facts["final.fail"]["value"] == 30 and facts["final.fail"]["required"] is True
    assert "more flagged check(s) are listed in validation.json" in log["summary_text"]
