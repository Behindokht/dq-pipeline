"""Build the static site for GitHub Pages from finished runs.

    dqp site --out site outputs/copper outputs/payments

writes
    site/index.html               one card per dataset: verdict, counts, link
    site/<dataset>/index.html     that run's report.html
    site/.nojekyll                serve files as they are

Only files the pipeline already produced are used; nothing is recomputed, so the
site shows exactly what the run found.
"""

from __future__ import annotations

import json
import shutil
from html import escape
from importlib import resources
from pathlib import Path

from dqp.report.html import FONTS, grid_sections, pill

STEPS = [
    ("Load", "CSV and Excel into DuckDB, every value kept as it arrived"),
    ("Profile", "types, values that don't fit, missing values, duplicates, outliers, date gaps"),
    ("Validate on arrival", "generic checks plus rules from the dataset's config"),
    ("Clean", "logged steps with reasons; removed rows kept; gaps documented, not filled"),
    ("Validate after cleaning", "the result that counts"),
    ("Summarise", "plain language; every number comes from a computed fact, none is typed by the writer"),
]

EXTRA_CSS = """
.lead { font-size: 1.15rem; line-height: 1.5; max-width: 52ch; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); column-gap: 32px; row-gap: 40px; }
.card { display: grid; gap: 12px; align-content: start; min-width: 0; border-top: 1px solid var(--rule); padding-top: 12px; }
.card h3 { font: 700 1.6rem/1.05 var(--font-head); letter-spacing: -0.02em; }
.card .route { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; font-size: 0.82rem; }
.card .open { font-weight: 600; }
.card dl { display: grid; grid-template-columns: auto 1fr; gap: 3px 14px; margin: 0; font-size: 0.8rem; }
.card dt { color: var(--muted); }
.card dd { margin: 0; font-variant-numeric: tabular-nums; }
.how { margin: 0; padding-left: 1.6em; display: grid; gap: 8px; max-width: 68ch; }
.how li::marker { color: var(--muted); font-variant-numeric: tabular-nums; }
.how b { font-weight: 600; }
"""


def _e(v) -> str:
    return escape("" if v is None else str(v))


def _card(slug: str, log: dict, facts: dict) -> str:
    st = log["validation"]["stages"]
    loads = [l for l in log["loads"] if l["table"]]
    desc = facts.get("dataset.description", {}).get("value", "")
    if len(desc) > 220:
        desc = desc[:217].rsplit(" ", 1)[0] + "…"
    rejected = sum(c["rows_rejected"] for c in log.get("cleaning", {}).values())
    final = st["staging"]["summary"]
    attention = final.get("FAIL", 0) + final.get("ERROR", 0) + final.get("WARN", 0)
    by = "LLM, checked" if log.get("summary", {}).get("source") == "llm" else "template"
    shown = "quality only (licensed data)" if facts.get("key.available", {}).get("value") == "hidden" else "full"
    return f"""
<article class="card">
  <h3>{_e(log['dataset'])}</h3>
  <p class="muted small">{_e(desc)}</p>
  <div class="route"><span class="small muted">on arrival</span>{pill(st['raw']['status'])}
    <span class="muted" aria-hidden="true">→</span><span class="small muted">after cleaning</span>{pill(st['staging']['status'])}</div>
  <dl>
    <dt>Rows loaded</dt><dd>{sum(l['rows'] for l in loads):,} in {len(loads)} table(s)</dd>
    <dt>Checks after cleaning</dt><dd>{sum(final.values())}, {attention} need attention</dd>
    <dt>Rows set aside</dt><dd>{rejected}</dd>
    <dt>Summary written by</dt><dd>{by}</dd>
    <dt>Report</dt><dd>{shown}</dd>
    <dt>Run</dt><dd class="mono">{_e(log['started_at'][:16].replace('T', ' '))} UTC</dd>
    <dt>Built by</dt><dd>{"a local run (data not in the repo)" if log.get("snapshot") else "GitHub Actions"}</dd>
  </dl>
  <a class="open" href="{_e(slug)}/">Open report →</a>
</article>"""


