"""Checks a summary draft against the fact pack, then renders it.

A draft is markdown in which every number is a placeholder:  {{final.fail}}.
The check fails if the draft:
  - uses a placeholder that is not a fact                 (invented fact)
  - contains a digit outside a placeholder                (a number the writer made up)
  - contains a number word such as "three", "half", "doubled"
  - leaves out a required fact (a failure, removed rows, a documented gap):
    a summary must not be able to hide a problem by not mentioning it
  - claims a cause ("because", "due to", "driven by"): the facts hold no causes,
    so any explanation would be invented
  - puts a headline number in a sentence without its role word, e.g. the
    lowest value in a sentence that never says low/lowest/minimum
    (catches "copper is now at {{key.cu.min}}")

What it cannot check: whether a sentence that has the right role word is still
misleading, or tone. Those limits are stated in the README.

Allowed exceptions, because they are not claims about the data:
  - ordered-list markers at the start of a line ("1. ")
  - code spans naming a table or column the pipeline knows (`ratio_ma12`)

render() then replaces every placeholder with its value, formatted the same way
every time. The same check runs on LLM drafts and on the built-in template.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from dqp.summary.facts import FactPack

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
NUMBER_WORDS = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|hundreds|"
    r"thousand|thousands|million|millions|billion|billions|dozen|dozens|half|halved|twice|double|doubled|"
    r"triple|tripled|quadrupled|percent|percentage|zero|once|third|thirds|fifth|fifths|tenth|tenths|"
    r"a quarter of|quarters of|majority|minority)\b", re.IGNORECASE)
CAUSAL = re.compile(
    r"\b(because|due to|driven by|caused by|causes|owing to|as a result of|thanks to|led to|leads to|"
    r"attributable to|in response to|on the back of|explained by|stems from|resulted from)\b", re.IGNORECASE)
# a headline-number placeholder must share its sentence with one of its role words
ROLE_WORDS = {
    "latest": r"latest|now|current|currently|most recent|last|recent",
    "min": r"low|lowest|minimum|bottom|trough|least",
    "max": r"high|highest|peak|maximum|record|top",
    "change_1y_pct": r"change|changed|rose|risen|rise|fell|fallen|fall|up|down|increase|increased|decrease|"
                     r"decreased|gain|gained|loss|lost|grew|grown|dropped|drop|over the year|year",
}
ROLE_OF = {"latest": "latest", "latest_date": "latest", "min": "min", "min_date": "min",
           "max": "max", "max_date": "max", "change_1y_pct": "change_1y_pct", "change_1y_from": "change_1y_pct"}
LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
CODE_SPAN = re.compile(r"`([^`\n]+)`")


@dataclass
class CheckOutcome:
    ok: bool
    violations: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)


def check_draft(draft: str, facts: FactPack, identifiers: set[str] | None = None) -> CheckOutcome:
    identifiers = identifiers or set()
    violations: list[str] = []

    used = PLACEHOLDER.findall(draft)
    for fid in dict.fromkeys(used):
        if fid not in facts:
            violations.append(f"unknown fact id {{{{{fid}}}}}")

    text = PLACEHOLDER.sub(" ", draft)
    text = LIST_MARKER.sub(" ", text)

    def code(m: re.Match) -> str:
        span = m.group(1).strip()
        return " " if span in identifiers or not re.search(r"\d", span) else f" {span} "
    text = CODE_SPAN.sub(code, text)

    for m in re.finditer(r"\S*\d\S*", text):
        violations.append(f"number written without a fact placeholder: {_context(text, m)!r}")
    for m in NUMBER_WORDS.finditer(text):
        violations.append(f"number word {m.group(0)!r}: use a fact placeholder instead ({_context(text, m)!r})")

    for m in CAUSAL.finditer(text):
        violations.append(f"causal claim {m.group(0)!r}: the facts contain no causes ({_context(text, m)!r})")
    violations.extend(_role_violations(draft, facts))

    used_set = set(used)
    missing = [fid for fid in facts.required() if fid not in used_set]
    for fid in missing:
        violations.append(f"required fact not mentioned: {{{{{fid}}}}} ({facts[fid].desc})")

    return CheckOutcome(ok=not violations, violations=violations, used=sorted(used_set & set(facts.facts)),
                        missing_required=missing)


def _role_violations(draft: str, facts: FactPack) -> list[str]:
    """Headline numbers (key.*) must appear in a sentence that names their role."""
    out = []
    marked = PLACEHOLDER.sub(lambda m: f"\x00{m.group(1).replace('.', '\x01')}\x00", draft)
    for sentence in re.split(r"(?<=[.!?;])\s+|\n", marked):
        for raw in re.findall(r"\x00([^\x00]+)\x00", sentence):
            fid = raw.replace("\x01", ".")
            if not fid.startswith("key.") or fid not in facts:
                continue
            role = ROLE_OF.get(fid.rsplit(".", 1)[-1])
            if role is None:
                continue
            plain = re.sub(r"\x00[^\x00]+\x00", " ", sentence)
            if not re.search(rf"\b({ROLE_WORDS[role]})\b", plain, re.IGNORECASE):
                out.append(f"{{{{{fid}}}}} is a {role.replace('_1y_pct', ' over one year')} value but its sentence "
                           f"does not say so: {' '.join(plain.split())[:80]!r}")
    return out


def _context(text: str, m: re.Match, width: int = 30) -> str:
    a, b = max(m.start() - width, 0), min(m.end() + width, len(text))
    return " ".join(text[a:b].split())


def fmt(value, kind: str) -> str:
    """One formatting rule for every number in every report."""
    if value is None:
        return "n/a"
    if kind == "percent":
        return f"{value:+.1f}%"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if math.isnan(value):
            return "n/a"
        if value == int(value) and abs(value) < 1e15:
            return f"{int(value):,}"
        a = abs(value)
        if a >= 100:
            return f"{value:,.1f}"
        if a >= 1:
            return f"{value:,.2f}"
        return f"{value:.4g}"
    return str(value)


def render(draft: str, facts: FactPack) -> str:
    return PLACEHOLDER.sub(lambda m: fmt(facts[m.group(1)].value, facts[m.group(1)].kind), draft)
