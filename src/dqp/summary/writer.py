"""Layer 5: write the plain-language summary.

  1. Validation after cleaning failed?  -> template only. The LLM is not asked
     to describe data that did not pass validation.
  2. No provider / no API key?          -> template.
  3. Otherwise ask the LLM for a draft with {{fact}} placeholders, check it,
     and on violations retry with the checker's feedback (max_retries).
     Still failing, or the API errors?  -> template, and the reason is logged.

The result records which path was taken, every rejected draft and why, and the
fact IDs the final text uses, so a reader can trace each number.
"""

from __future__ import annotations

import json

from dqp.summary.checker import check_draft, render
from dqp.summary.facts import FactPack
from dqp.summary.llm import LLMClient, LLMError
from dqp.summary.template import template_draft

SYSTEM = """You write the data-quality summary for an automated data pipeline.
Your reader is a busy analyst or manager. Plain English, short sentences.

Hard rules, checked by code after you answer:
1. You never write a number yourself: no digits and no number words (two, half,
   doubled, percent ...). Every number, date, count, status, name with digits or
   reason comes from a fact, written as its placeholder, e.g. {{final.fail}}.
   The pipeline replaces placeholders with the real values.
2. Use only placeholders from the fact list. Unknown IDs are rejected.
3. Mention every fact marked REQUIRED. A summary may not hide a failure,
   a removed row or a documented gap by leaving it out.
4. Say only what the facts support. Do not guess causes, do not judge
   the data as good or bad beyond what the validation status says.
5. Use bullet lists with "-", never numbered lists. Table and column names
   go in backticks, preferably as placeholders.

Format: markdown with exactly these sections:
## Summary          (what was loaded, validation on arrival and after cleaning)
## Data quality     (what failed or warned, what cleaning changed and why, gaps)
## Key findings     (headline numbers, only if key.available is yes)
"""


def _prompt(facts: FactPack, max_words: int, feedback: list[str] | None) -> str:
    lines = []
    for f in facts.facts.values():
        value = json.dumps(f.value, default=str) if not isinstance(f.value, str) else f.value
        lines.append(f"{{{{{f.id}}}}} = {value}   [{f.kind}] {f.desc}" + ("   REQUIRED" if f.required else ""))
    text = (f"Write the summary in at most {max_words} words.\n\n"
            "Facts (placeholder = value [kind] description):\n" + "\n".join(lines))
    if feedback:
        text += ("\n\nYour previous draft was rejected by the checker. Fix exactly these problems "
                 "and return the full corrected summary:\n" + "\n".join(f"- {v}" for v in feedback))
    return text


def identifiers_of(con, tables: list[str]) -> set[str]:
    """Table and column names that may appear in code spans even if they contain digits."""
    out = set()
    for t in tables:
        out.add(t)
        for p in ("raw_", "stg_"):
            out.add(t.removeprefix(p))
        out.update(r[0] for r in con.execute(f'DESCRIBE "{t}"').fetchall())
    return out


def write_summary(facts: FactPack, cfg, client: LLMClient | None, why_no_client: str,
                  identifiers: set[str]) -> dict:
    result = {"source": "template", "reason": "", "model": None, "attempts": [], "facts_used": []}

    if facts["final.status"].value == "failed":
        result["reason"] = ("validation after cleaning failed: the LLM summary only runs on validated data, "
                            "so the built-in template reports the failures")
    elif client is None:
        result["reason"] = f"no LLM used: {why_no_client}"
    else:
        result["model"] = f"{client.provider}:{client.model}"
        feedback = None
        for attempt in range(1 + cfg.max_retries):
            try:
                draft = client.complete(SYSTEM, _prompt(facts, cfg.max_words, feedback))
            except LLMError as exc:
                result["attempts"].append({"attempt": attempt + 1, "error": str(exc)})
                result["reason"] = f"LLM call failed: {exc}"
                break
            outcome = check_draft(draft, facts, identifiers)
            result["attempts"].append({"attempt": attempt + 1, "ok": outcome.ok,
                                       "violations": outcome.violations, "draft": draft})
            if outcome.ok:
                result.update(source="llm", reason=f"LLM draft passed the number check "
                                                  f"(attempt {attempt + 1})",
                              draft=draft, facts_used=outcome.used, text=render(draft, facts))
                return result
            feedback = outcome.violations[:20]
        else:
            result["reason"] = (f"LLM drafts failed the number check {len(result['attempts'])} time(s); "
                                "using the template")

    draft = template_draft(facts)
    outcome = check_draft(draft, facts, identifiers)
    if not outcome.ok:  # the template must always pass its own check: anything else is a bug
        raise AssertionError("template failed the number check: " + "; ".join(outcome.violations[:5]))
    result.update(draft=draft, facts_used=outcome.used, text=render(draft, facts))
    return result
