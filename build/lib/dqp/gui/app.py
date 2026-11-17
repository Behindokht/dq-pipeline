"""The dqp GUI: the same pipeline, one step at a time, in the browser.

Start it with `dqp gui`. It runs on this computer; the data is not sent anywhere
(a Google Sheet is downloaded from Google, nothing is uploaded).

Steps: 1 data -> 2 layout -> 3 cleaning -> 4 rules -> 5 results.
The GUI only builds a config and calls the same functions as the command line
(init.analyse, pipeline.run, export.export_run), so a run here and a run with
`dqp run` on the downloaded config give the same result.
"""

from __future__ import annotations

import copy
import io
import os
import re
import shutil
import tempfile
import zipfile
from html import escape as e
from pathlib import Path

import duckdb
import streamlit as st
import streamlit.components.v1 as components
import yaml
from pydantic import ValidationError

from dqp.config import DatasetConfig, slugify
from dqp.connectors import load_source
from dqp.export import ExportError, export_run
from dqp.gui.helpers import is_number, parse_rows, portable, rule_label, sql_name, sql_text
from dqp.init import Analysis, InitError, Suggestion, analyse, profile_source, suggest
from dqp.pipeline import run
from dqp.profiling.highlights import build_highlights

HERE = Path(__file__).parent
EXAMPLE = HERE / "examples" / "shop_orders.csv"
STEPS = ["Data", "Layout", "Cleaning", "Rules", "Results"]
DELIMITERS = {",": "comma  ,", ";": "semicolon  ;", "\t": "tab", "|": "pipe  |"}
VERDICT = {"passed": ("Passed", "Every check passed after cleaning."),
           "passed_with_warnings": ("Passed with warnings", "No check failed; some need a look."),
           "failed": ("Failed", "At least one check failed after cleaning. The data is not ready to use.")}

ss = st.session_state


# ------------------------------------------------------------------ helpers

def work_dir() -> Path:
    if "work" not in ss:
        ss.work = Path(tempfile.mkdtemp(prefix="dqp-gui-"))
    return ss.work


def reset(keep_step: bool = False) -> None:
    for k in list(ss.keys()):
        if k not in ("work",) and not (keep_step and k == "step"):
            del ss[k]


def local_source(an: Analysis) -> dict:
    """The source block that reads the local file (a Google Sheet's downloaded copy)."""
    if an.source["type"] != "gsheet":
        return an.source
    return {"name": an.source["name"], "type": "excel", "path": str(an.local_path),
            **{k: v for k, v in an.source.items() if k in ("sheets", "na_values")}}


def preview(an: Analysis, rows: int = 12) -> dict:
    cfg = DatasetConfig.model_validate({"dataset": an.dataset, "sources": [local_source(an)]})
    con = duckdb.connect()
    try:
        loads = load_source(cfg.sources[0], con)
        return {l.table: con.execute(f'SELECT * FROM "{l.table}" LIMIT {rows}').df() for l in loads if l.table}
    finally:
        con.close()


def skey(s: Suggestion) -> str:
    """A key that stays the same when other choices change the suggestion's details."""
    if s.section == "cleaning":
        return f"cleaning:{s.table}:{next(iter(s.config))}"
    if s.section == "text_columns":
        return f"text:{s.table}"
    if s.section == "generic":
        return f"generic:{next(iter(s.config))}"
    return s.id


def keep_choices() -> None:
    """Streamlit forgets a widget's value when the widget is not drawn (another step
    is shown). Writing the values back once per run keeps them."""
    for k in list(ss.keys()):
        if k.startswith(("use:", "reason:")) or k in ("desc", "ai", "fmt", "rej", "anyway"):
            ss[k] = ss[k]


def checkbox(label: str, key: str, default: bool = True, **kw) -> bool:
    if key not in ss:
        ss[key] = default
    return st.checkbox(label, key=key, **kw)


def chosen(s: Suggestion) -> bool:
    return ss.get(f"use:{skey(s)}", True)


def current_suggestions(an: Analysis) -> tuple[list[Suggestion], dict]:
    """Suggestions with column names that follow the renaming steps the person kept."""
    skip = {(s.table, next(iter(s.config))) for s in an.suggestions
            if s.section == "cleaning" and not chosen(s)}
    return suggest(an.profiles, skip)


