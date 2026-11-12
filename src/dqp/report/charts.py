"""Small inline-SVG charts for the report. No JavaScript library: the report is
one self-contained file that opens offline and on GitHub Pages.

Colors come from CSS classes defined in the report's stylesheet, so every
chart follows the light/dark theme."""

from __future__ import annotations

from datetime import date
from html import escape

STATUS_ORDER = ("PASS", "INFO", "WARN", "FAIL", "ERROR")


def _num(v: float) -> str:
    return f"{v:.1f}"


def stage_bar(summary: dict, width: int = 100) -> str:
    """Stacked bar of check statuses, as percentages of the stage's checks."""
    total = sum(summary.values()) or 1
    parts, x = [], 0.0
    for s in STATUS_ORDER:
        n = summary.get(s, 0)
        if not n:
            continue
        w = n / total * width
        parts.append(f'<rect class="seg-{s.lower()}" x="{_num(x)}" y="0" width="{_num(max(w, 0.6))}" height="8">'
                     f'<title>{n} {s}</title></rect>')
        x += w
    return (f'<svg class="stagebar" viewBox="0 0 {width} 8" preserveAspectRatio="none" role="img" '
            f'aria-label="check results">{"".join(parts)}</svg>')


def _years(d0: date, d1: date) -> list[int]:
    span = max(d1.year - d0.year, 1)
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50) if span / s <= 8)
    first = (d0.year // step + 1) * step
    return list(range(first, d1.year + 1, step))


def _to_date(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def coverage_chart(cov: dict, max_cols: int) -> str:
    """One row per column: a bar from its first to its last value, with a red tick
    at every date inside that span where the value is missing."""
    cols = [(c, v) for c, v in cov["columns"].items() if v["first"]]
    if not cols:
        return ""
    # columns with holes first, then the rest, keep it readable on wide tables
    cols.sort(key=lambda cv: (cv[1]["internal_missing"] == 0,))
    shown, hidden = cols[:max_cols], len(cols) - max_cols
    d0 = min(_to_date(v["first"]) for _, v in cols)
    d1 = max(_to_date(v["last"]) for _, v in cols)
    if d1 <= d0:
        return ""
    label_w, plot_w, row_h, top = 190, 560, 16, 22
    width = label_w + plot_w + 10
    height = top + row_h * len(shown) + 6

    def x(d: date) -> float:
        return label_w + (d - d0).days / (d1 - d0).days * plot_w

    out = [f'<svg class="coverage" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="coverage of each column over time">']
    for y in _years(d0, d1):
        xx = x(date(y, 1, 1))
        out.append(f'<line class="grid" x1="{_num(xx)}" y1="{top - 4}" x2="{_num(xx)}" y2="{height - 4}"/>'
                   f'<text class="axis" x="{_num(xx)}" y="12" text-anchor="middle">{y}</text>')
    for i, (col, v) in enumerate(shown):
        y = top + i * row_h
        name = col if len(col) <= 26 else col[:24] + "…"
        out.append(f'<text class="label" x="{label_w - 8}" y="{y + 11}" text-anchor="end">'
                   f'<title>{escape(col)}</title>{escape(name)}</text>')
        x1, x2 = x(_to_date(v["first"])), x(_to_date(v["last"]))
        out.append(f'<rect class="span" x="{_num(x1)}" y="{y + 3}" width="{_num(max(x2 - x1, 1.5))}" height="9" rx="2">'
                   f'<title>{escape(col)}: {v["values"]} values, {v["first"]} to {v["last"]}</title></rect>')
        for d in v["internal_missing_dates"]:
            xx = x(_to_date(d))
            out.append(f'<rect class="hole" x="{_num(xx - 1)}" y="{y + 1}" width="2" height="13">'
                       f'<title>{escape(col)}: missing on {d}</title></rect>')
    out.append("</svg>")
    note = (f'<p class="muted small">{hidden} more column(s) without gaps not drawn.</p>' if hidden > 0 else "")
    return "".join(out) + note


def line_chart(points: list[tuple[date, float]], label: str) -> str:
    """Line with area fill, faint grid, and the latest, lowest and highest points marked."""
    if len(points) < 2:
        return ""
    W, H, L, R, T, B = 360, 150, 46, 10, 12, 22
    d0, d1 = points[0][0], points[-1][0]
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    span_days = max((d1 - d0).days, 1)

    def px(d):
        return L + (d - d0).days / span_days * (W - L - R)

    def py(v):
        return T + (1 - (v - lo) / (hi - lo)) * (H - T - B)

    path = " ".join(f"{'M' if i == 0 else 'L'}{_num(px(d))},{_num(py(v))}" for i, (d, v) in enumerate(points))
    area = f"{path} L{_num(px(d1))},{_num(py(lo))} L{_num(px(d0))},{_num(py(lo))} Z"
    i_lo = min(range(len(points)), key=lambda i: (vals[i], i))
    i_hi = max(range(len(points)), key=lambda i: (vals[i], -i))
    out = [f'<svg class="line" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)} over time">']
    for k in range(3):
        v = lo + (hi - lo) * k / 2
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{_num(py(v))}" y2="{_num(py(v))}"/>'
                   f'<text class="axis" x="{L - 6}" y="{_num(py(v) + 3)}" text-anchor="end">{_short(v)}</text>')
    out.append(f'<text class="axis" x="{L}" y="{H - 6}">{d0.isoformat()[:7]}</text>'
               f'<text class="axis" x="{W - R}" y="{H - 6}" text-anchor="end">{d1.isoformat()[:7]}</text>')
    out.append(f'<path class="area" d="{area}"/><path class="stroke" d="{path}"/>')
    for i, cls in ((i_lo, "lo"), (i_hi, "hi"), (len(points) - 1, "end")):
        d, v = points[i]
        out.append(f'<circle class="pt-{cls}" cx="{_num(px(d))}" cy="{_num(py(v))}" r="3.2">'
                   f'<title>{d}: {v:,.4g}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def _short(v: float) -> str:
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 1e3:
        return f"{v / 1e3:.1f}k"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.1f}"
    return f"{v:.2g}"