def build_site(out: str | Path, run_dirs: list[str | Path], repo_url: str | None = None) -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cards = []
    for d in map(Path, run_dirs):
        log_file, report = d / "run_log.json", d / "report.html"
        if not log_file.exists() or not report.exists():
            raise FileNotFoundError(f"{d}: no finished run (run_log.json and report.html are needed)")
        log = json.loads(log_file.read_text(encoding="utf-8"))
        if log.get("status") != "success":
            raise ValueError(f"{d}: the last run did not finish ({log.get('error', 'unknown error')})")
        facts_file = d / "facts.json"
        facts = json.loads(facts_file.read_text(encoding="utf-8"))["facts"] if facts_file.exists() else {}
        slug = log["dataset"]
        (out / slug).mkdir(exist_ok=True)
        shutil.copyfile(report, out / slug / "index.html")
        cards.append(_card(slug, log, facts))

    css = resources.files("dqp.report").joinpath("style.css").read_text(encoding="utf-8") + EXTRA_CSS
    repo = (f'<a href="{_e(repo_url)}">Code and README on GitHub</a>' if repo_url else "")
    how = "".join(f"<li><b>{_e(a)}</b>: {_e(b)}</li>" for a, b in STEPS)
    sections = (f'<section><div class="section-head"><h2>Datasets</h2><span class="muted small">{len(cards)} '
                f'report(s)</span></div><div class="cards">{"".join(cards)}</div></section>'
                f'<section><div class="section-head"><h2>What every run does</h2></div>'
                f'<ol class="how">{how}</ol></section>')
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>dq-pipeline Reports</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{FONTS}">
<style>
{css}
</style>
</head>
<body>
<main class="page">
<header class="top">
  <span class="eyebrow">Data quality pipeline</span>
  <h1>dq-pipeline</h1>
  <p class="lead">Point it at a tabular dataset and it profiles, validates, cleans, documents and summarises it.
    Each report below was produced by the latest automatic run of the pipeline.</p>
  <div class="meta">{repo}</div>
</header>
{grid_sections(sections)}
<footer><span>Built by GitHub Actions from the pipeline's own output files. Reports marked "a local run"
  are snapshots of datasets whose data cannot be published; they show quality results only.</span></footer>
</main>
</body>
</html>
"""
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return out


SNAPSHOT_FILES = ("report.html", "facts.json")


def make_snapshot(run_dir: str | Path, out: str | Path) -> Path:
    """Copy what the site needs from a local run, for datasets whose data is not in the repo.

    Refuses unless the run's report was made with report.show_values: false, so data
    values can never reach the public site this way. The run log is reduced to the
    fields the site reads; profile highlights are dropped because they quote raw values."""
    run_dir, out = Path(run_dir), Path(out)
    log = json.loads((run_dir / "run_log.json").read_text(encoding="utf-8"))
    if log.get("status") != "success":
        raise ValueError(f"{run_dir}: the last run did not finish")
    report_step = next((s for s in log["steps"] if s["step"] == "report"), None)
    if report_step is None or report_step.get("show_values") is not False:
        raise ValueError(f"{run_dir}: refusing to snapshot a report that shows data values; "
                         f"set report.show_values: false in the config and run again")
    facts = json.loads((run_dir / "facts.json").read_text(encoding="utf-8"))["facts"]
    if any(k.startswith("key.") and k != "key.available" for k in facts):
        raise ValueError(f"{run_dir}: facts.json holds headline numbers; refusing to snapshot")
    out.mkdir(parents=True, exist_ok=True)
    for name in SNAPSHOT_FILES:
        shutil.copyfile(run_dir / name, out / name)
    keep = ("run_id", "dataset", "config_sha256", "dqp_version", "started_at", "finished_at", "status",
            "validation", "cleaning", "summary")
    slim = {k: log[k] for k in keep if k in log}
    slim["loads"] = [{k: l[k] for k in ("table", "location", "sheet", "rows", "columns", "status")}
                     for l in log["loads"]]
    for l in slim["loads"]:
        l["location"] = Path(l["location"]).name
    slim["snapshot"] = True
    (out / "run_log.json").write_text(json.dumps(slim, indent=2), encoding="utf-8")
    return out