def accepted(an: Analysis) -> tuple[list[Suggestion], list[str]]:
    """The suggestions switched on, with reasons filled in, and what is still missing."""
    sugg, _ = current_suggestions(an)
    out, missing = [], []
    for s in sugg:
        if not chosen(s):
            continue
        s = copy.deepcopy(s)
        if s.needs_reason:
            reason = ss.get(f"reason:{skey(s)}", "").strip()
            if len(reason) < 3:
                missing.append(f"{s.table}: {step_label(s)}")
            next(iter(s.config.values()))["reason"] = reason
        out.append(s)
    out += [Suggestion("cleaning", c["table"], c["step"]) for c in ss.get("custom_steps", [])]
    out += [Suggestion("rules", r["table"], r["rule"]) for r in ss.get("custom_rules", [])]
    return out, missing


def final_columns(an: Analysis) -> dict[str, list[str]]:
    _, names = current_suggestions(an)
    return {t.removeprefix("raw_"): list(m.values()) for t, m in names.items()}


def step_label(s: Suggestion) -> str:
    kind, v = next(iter(s.config.items()))
    return {"rename": lambda: f"name the date column {next(iter(v))!r} 'date'",
            "snake_case": lambda: "tidy the column names",
            "trim_whitespace": lambda: "remove spaces around values",
            "drop_rows": lambda: "set aside rows that are not data",
            "drop_duplicates": lambda: "set aside exact duplicate rows"}.get(kind, lambda: kind)()


# ------------------------------------------------------------------ look

def style() -> None:
    st.markdown((HERE / "style.css").read_text(encoding="utf-8").join(["<style>", "</style>"]),
                unsafe_allow_html=True)
    st.markdown('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600'
                '&family=Inter+Tight:wght@600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">',
                unsafe_allow_html=True)


def header() -> None:
    cells = "".join(
        f'<li class="{"on" if i == ss.step else "done" if i < ss.step else ""}">'
        f'<span>{i + 1:02d}</span>{name}</li>' for i, name in enumerate(STEPS))
    st.markdown(f'<div class="dqp-head"><div class="dqp-title">Data Quality Pipeline</div>'
                f'<ol class="dqp-steps">{cells}</ol></div>', unsafe_allow_html=True)


def nav(next_ok: bool = True, next_label: str = "Next", why: str = "", back: bool = True) -> None:
    st.markdown('<div class="dqp-rule"></div>', unsafe_allow_html=True)
    left, mid, _, right = st.columns([1, 1, 2, 1])
    if back and ss.step > 0 and left.button("Back", key=f"back{ss.step}", width="stretch"):
        ss.step -= 1
        st.rerun()
    if ss.step > 0 and mid.button("Start over", key="restart", type="tertiary"):
        reset()
        st.rerun()
    if next_label and right.button(next_label, key=f"next{ss.step}", type="primary", disabled=not next_ok,
                                   width="stretch"):
        ss.step += 1
        st.rerun()
    if why and not next_ok:
        st.caption(why)


# ------------------------------------------------------------------ 1 data

def step_data() -> None:
    st.markdown("## Your data")
    st.markdown("A CSV or Excel file, or a Google Sheet. Everything runs on this computer.")
    how = st.radio("Where is the data?", ["Upload a file", "Google Sheets link", "Try the example",
                                          "Saved settings"],
                   horizontal=True, key="how", label_visibility="collapsed")
    src = None
    if how == "Saved settings":
        return saved_settings()
    if how == "Upload a file":
        f = st.file_uploader("CSV or Excel file", type=["csv", "txt", "tsv", "xlsx", "xlsm", "xls"], key="upload")
        if f is not None:
            target = work_dir() / "input" / Path(f.name).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.getvalue())
            src = target
    elif how == "Google Sheets link":
        url = st.text_input("Link to the sheet", key="url",
                            placeholder="https://docs.google.com/spreadsheets/d/…/edit")
        st.caption("Share the sheet as “Anyone with the link can view” first. Every tab becomes a table.")
        src = url.strip() or None
    else:
        st.markdown(f"`{EXAMPLE.name}`: four weeks of orders from a small shop, exported from the till. "
                    "It has the usual problems: a note under the data, double scans, codes that look like "
                    "numbers and stray spaces.")
        target = work_dir() / "input" / EXAMPLE.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(EXAMPLE, target)
        src = target
    name = st.text_input("Dataset name (optional)", key="name", placeholder="e.g. shop_orders",
                         help="Used for file and table names: lower case letters, digits and _.")
    if st.button("Analyse", type="primary", disabled=src is None, key="analyse"):
        with st.spinner("Reading the data and looking for problems…"):
            try:
                an = analyse(src, slugify(name) if name.strip() else None, download_dir=work_dir() / "downloads")
            except (InitError, ValidationError, ValueError) as exc:
                st.error(str(exc))
                return
        keep = {k: ss[k] for k in ("how", "url", "name") if k in ss}
        reset()
        ss.update(keep)
        ss.an, ss.step = an, 1
        st.rerun()


