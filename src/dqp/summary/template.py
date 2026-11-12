"""The built-in summary: a fixed template filled with fact placeholders.

Used when no LLM is configured, when the LLM's draft fails the number check,
and always when validation after cleaning failed. It goes through the same
checker as an LLM draft, so the guarantee is the same either way.
"""

from __future__ import annotations

import re

from dqp.summary.facts import FactPack


def template_draft(facts: FactPack) -> str:
    f = facts.facts
    has = lambda fid: fid in f  # noqa: E731
    out = ["## Summary", ""]
    out.append("Dataset **{{dataset.name}}**: {{dataset.tables}} table(s) and {{dataset.raw_rows}} rows "
               "loaded from {{dataset.sources}}.")
    out.append("")
    out.append("- On arrival, validation **{{arrival.status}}**: {{arrival.pass}} passed, {{arrival.warn}} warning(s), "
               "{{arrival.fail}} failure(s) out of {{arrival.checks}} checks.")
    out.append("- Cleaning made {{clean.changes}} change(s) to rows or values and moved {{clean.rejected_rows}} "
               "row(s) to rejected tables, where they are kept.")
    out.append("- After cleaning, validation **{{final.status}}**: {{final.pass}} passed, {{final.warn}} warning(s), "
               "{{final.fail}} failure(s), {{final.error}} error(s) out of {{final.checks}} checks. "
               "This is the result that counts.")

    checks = sorted({fid.split(".")[0] for fid in f if fid.startswith("check") and fid != "checks.not_listed"},
                    key=lambda c: int(c.removeprefix("check")))
    if checks:
        out += ["", "## Checks that did not pass", ""]
        for c in checks:
            out.append(f"- **{{{{{c}.status}}}}** ({{{{{c}.stage}}}}) in `{{{{{c}.table}}}}`: {{{{{c}.name}}}}. "
                       f"{{{{{c}.detail}}}}.")
        if has("checks.not_listed"):
            out.append("- {{checks.not_listed}} more flagged check(s) are listed in validation.json.")

    steps = sorted({fid.split(".")[0] for fid in f if fid.startswith("clean") and fid[5:6].isdigit()},
                   key=lambda c: int(c.removeprefix("clean")))
    if steps:
        out += ["", "## What cleaning changed", ""]
        for s in steps:
            out.append(f"- `{{{{{s}.table}}}}`, {{{{{s}.action}}}}: {{{{{s}.rows_removed}}}} row(s) removed, "
                       f"{{{{{s}.values_changed}}}} value(s) changed. Reason: {{{{{s}.reason}}}}.")

    gap_tables = sorted({fid.split(".")[1] for fid in f if fid.startswith("gap.")})
    if gap_tables:
        out += ["", "## Gaps (documented, not filled)", ""]
        for t in gap_tables:
            if has(f"gap.{t}.missing_periods"):
                out.append(f"- `{{{{gap.{t}.table}}}}`: {{{{gap.{t}.missing_periods}}}} period(s) missing from the date column: "
                           f"{{{{gap.{t}.missing_list}}}}.")
            if has(f"gap.{t}.columns_with_holes"):
                cols = sorted({fid.split(".")[2] for fid in f
                               if re.fullmatch(rf"gap\.{re.escape(t)}\.col\d+\.name", fid)},
                              key=lambda c: int(c.removeprefix("col")))
                listed = "; ".join(f"`{{{{gap.{t}.{c}.name}}}}` ({{{{gap.{t}.{c}.dates}}}})" for c in cols)
                out.append(f"- `{{{{gap.{t}.table}}}}`: {{{{gap.{t}.columns_with_holes}}}} column(s) have values "
                           f"missing inside their own date range: {listed}.")

    keys = [fid.removesuffix(".latest") for fid in f if fid.startswith("key.") and fid.endswith(".latest")]
    out += ["", "## Headline numbers", ""]
    if f["key.available"].value != "yes":
        out.append("Not given: validation after cleaning failed, and numbers are only reported from validated data.")
    elif not keys:
        out.append("No headline columns configured (see `summary.key_columns`).")
    else:
        for k in keys:
            line = (f"- `{{{{{k}.name}}}}`: latest {{{{{k}.latest}}}} ({{{{{k}.latest_date}}}}); lowest "
                    f"{{{{{k}.min}}}} ({{{{{k}.min_date}}}}), highest {{{{{k}.max}}}} ({{{{{k}.max_date}}}}) "
                    f"since {{{{{k}.first_date}}}}")
            if has(f"{k}.change_1y_pct"):
                line += f"; change over the year since {{{{{k}.change_1y_from}}}}: {{{{{k}.change_1y_pct}}}}"
            out.append(line + ".")
    return "\n".join(out) + "\n"
