"""Layer 6: the HTML report.

One self-contained file per run (outputs/<dataset>/report.html): no server, no
JavaScript library, charts are inline SVG. It opens offline and on GitHub Pages.

Order follows what a reader needs first:
  verdict (arrival -> after cleaning) -> summary -> checks -> cleaning
  -> gaps -> headline numbers -> column profiles -> lineage

With report.show_values: false the report shows quality results only (statuses,
counts, row numbers, dates, reasons) and no data values: for licensed data
that may not be published.
"""

from __future__ import annotations

import re
from datetime import date
from html import escape
from importlib import resources

from dqp.profiling.profiler import q
from dqp.report.charts import coverage_chart, line_chart, stage_bar

FONTS = ("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&"
         "family=Inter+Tight:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap")
STATUS_CLASS = {"passed": "pass", "passed with warnings": "warn", "failed": "fail"}
ATTENTION = ("FAIL", "ERROR", "WARN")


def e(v) -> str:
    return escape("" if v is None else str(v))


def pill(status: str, big: bool = False) -> str:
    s = str(status)
    cls = STATUS_CLASS.get(s.replace("_", " "), s.lower())
    return f'<span class="pill {cls}{" big" if big else ""}">{e(s.replace("_", " ").upper())}</span>'


def md_to_html(md: str) -> str:
    """Just enough markdown for the summary: ## headings, - bullets, **bold**, `code`."""
    def inline(t: str) -> str:
        t = e(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", t)

    out, in_list, para = [], False, []

    def flush():
        nonlocal para
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para = []

    for line in md.splitlines():
        s = line.strip()
        if s.startswith("- "):
            flush()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(s[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not s:
            flush()
        elif s.startswith("#"):
            flush()
            out.append(f"<h3>{inline(s.lstrip('#').strip())}</h3>")
        else:
            para.append(s)
    flush()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _fmt_example(ex: dict) -> str:
    return ", ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in ex.items())


# ---------------------------------------------------------------- sections

def _header(config, log: dict) -> str:
    loads = [l for l in log["loads"] if l["table"]]
    files = sorted({l["location"].split("/")[-1] for l in loads})
    started = log["started_at"].replace("T", " ").replace("+00:00", " UTC")
    desc = f'<p class="muted" style="max-width:70ch">{e(" ".join(config.description.split()))}</p>' \
        if config.description else ""
    return f"""
<header class="top">
  <span class="eyebrow">Data quality report</span>
  <h1>{e(config.dataset)}</h1>
  {desc}
  <div class="meta">
    <span>Run <b class="mono">{e(log['run_id'])}</b></span>
    <span><b>{e(started)}</b></span>
    <span><b>{len(loads)}</b> table(s) from <b>{len(files)}</b> file(s)</span>
    <span><b>{sum(l['rows'] for l in loads):,}</b> rows loaded</span>
  </div>
  <nav class="jump" aria-label="Sections">
    <a href="#summary">Summary</a><a href="#checks">Checks</a><a href="#cleaning">Cleaning</a>
    <a href="#gaps">Gaps</a><a href="#numbers">Headline numbers</a><a href="#columns">Columns</a>
    <a href="#lineage">Lineage</a>
  </nav>
</header>"""


def _verdict(log: dict) -> str:
    st = log["validation"]["stages"]

    def card(stage: str, title: str, final: bool) -> str:
        s = st[stage]
        status = s["status"].replace("_", " ")
        word = {"passed": "Passed", "passed with warnings": "Passed, with warnings", "failed": "Failed"}.get(status, status)
        tally = " · ".join(f"<b>{n}</b> {k.lower()}" for k, n in s["summary"].items() if n)
        return f"""
  <div class="stage{' final' if final else ''}">
    <span class="eyebrow">{title}</span>
    <div class="status-word {STATUS_CLASS.get(status, 'info')}">{e(word)}</div>
    {stage_bar(s['summary'])}
    <div class="tally">{tally} <span class="muted">of {sum(s['summary'].values())} checks</span></div>
  </div>"""
    rejected = sum(c["rows_rejected"] for c in log["cleaning"].values())
    return f"""
<section aria-label="Result">
  <div class="section-head"><h2>Result</h2></div>
  <div class="verdict">
    {card('raw', 'On arrival', False)}
    {card('staging', 'After cleaning', True)}
    <p class="verdict-note">Cleaning moved {rejected} row(s) to rejected tables, where they are kept as they were.
      The run is judged on the cleaned data, because that is what moves on to analysis.</p>
  </div>
</section>"""


def _summary(summary: dict) -> str:
    by = (f"Written by an LLM ({e(summary['model'])}), checked against the computed facts"
          if summary["source"] == "llm" else "Written by the built-in template")
    return f"""
<section id="summary">
  <div class="section-head"><h2>Summary</h2></div>
  <div class="summary">
    <div class="byline"><span>{by}.</span><span>{len(summary['facts_used'])} facts used; every number comes
      from <code>facts.json</code>, none was typed by the writer.</span></div>
    {md_to_html(re.sub(r'\A\s*## Summary\s*\n', '', summary['text']))}
  </div>
</section>"""


def _checks(checks: dict, show_values: bool) -> str:
    order = {"ERROR": 0, "FAIL": 1, "WARN": 2, "INFO": 3, "PASS": 4}
    rows = sorted(checks.values(), key=lambda c: (c["stage"] != "staging", order[c["status"]], c["table"]))
    attention = sum(c["status"] in ATTENTION for c in rows)
    body = []
    for c in rows:
        ex = ""
        if c["examples"] and c["status"] != "PASS":
            items = ([f"<span>{e(_fmt_example(x))}</span>" for x in c["examples"][:3]] if show_values
                     else [f"<span>row {e(x.get('_row', x.get('row', '?')))}</span>" for x in c["examples"][:3]
                           if "_row" in x or "row" in x] or ["<span>values hidden</span>"])
            ex = f'<div class="ex">{"".join(items)}</div>'
        stage = "after cleaning" if c["stage"] == "staging" else "on arrival"
        att = "1" if c["status"] in ATTENTION else "0"
        body.append(
            f'<tr data-stage="{c["stage"]}" data-att="{att}" data-text="{e((c["name"] + " " + c["table"]).lower())}">'
            f'<td>{pill(c["status"])}</td><td class="small">{stage}</td><td class="mono small">{e(c["table"])}</td>'
            f'<td class="name">{e(c["name"])}<div class="muted small">{e(c["detail"])}</div>{ex}</td>'
            f'<td class="num">{e(c["failing"])}</td><td class="num">{e(c["checked"] if c["checked"] is not None else "")}</td></tr>')
    default = "att" if attention else "all"
    # nothing to look at: keep the full list one click away instead of a wall of PASS rows
    opener = (f'<details><summary>All {len(rows)} checks passed. Show them</summary><div style="display:grid;gap:12px">'
              if not attention else "")
    closer = "</div></details>" if not attention else ""
    return f"""
<section id="checks">
  <div class="section-head"><h2>Checks</h2>
    <span class="muted small">{len(rows)} checks, {attention} need attention</span></div>
  {opener}<div class="filters" role="group" aria-label="Filter checks">
    <button class="chip" id="f-att" data-f="att" aria-pressed="{str(default == 'att').lower()}">Needs attention</button>
    <button class="chip" id="f-all" data-f="all" aria-pressed="{str(default == 'all').lower()}">All</button>
    <button class="chip" id="f-staging" data-f="staging" aria-pressed="false">After cleaning</button>
    <button class="chip" id="f-raw" data-f="raw" aria-pressed="false">On arrival</button>
    <input id="check-search" type="search" placeholder="Search checks or tables" aria-label="Search checks">
  </div>
  <div class="scroll"><table id="checks-table">
    <thead><tr><th>Status</th><th>Stage</th><th>Table</th><th>Check</th><th class="num">Failing</th>
      <th class="num">Checked</th></tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table></div>
  <p class="muted small" id="checks-empty" hidden>No checks match this filter.</p>{closer}
</section>"""


def _cleaning(con, cleaning: dict, report_cfg) -> str:
    cards = []
    steps_by_table: dict[str, list] = {}
    for st in cleaning["log"]:
        steps_by_table.setdefault(st["table"], []).append(st)
    for stg, info in cleaning["tables"].items():
        steps = [s for s in steps_by_table.get(stg, []) if s["action"] != "load_typed"]
        items = []
        for s in steps:
            counts = []
            if s["rows_removed"]:
                counts.append(f"{s['rows_removed']} row(s) removed")
            if s["values_changed"]:
                counts.append(f"{s['values_changed']} value(s) changed")
            items.append(f'<li><span class="mono">{e(s["action"])}</span> {e(s["details"])}'
                         + (f' <strong>{e(", ".join(counts))}</strong>' if counts else "")
                         + f'<div class="reason">Reason: {e(s["reason"])}</div></li>')
        rejected = ""
        if info["rows_rejected"] and report_cfg.max_rejected_rows:
            cur = con.execute(f"SELECT * FROM {q(info['rejected_table'])} ORDER BY _row "
                              f"LIMIT {report_cfg.max_rejected_rows}")
            names = [d[0] for d in cur.description]
            data = cur.fetchall()
            keep = ["_step", "_action", "_row"] + ([n for n in names if not n.startswith("_")][:5]
                                                   if report_cfg.show_values else [])
            idx = [names.index(n) for n in keep]
            head = "".join(f"<th>{e(n)}</th>" for n in keep)
            trs = "".join("<tr>" + "".join(f'<td class="mono small">{e(_cut(r[i]))}</td>' for i in idx) + "</tr>"
                          for r in data)
            rejected = (f'<details><summary>Rejected rows ({info["rows_rejected"]}), as they were in the source'
                        f'</summary><div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{trs}'
                        f'</tbody></table></div></details>')
        flow = (f'<div class="flow"><span class="box">{e(info["raw_table"])} · {info["rows_raw"]:,}</span>→'
                f'<span class="box">{e(stg)} · {info["rows_staging"]:,}</span>'
                + (f'<span class="rej">+ {info["rows_rejected"]:,} in {e(info["rejected_table"])}</span>'
                   if info["rows_rejected"] else "") + "</div>")
        body = (f'<ol class="steps">{"".join(items)}</ol>' if items
                else '<p class="muted small">No cleaning steps: typed copy of the raw table.</p>')
        cards.append(f'<div class="tablecard"><h3 class="mono">{e(stg)}</h3>{flow}{body}{rejected}</div>')
    marts = ""
    if cleaning["marts"]:
        mlog = [s for s in cleaning["log"] if s["action"] == "build_mart"]
        marts = ('<div class="tablecard"><h3>Marts</h3><ul class="steps">'
                 + "".join(f'<li><span class="mono">{e(s["table"])}</span> · {s["rows_after"]:,} rows'
                           f'<div class="reason">{e(s["reason"])}</div></li>' for s in mlog) + "</ul></div>")
    return f"""
<section id="cleaning">
  <div class="section-head"><h2>Cleaning</h2>
    <span class="muted small">raw → staging → mart; every step has a reason</span></div>
  {''.join(cards)}{marts}
</section>"""


def _gaps(coverage: dict, report_cfg) -> str:
    parts = []
    for t, cov in coverage.items():
        holes = sum(1 for v in cov["columns"].values() if v["internal_missing"])
        if not cov["date_column"]:
            continue
        chart = coverage_chart(cov, report_cfg.max_coverage_columns)
        if not chart:
            continue
        missing = (f'<p><strong>{cov["missing_period_count"]}</strong> {e(cov["frequency"])} period(s) missing from '
                   f'<code>{e(cov["date_column"])}</code>: <span class="mono small">'
                   f'{e(", ".join(cov["missing_periods"][:12]))}{" …" if cov["missing_period_count"] > 12 else ""}'
                   f'</span></p>' if cov["missing_period_count"] else
                   f'<p class="muted small">No missing {e(cov["frequency"] or "")} periods in '
                   f'<code>{e(cov["date_column"])}</code>.</p>')
        parts.append(f'<div class="tablecard"><h3 class="mono">{e(t)}</h3>{missing}'
                     f'<p class="small muted">{holes} column(s) with values missing inside their own range '
                     f'(red marks). Bars run from each column\'s first to its last value.</p>'
                     f'<div class="chartbox">{chart}</div></div>')
    body = "".join(parts) or '<p class="muted">No dated tables.</p>'
    return f"""
<section id="gaps">
  <div class="section-head"><h2>Gaps</h2><span class="muted small">documented, not filled</span></div>
  {body}
</section>"""


def _numbers(con, facts, stg_profiles: dict, report_cfg) -> str:
    head = '<div class="section-head"><h2>Headline numbers</h2></div>'
    avail = facts["key.available"].value if "key.available" in facts else "no"
    if not report_cfg.show_values:
        return (f'<section id="numbers">{head}<p class="muted">Not shown: this dataset includes licensed data, '
                f'so the public report shows quality results only.</p></section>')
    if avail != "yes":
        return (f'<section id="numbers">{head}<p class="muted">Not shown: validation after cleaning failed, and '
                f'numbers are only reported from validated data.</p></section>')
    f = facts.facts
    cards = []
    for fid in f:
        if not (fid.startswith("key.") and fid.endswith(".latest")):
            continue
        base = fid.removesuffix(".latest")
        st = base.split(".")[1]
        table = f[f"key.{st}.table"].value
        col = f[f"{base}.name"].value
        date_col = stg_profiles[table].get("main_date_column")
        rows = con.execute(f"SELECT {q(date_col)}, {q(col)} FROM {q(table)} WHERE {q(col)} IS NOT NULL "
                           f"AND {q(date_col)} IS NOT NULL ORDER BY 1").fetchall()
        step = max(len(rows) // 600, 1)
        pts = [(r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])[:10]), float(r[1]))
               for r in rows[::step]]
        if rows and (not pts or pts[-1][0] != rows[-1][0]):
            pts.append((rows[-1][0], float(rows[-1][1])))
        from dqp.summary.checker import fmt
        val = lambda k: fmt(f[f"{base}.{k}"].value, f[f"{base}.{k}"].kind) if f"{base}.{k}" in f else "n/a"  # noqa: E731
        change = (f'<dt>Change over the year</dt><dd>{e(val("change_1y_pct"))} since {e(val("change_1y_from"))}</dd>'
                  if f"{base}.change_1y_pct" in f else "")
        cards.append(f"""
  <div class="key">
    <span class="eyebrow mono">{e(col)}</span>
    <div><span class="latest">{e(val('latest'))}</span> <span class="muted small">latest, {e(val('latest_date'))}</span></div>
    {line_chart(pts, col)}
    <dl><dt>Lowest</dt><dd>{e(val('min'))} ({e(val('min_date'))})</dd>
        <dt>Highest</dt><dd>{e(val('max'))} ({e(val('max_date'))})</dd>{change}</dl>
  </div>""")
    if not cards:
        return f'<section id="numbers">{head}<p class="muted">No headline columns configured.</p></section>'
    return f'<section id="numbers">{head}<div class="keys">{"".join(cards)}</div></section>'


def _columns(stg_profiles: dict, show_values: bool) -> str:
    blocks = []
    for t, p in stg_profiles.items():
        trs = []
        for col, c in p["columns_profile"].items():
            rng = ""
            if c.get("date"):
                rng = f'{c["date"]["min"]} → {c["date"]["max"]} ({c["date"]["frequency"]})'
            elif c.get("numeric") and show_values and c["numeric"]["count"]:
                rng = f'{c["numeric"]["min"]:,.4g} → {c["numeric"]["max"]:,.4g}'
            trs.append(f'<tr><td class="mono small">{e(col)}</td><td class="small">{e(c["inferred_type"].replace("_", " "))}</td>'
                       f'<td class="num">{c["null_share"]:.1%}</td><td class="num">{c["distinct_values"]:,}</td>'
                       f'<td class="num">{c["nonconforming"]}</td><td class="mono small">{e(rng)}</td></tr>')
        blocks.append(f'<details><summary><span class="mono">{e(t)}</span> · {p["rows"]:,} rows × {p["columns"]} '
                      f'columns</summary><div class="scroll"><table><thead><tr><th>Column</th><th>Type</th>'
                      f'<th class="num">Missing</th><th class="num">Distinct</th><th class="num">Unreadable</th>'
                      f'<th>Range</th></tr></thead><tbody>{"".join(trs)}</tbody></table></div></details>')
    return f"""
<section id="columns">
  <div class="section-head"><h2>Columns</h2><span class="muted small">profile of the cleaned tables and marts</span></div>
  {''.join(blocks)}
</section>"""


def _lineage(log: dict) -> str:
    rows = "".join(
        f'<tr><td class="mono small">{e(l["table"])}</td><td class="small">{e(l["location"].split("/")[-1])}'
        f'{(" · sheet " + e(l["sheet"])) if l["sheet"] else ""}</td><td class="num">{l["rows"]:,}</td>'
        f'<td class="mono small">{e((l["sha256"] or "")[:12])}</td><td class="small muted">{e("; ".join(l["notes"]))}</td></tr>'
        for l in log["loads"] if l["table"])
    return f"""
<section id="lineage">
  <div class="section-head"><h2>Lineage</h2><span class="muted small">which file each table came from</span></div>
  <div class="scroll"><table><thead><tr><th>Raw table</th><th>Source</th><th class="num">Rows</th>
    <th>File SHA-256</th><th>Load notes</th></tr></thead><tbody>{rows}</tbody></table></div>
</section>"""


SCRIPT = """
<script>
(function () {
  var rows = Array.prototype.slice.call(document.querySelectorAll('#checks-table tbody tr'));
  var chips = Array.prototype.slice.call(document.querySelectorAll('.filters .chip'));
  var search = document.getElementById('check-search');
  var empty = document.getElementById('checks-empty');
  var current = (chips.filter(function (c) { return c.getAttribute('aria-pressed') === 'true'; })[0] || chips[1]).dataset.f;
  function apply() {
    var term = (search.value || '').toLowerCase().trim(), shown = 0;
    rows.forEach(function (r) {
      var ok = current === 'all' || (current === 'att' ? r.dataset.att === '1' : r.dataset.stage === current);
      if (ok && term) ok = r.dataset.text.indexOf(term) !== -1;
      r.hidden = !ok; if (ok) shown++;
    });
    empty.hidden = shown !== 0;
  }
  chips.forEach(function (c) {
    c.addEventListener('click', function () {
      current = c.dataset.f;
      chips.forEach(function (x) { x.setAttribute('aria-pressed', String(x === c)); });
      apply();
    });
  });
  search.addEventListener('input', apply);
  apply();
})();
</script>"""


_SECTION = re.compile(r'(<section\b[^>]*>\s*<div class="section-head">.*?</div>)(.*?)(</section>)', re.S)


def grid_sections(html: str) -> str:
    """Put each section's content in the right-hand column of the grid."""
    return _SECTION.sub(lambda m: f'{m.group(1)}<div class="section-body">{m.group(2)}</div>{m.group(3)}', html)


def _cut(v, width: int = 60) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= width else s[: width - 1] + "…"


def build_report(con, config, log: dict, checks: dict, cleaning: dict, coverage: dict,
                 stg_profiles: dict, facts, summary: dict, standalone: bool = True) -> str:
    css = resources.files("dqp.report").joinpath("style.css").read_text(encoding="utf-8")
    rc = config.report
    title = f"{config.dataset.replace('_', ' ').title()} Data Quality"
    head = (f'<title>{e(title)}</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n'
            f'<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            f'<link rel="stylesheet" href="{FONTS}">\n<style>\n{css}\n</style>')
    sections = "".join([
        _verdict(log), _summary(summary), _checks(checks, rc.show_values), _cleaning(con, cleaning, rc),
        _gaps(coverage, rc), _numbers(con, facts, stg_profiles, rc), _columns(stg_profiles, rc.show_values),
        _lineage(log),
    ])
    body = (f'<main class="page">{_header(config, log)}{grid_sections(sections)}'
            f'<footer><span>Generated by dq-pipeline {e(log["dqp_version"])} · config '
            f'<code>{e(log["config_sha256"])}</code> · run <code>{e(log["run_id"])}</code></span>'
            f'<span>Raw data is never changed; removed rows are kept in rejected_ tables; gaps are documented, '
            f'not filled.</span></footer></main>{SCRIPT}')
    if not standalone:
        return head + "\n" + body
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f'{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n')