def saved_settings() -> None:
    """Run the same checks again on new data, with a settings file downloaded earlier."""
    st.markdown("The settings file you downloaded after an earlier run, and this month's data file. "
                "The same cleaning and rules are applied; nothing is suggested or changed.")
    f = st.file_uploader("Settings (YAML)", type=["yaml", "yml"], key="saved_yaml")
    if f is None:
        return
    try:
        cfg = yaml.safe_load(f.getvalue().decode("utf-8"))
        DatasetConfig.model_validate({**cfg, "output_dir": "x"})
    except (yaml.YAMLError, ValidationError, AttributeError, TypeError) as exc:
        st.error(f"This is not a settings file dqp can use: {exc}")
        return
    files = [s for s in cfg["sources"] if s["type"] in ("csv", "excel") and not s.get("download")]
    data = {}
    if files:
        names = ", ".join(Path(s["path"]).name for s in files)
        ups = st.file_uploader(f"Data file(s): {names}", type=["csv", "txt", "tsv", "xlsx", "xlsm", "xls"],
                               accept_multiple_files=True, key="saved_data")
        for u in ups or []:
            target = work_dir() / "input" / Path(u.name).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(u.getvalue())
            data[target.name] = target
        if len(files) == 1 and len(data) == 1:          # one source: the file name may differ (new month)
            data = {Path(files[0]["path"]).name: next(iter(data.values()))}
    missing = [Path(s["path"]).name for s in files if Path(s["path"]).name not in data]
    if st.button("Run with these settings", type="primary", disabled=bool(missing), key="saved_run"):
        for s in cfg["sources"]:
            if s in files:
                s["path"] = str(data[Path(s["path"]).name])
            elif s["type"] in ("csv", "excel"):              # downloaded by the pipeline itself
                s["path"] = str(work_dir() / "downloads" / Path(s["path"]).name)
            elif s["type"] == "gsheet":
                s["cache_dir"] = str(work_dir() / "downloads")
        cfg["output_dir"] = str(work_dir() / "outputs" / cfg["dataset"])
        path = work_dir() / f"{cfg['dataset']}.yaml"
        path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        with st.spinner("Profiling, cleaning and checking…"):
            log = run(path)
        src = cfg["sources"][0]
        label = "the Google Sheet" if src["type"] == "gsheet" else Path(src["path"]).name
        an = Analysis(cfg["dataset"], src, Path(src.get("path", "")), label, [], {})
        keep = {k: ss[k] for k in ("how",) if k in ss}
        reset()
        ss.update(keep)
        ss.an, ss.saved, ss.result, ss.result_cfg, ss.step = an, f.name, log, cfg, 4
        st.rerun()
    if missing:
        st.caption("Waiting for: " + ", ".join(missing))


# ------------------------------------------------------------------ 2 layout

