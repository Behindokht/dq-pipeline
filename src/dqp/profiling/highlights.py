"""Turn the profile facts into short plain-language highlights.

These are deterministic sentences built from the computed numbers, with no LLM
involved. They tell a human where to look first. Each highlight carries the fact
IDs it was built from, so it can be traced back.

Highlights are observations, not verdicts: whether 48 missing months is a problem
depends on the dataset, and that judgement belongs to validation rules (layer 3).
"""

from __future__ import annotations


def _fmt_examples(examples: list[dict], key: str = "value", limit: int = 2) -> str:
    shown = []
    for e in examples[:limit]:
        val = str(e[key])
        val = val if len(val) <= 40 else val[:37] + "..."
        shown.append(f"row {e['row']}: {val!r}" if "row" in e else repr(val))
    return "; ".join(shown)


def build_highlights(table: str, prof: dict) -> list[dict]:
    out: list[dict] = []

    def add(level: str, text: str, *facts: str):
        out.append({"table": table, "level": level, "text": text,
                    "facts": [f"tables.{table}.{f}" for f in facts]})

    cols = prof["columns_profile"]

    if prof["duplicate_rows"]:
        add("attention", f"{prof['duplicate_rows']} exact duplicate row(s)", "duplicate_rows")

    for name, c in cols.items():
        base = f"columns_profile.{name}"
        kind = c["inferred_type"]
        if kind == "empty":
            add("info", f"column {name!r} is completely empty", f"{base}.inferred_type")
            continue
        if c["nonconforming"]:
            expected = c.get("mixed_dominant_type", kind).replace("numeric_decimal_comma", "numeric")
            add("attention",
                f"{name!r}: {c['nonconforming']} value(s) don't look {expected} "
                f"({_fmt_examples(c['nonconforming_examples'])})",
                f"{base}.nonconforming")
        if kind == "mixed":
            add("attention", f"{name!r} mixes types: only {c['type_share']:.0%} of values look "
                             f"{c['mixed_dominant_type']}", f"{base}.type_share")
        if kind == "numeric_decimal_comma":
            add("info", f"{name!r} uses a decimal comma (e.g. 1,50)", f"{base}.inferred_type")
        if c["whitespace_values"]:
            add("info", f"{name!r}: {c['whitespace_values']} value(s) with leading/trailing spaces",
                f"{base}.whitespace_values")

        d = c.get("date")
        if d:
            if d.get("ambiguous_day_month"):
                add("attention", f"{name!r}: dates like 03/04/2024 could be day-first or month-first",
                    f"{base}.date.ambiguous_day_month")
            if len(d.get("formats_used", {})) > 1:
                add("info", f"{name!r} mixes date formats: {', '.join(d['formats_used'])}",
                    f"{base}.date.formats_used")
            if d["duplicate_dates"] and name == prof["main_date_column"]:
                add("attention", f"{name!r}: {d['duplicate_dates']} repeated date(s)",
                    f"{base}.date.duplicate_dates")
            if d.get("missing_periods"):
                gap = d.get("largest_gap") or {}
                extra = (f"; largest gap {gap['missing_periods']} period(s) between {gap['after']} and {gap['before']}"
                         if gap else "")
                add("info", f"{name!r} ({d['frequency']}, {d['min']} to {d['max']}): "
                            f"{d['missing_periods']} of {d['expected_periods']} periods missing{extra}",
                    f"{base}.date.missing_periods", f"{base}.date.expected_periods")

    # Mostly-empty and outlier columns are summarised, not listed one by one,
    # so a 186-column sheet doesn't produce 186 lines.
    sparse = sorted(((c["null_share"], n) for n, c in cols.items()
                     if c["inferred_type"] != "empty" and c["null_share"] >= 0.5), reverse=True)
    if sparse:
        names = ", ".join(f"{n!r} ({share:.0%})" for share, n in sparse[:3])
        more = f" and {len(sparse) - 3} more" if len(sparse) > 3 else ""
        add("info", f"{len(sparse)} column(s) are at least half empty: {names}{more}",
            *[f"columns_profile.{n}.null_share" for _, n in sparse])

    with_outliers = sorted(((c["numeric"]["outliers"], n) for n, c in cols.items()
                            if c.get("numeric", {}).get("outliers")), reverse=True)
    if with_outliers:
        names = ", ".join(f"{n!r} ({k})" for k, n in with_outliers[:3])
        more = f" and {len(with_outliers) - 3} more" if len(with_outliers) > 3 else ""
        add("info", f"{len(with_outliers)} numeric column(s) have outliers (IQR rule): {names}{more}",
            *[f"columns_profile.{n}.numeric.outliers" for _, n in with_outliers])

    if prof["constant_columns"]:
        names = ", ".join(repr(c) for c in prof["constant_columns"][:3])
        add("info", f"{len(prof['constant_columns'])} column(s) hold a single value: {names}", "constant_columns")
    return out