def step_layout(an: Analysis) -> None:
    st.markdown("## How the data is laid out")
    st.markdown("Detected from the file. Check the preview: the column names should be in the header.")
    st.markdown("".join(f"<div class='dqp-note'>{e(n)}</div>" for n in an.notes), unsafe_allow_html=True)

    src = an.source
    with st.expander("Change how the file is read"):
        if src["type"] == "csv":
            c1, c2, c3 = st.columns(3)
            delim = c1.selectbox("Separator", list(DELIMITERS), format_func=DELIMITERS.get,
                                 index=list(DELIMITERS).index(src["delimiter"]) if src["delimiter"] in DELIMITERS else 0)
            header = c2.checkbox("First line holds the column names", value=src["header"])
            skip = c3.number_input("Lines to skip above it", 0, 100, int(src.get("skip_rows", 0)))
            new = {**src, "delimiter": delim, "header": header}
            new.pop("skip_rows", None)
            if skip:
                new["skip_rows"] = int(skip)
        else:
            st.caption("Row numbers as Excel shows them. A header spread over rows 4 to 6: write 4-6.")
            sheets = []
            for i, sh in enumerate(src["sheets"]):
                hr = sh["header_rows"]
                shown = f"{hr + 1}" if isinstance(hr, int) else f"{hr[0] + 1}-{hr[-1] + 1}"
                c1, c2 = st.columns([2, 1])
                use = c1.checkbox(f"Sheet “{sh['sheet']}”", value=True, key=f"sheet_use{i}")
                txt = c2.text_input("Header row", value=shown, key=f"sheet_hr{i}")
                if use:
                    sheets.append({"sheet": sh["sheet"], "header_rows": parse_rows(txt)})
            new = {**src, "sheets": sheets}
        if st.button("Read again with these settings", key="reread"):
            if new.get("sheets") == []:
                st.error("Keep at least one sheet.")
            else:
                try:
                    an.source = new
                    with st.spinner("Reading again…"):
                        an.profiles = profile_source(local_source(an), an.dataset)
                        an.suggestions, an.final_names = suggest(an.profiles)
                    ss.pop("preview", None)
                    st.rerun()
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))

    if "preview" not in ss:
        ss.preview = preview(an)
    for t, prof in an.profiles.items():
        st.markdown(f"### {t.removeprefix('raw_')}")
        st.markdown(f"<div class='dqp-meta'>{prof['rows']:,} rows · {len(prof['columns_profile'])} columns"
                    + (f" · dates {prof['columns_profile'][prof['main_date_column']]['date']['min']} to "
                       f"{prof['columns_profile'][prof['main_date_column']]['date']['max']}"
                       if prof.get("main_date_column") else "") + "</div>", unsafe_allow_html=True)
        st.dataframe(ss.preview.get(t), hide_index=True, width="stretch")
        hl = build_highlights(t, prof)
        if hl:
            st.markdown("**What stands out**")
            st.markdown("".join(f"<div class='dqp-hl {h['level']}'>{e(h['text'])}</div>" for h in hl[:12]),
                        unsafe_allow_html=True)
    nav()


# ------------------------------------------------------------------ 3 cleaning

def step_cleaning(an: Analysis) -> None:
    st.markdown("## Cleaning")
    st.markdown("Suggested from what was found. Untick what you do not want. Nothing is deleted: rows that "
                "are set aside go to a separate <code>rejected_</code> table that you can download.",
                unsafe_allow_html=True)
    sugg, _ = current_suggestions(an)

    text = [s for s in sugg if s.section == "text_columns"]
    if text:
        st.markdown("### Keep as text")
        for s in text:
            checkbox(f"{s.table}: {', '.join(s.config)}", f"use:{skey(s)}", help=s.note)
        st.caption("IDs and codes that look like numbers. As text, 00412 stays 00412.")

    steps = [s for s in sugg if s.section == "cleaning"]
    st.markdown("### Steps")
    if not steps:
        st.markdown("No cleaning needed: no notes under the data, duplicates or odd column names found.")
    for s in steps:
        k = skey(s)
        checkbox(f"{s.table}: {step_label(s)}", f"use:{k}")
        st.markdown(f"<div class='dqp-why'>{e(s.note)}</div>", unsafe_allow_html=True)
        if s.needs_reason and ss.get(f"use:{k}", True):
            ss.setdefault(f"reason:{k}", "")
            st.text_input("Why? (required; goes into the cleaning log)", key=f"reason:{k}",
                          placeholder="e.g. the till adds an export note under the data")
        if "drop_rows" in s.config:
            st.code(f"where {s.config['drop_rows']['where']}", language="sql")

    cols = final_columns(an)
    with st.expander("Set aside other rows"):
        c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
        table = c1.selectbox("Table", list(cols), key="cs_table")
        col = c2.selectbox("Column", cols[table], key="cs_col")
        cond = c3.selectbox("Condition", ["is empty", "equals", "contains"], key="cs_cond")
        val = c4.text_input("Value", key="cs_val", disabled=cond == "is empty")
        reason = st.text_input("Why?", key="cs_reason")
        if st.button("Add step", key="cs_add"):
            where = (f"{sql_name(col)} IS NULL" if cond == "is empty"
                     else f"CAST({sql_name(col)} AS VARCHAR) = {sql_text(val)}" if cond == "equals"
                     else f"CAST({sql_name(col)} AS VARCHAR) LIKE {sql_text('%' + val + '%')}")
            if len(reason.strip()) < 3:
                st.error("Write a reason first.")
            else:
                ss.setdefault("custom_steps", []).append(
                    {"table": table, "step": {"drop_rows": {"where": where, "reason": reason.strip()}}})
                st.rerun()
    for i, c in enumerate(ss.get("custom_steps", [])):
        a, b = st.columns([6, 1])
        a.markdown(f"<div class='dqp-custom'>{e(c['table'])}: set aside rows where "
                   f"<code>{e(c['step']['drop_rows']['where'])}</code> — {e(c['step']['drop_rows']['reason'])}</div>",
                   unsafe_allow_html=True)
        if b.button("Remove", key=f"cs_rm{i}"):
            ss.custom_steps.pop(i)
            st.rerun()

    _, missing = accepted(an)
    nav(not missing, why="Write a reason for: " + "; ".join(missing))


# ------------------------------------------------------------------ 4 rules

def step_rules(an: Analysis) -> None:
    st.markdown("## Rules")
    st.markdown("Checks that run on the cleaned data, on top of the built-in ones (types, empty columns, "
                "duplicates, gaps in dates). Keep only rules that must <b>always</b> hold.",
                unsafe_allow_html=True)
    sugg, _ = current_suggestions(an)
    gen = [s for s in sugg if s.section == "generic"]
    rules = [s for s in sugg if s.section == "rules"]
    if gen or rules:
        st.markdown("### Suggested")
    for s in gen:
        (k, v), = s.config.items()
        checkbox(f"Switch off the built-in check “{k.replace('_', ' ')}”", f"use:{skey(s)}")
        st.markdown(f"<div class='dqp-why'>{e(s.note)}</div>", unsafe_allow_html=True)
    for s in rules:
        checkbox(f"{s.table}: {s.note}", f"use:{skey(s)}")
        kind, v = next(iter(s.config.items()))
        if kind == "expr":
            st.code(v, language="sql")

    st.markdown("### Add a rule")
    cols = final_columns(an)
    c1, c2, c3 = st.columns(3)
    table = c1.selectbox("Table", list(cols), key="r_table")
    col = c2.selectbox("Column", cols[table], key="r_col")
    kind = c3.selectbox("Must be", ["never empty", "unique", "between", "one of", "a condition (SQL)"], key="r_kind")
    rule: dict = {}
    if kind == "never empty":
        rule = {"not_null": [col]}
    elif kind == "unique":
        rule = {"unique": [col]}
    elif kind == "between":
        a, b = st.columns(2)
        lo, hi = a.text_input("At least", key="r_min"), b.text_input("At most", key="r_max")
        rng = {k: float(x) for k, x in (("min", lo), ("max", hi)) if is_number(x)}
        rule = {"between": {"column": col, **rng}} if rng else {}
    elif kind == "one of":
        vals = [v.strip() for v in st.text_input("Allowed values, separated by commas", key="r_vals").split(",")]
        rule = {"allowed": {"column": col, "values": [v for v in vals if v]}} if any(vals) else {}
    else:
        expr = st.text_input("Condition every row must meet", key="r_expr", placeholder=f"{sql_name(col)} > 0")
        rule = {"expr": expr.strip()} if expr.strip() else {}
    sev = st.radio("If it does not hold", ["fail", "warn"], horizontal=True, key="r_sev",
                   format_func={"fail": "the check fails", "warn": "only a warning"}.get)
    if st.button("Add rule", key="r_add", disabled=not rule):
        ss.setdefault("custom_rules", []).append({"table": table, "rule": {**rule, "severity": sev}})
        st.rerun()
    for i, r in enumerate(ss.get("custom_rules", [])):
        a, b = st.columns([6, 1])
        a.markdown(f"<div class='dqp-custom'>{e(r['table'])}: {e(rule_label(r['rule']))}"
                   f"{' (warning)' if r['rule']['severity'] == 'warn' else ''}</div>", unsafe_allow_html=True)
        if b.button("Remove", key=f"r_rm{i}"):
            ss.custom_rules.pop(i)
            st.rerun()
    nav(next_label="Next")


# ------------------------------------------------------------------ 5 results

def step_results(an: Analysis) -> None:
    st.markdown("## Run and results")
    if ss.get("saved"):
        st.markdown(f"<div class='dqp-note'>settings: {e(ss.saved)} · data: {e(an.label)} · "
                    "same cleaning and rules as before</div>", unsafe_allow_html=True)
        show_result(an, ss.result, ss.result_cfg)
        nav(next_label="", back=False)
        return
    acc, missing = accepted(an)
    if missing:
        st.error("Go back to Cleaning: a reason is missing for " + "; ".join(missing))
        nav(next_label="")
        return
    desc = st.text_area("What is this data? (optional, shown in the report)", key="desc", height=68)
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    ai = checkbox("Write the summary with an AI model", "ai", default=has_key, disabled=not has_key,
                     help="Uses ANTHROPIC_API_KEY or OPENAI_API_KEY. The model only sees computed, checked "
                          "numbers, and its text is checked before it is used.")
    if not has_key:
        st.caption("No API key set, so the built-in summary template is used. The checks are the same.")

    cfg = an.config(acc, description=desc.strip() or None)
    out_dir = work_dir() / "outputs" / an.dataset
    cfg["output_dir"] = str(out_dir)
    cfg["summary"] = {"provider": "auto" if ai else "none"}
    if st.button("Run the check", type="primary", key="run"):
        try:
            DatasetConfig.model_validate(cfg)
        except ValidationError as exc:
            st.error("The settings do not fit together:\n\n" + "\n".join(f"- {e['msg']}" for e in exc.errors()))
            return
        path = work_dir() / f"{an.dataset}.yaml"
        path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        with st.spinner("Profiling, cleaning and checking…"):
            ss.result, ss.result_cfg = run(path), cfg
    if "result" in ss:
        show_result(an, ss.result, ss.result_cfg)
    nav(next_label="")


def show_result(an: Analysis, log: dict, cfg: dict) -> None:
    if log["status"] != "success":
        st.error(f"The run stopped: {log.get('error')}")
        return
    v = log["validation"]
    title, line = VERDICT[v["status"]]
    st.markdown(f"<div class='dqp-verdict {v['status']}'><div class='big'>{title}</div><div>{line}</div></div>",
                unsafe_allow_html=True)
    raw, stg = v["stages"]["raw"]["summary"], v["stages"]["staging"]["summary"]
    rows = "".join(
        f"<tr><td>{k}</td><td class='n'>{raw.get(k, 0)}</td><td class='n'>{stg.get(k, 0)}</td></tr>"
        for k in ("PASS", "WARN", "FAIL", "ERROR", "INFO") if raw.get(k) or stg.get(k))
    tables = "".join(
        f"<tr><td>{e(t.removeprefix('stg_'))}</td><td class='n'>{c['rows_raw']:,}</td>"
        f"<td class='n'>{c['rows_staging']:,}</td><td class='n'>{c['rows_rejected']:,}</td></tr>"
        for t, c in log["cleaning"].items())
    st.markdown(
        "<div class='dqp-grid'>"
        f"<table class='dqp-t'><tr><th>Checks</th><th class='n'>on arrival</th><th class='n'>after cleaning</th></tr>{rows}</table>"
        f"<table class='dqp-t'><tr><th>Table</th><th class='n'>rows in</th><th class='n'>clean</th>"
        f"<th class='n'>set aside</th></tr>{tables}</table></div>", unsafe_allow_html=True)

    flagged = [c for c in log["checks"].values() if c["stage"] == "staging" and c["status"] in ("FAIL", "ERROR", "WARN")]
    if flagged:
        st.markdown("### What needs a look")
        st.markdown("".join(f"<div class='dqp-hl {'attention' if c['status'] != 'WARN' else 'info'}'>"
                            f"<b>{c['status']}</b> {e(c['table'])} · {e(c['name'])} — {e(str(c['detail']))}</div>"
                            for c in flagged[:20]), unsafe_allow_html=True)

    st.markdown("### Summary")
    st.markdown(re.sub(r"^#{1,3} (?!Summary$)", "#### ", re.sub(r"^#{1,3} Summary\s*\n", "", log["summary_text"]),
                       flags=re.M))

    run_dir = Path(cfg["output_dir"])
    show_tables(run_dir / f"{log['dataset']}.duckdb")
    report = (run_dir / "report.html").read_text(encoding="utf-8")
    st.markdown("### Download")
    failed = v["status"] == "failed"
    c1, c2, c3 = st.columns(3)
    fmt = c1.radio("Cleaned data as", ["Excel", "CSV"], horizontal=True, key="fmt")
    rej = c2.checkbox("Include the rows set aside", key="rej")
    anyway = c3.checkbox("Validation failed: download anyway", key="anyway") if failed else False
    try:
        data, fname, mime = export_bytes(run_dir, fmt, rej, anyway, an.dataset)
        st.download_button(f"Cleaned data ({fname})", data, file_name=fname, mime=mime, type="primary",
                           key="dl_data", width="stretch")
    except ExportError as exc:
        st.warning(str(exc).split(": ", 1)[-1].replace("--allow-failed", "“download anyway”"))
    d1, d2 = st.columns(2)
    d1.download_button("Report (HTML)", report, file_name=f"{an.dataset}_report.html", mime="text/html",
                       key="dl_report", width="stretch")
    d2.download_button("Settings for next time (YAML)", portable(cfg, an), file_name=f"{an.dataset}.yaml",
                       mime="text/yaml", key="dl_cfg", width="stretch",
                       help="Run the same checks again, by hand or on a schedule: dqp run <file>")

    st.markdown("### Full report")
    if hasattr(st, "iframe"):          # our own report: values are escaped, no scripts
        st.iframe(run_dir / "report.html", height=900)
    else:
        components.html(report, height=900, scrolling=True)


def show_tables(db: Path, rows: int = 200) -> None:
    """The cleaned tables and the rows that were set aside, with the step and reason."""
    con = duckdb.connect(str(db), read_only=True)
    try:
        names = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables "
                                           "WHERE table_schema = 'main' ORDER BY table_name").fetchall()]
        clean = [n for n in names if n.startswith(("stg_", "mart_"))]
        rejected = [n for n in names if n.startswith("rejected_")
                    and con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchall()[0][0]]
        st.markdown("### The data")
        tabs = st.tabs([f"clean · {n.removeprefix('stg_')}" for n in clean]
                       + [f"set aside · {n.removeprefix('rejected_')}" for n in rejected])
        for tab, n in zip(tabs, clean + rejected):
            df = con.execute(f'SELECT * FROM "{n}" LIMIT {rows}').df()
            total = con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchall()[0][0]
            with tab:
                if n.startswith("stg_") and "_row" in df:
                    df = df.drop(columns="_row")
                st.dataframe(df, hide_index=True, width="stretch")
                st.caption(f"{total:,} row(s)" + (f", first {rows} shown" if total > rows else "")
                           + (". Kept with the step and reason that set them aside; download them with "
                              "“Include the rows set aside”." if n.startswith("rejected_") else ""))
    finally:
        con.close()


def export_bytes(run_dir: Path, fmt: str, rejected: bool, anyway: bool, dataset: str) -> tuple[bytes, str, str]:
    out = Path(tempfile.mkdtemp(dir=work_dir()))
    export_run(run_dir, "xlsx" if fmt == "Excel" else "csv", "all", out, allow_failed=anyway,
                   include_rejected=rejected)
    if fmt == "Excel":
        return (out / f"{dataset}.xlsx").read_bytes(), f"{dataset}_clean.xlsx", \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out))
    return buf.getvalue(), f"{dataset}_clean.zip", "application/zip"


# ------------------------------------------------------------------ page

def main() -> None:
    st.set_page_config(page_title="Data Quality Pipeline", layout="wide", initial_sidebar_state="collapsed")
    style()
    ss.setdefault("step", 0)
    keep_choices()
    if ss.step > 0 and "an" not in ss:
        ss.step = 0
    header()
    [step_data, step_layout, step_cleaning, step_rules, step_results][ss.step](
        *(() if ss.step == 0 else (ss.an,)))


main()
