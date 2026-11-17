"""Runs the layers in order for one dataset config.

  1 input        sources -> raw_<t>              (all text, as it arrived)
  2 profiling    facts about every raw column
  3 validation   on arrival: generic checks + rules with stage: raw
  4 cleaning     raw_<t> -> stg_<t> (+ rejected_<t>), then marts from SQL
                 cleaned tables are profiled, gaps documented, and validated
                 again: generic checks + rules with stage: staging (default)
  5 summary      plain-language report from a fact pack; an LLM may write it,
                 but numbers only enter as {{fact}} placeholders that the code
                 fills in, and a checker rejects any other number
  The run's validation result is the one AFTER cleaning: that is the data
  that moves on to the summary and the report.
  6 report       report.html: verdict, summary, checks, cleaning, gaps, numbers

Every run writes to outputs/<dataset>/:
  <dataset>.duckdb   raw_, typed_, stg_, rejected_, mart_ tables and the _dqp_ metadata
                     tables (_dqp_runs, _dqp_loads, _dqp_profile_columns, _dqp_highlights,
                     _dqp_checks, _dqp_cleaning_log, _dqp_gaps, _dqp_coverage)
  profile.json       every computed fact, addressable by its path
  validation.json    every check, both stages: status, failing rows, examples
  cleaning.json      every cleaning step: what, why, how many rows/values
  coverage.json      missing periods and per-column coverage of the cleaned data
  facts.json         every number the summary may use, by ID
  summary.md         the summary (numbers rendered from facts.json)
  summary.json       how it was written: template or LLM, checker results, rejected drafts
  report.html        the visual report: one self-contained file, opens offline
  run_log.json       what ran, on which inputs, with what result
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from dqp import __version__
from dqp.cleaning import CleaningError, build_mart, build_staging, table_coverage
from dqp.config import load_config
from dqp.connectors import load_source
from dqp.profiling import ProfileSettings, build_highlights, profile_table
from dqp.profiling.profiler import q
from dqp.report import build_report
from dqp.summary import build_facts, identifiers_of, make_client, write_summary
from dqp.validation import Target, create_typed_view, run_validation, typed_name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


class _Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = round(time.perf_counter() - self.t0, 3)


def run(config_path: str | Path, llm_client=None) -> dict:
    """Run every layer for one dataset config. llm_client overrides the
    provider from the config (used by the tests)."""
    config, config_hash = load_config(config_path)
    out_dir = Path(config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / f"{config.dataset}.duckdb"

    log: dict = {
        "run_id": uuid.uuid4().hex[:12],
        "dataset": config.dataset,
        "config_file": str(config_path),
        "config_sha256": config_hash,
        "dqp_version": __version__,
        "started_at": _now(),
        "status": "running",
        "steps": [],
        "loads": [],
    }

    con = duckdb.connect(str(db_path))
    try:
        # 1 input
        with _Timer() as tm:
            for source in config.sources:
                log["loads"].extend(r.to_dict() for r in load_source(source, con))
        raw_tables = [l["table"] for l in log["loads"] if l["table"]]
        log["steps"].append({"step": "input", "status": "done", "seconds": tm.seconds, "tables": raw_tables})

        # 2 profiling of the raw data
        settings = ProfileSettings(**config.profiling.model_dump())
        with _Timer() as tm:
            raw_profiles = {t: profile_table(con, t, settings) for t in raw_tables}
            text_cols = _text_columns(config, raw_tables, raw_profiles)
            for t, cols in text_cols.items():
                _force_text(raw_profiles[t], cols)
            highlights = [h for t, p in raw_profiles.items() for h in build_highlights(t, p)]
            _store_profiles(con, log["run_id"], "raw", raw_profiles, highlights)
        log["highlights"] = highlights
        log["steps"].append({"step": "profiling", "status": "done", "seconds": tm.seconds, "tables": raw_tables,
                             "highlights": {lvl: sum(h["level"] == lvl for h in highlights)
                                            for lvl in ("attention", "info")}})

        # 3 validation on arrival
        with _Timer() as tm:
            raw_targets = {}
            for t in raw_tables:
                create_typed_view(con, t, raw_profiles[t])
                raw_targets[t] = Target(name=t, view=typed_name(t), raw=t)
            v_raw = run_validation(con, raw_profiles, raw_targets, config.validation, "raw",
                                   config.profiling.max_examples)
        log["steps"].append({"step": "validation_on_arrival", "status": "done", "seconds": tm.seconds,
                             "result": v_raw["status"], **v_raw["summary"]})

        # 4 cleaning: raw -> staging -> marts
        with _Timer() as tm:
            cleaning = _clean(con, config, raw_tables)
        log["cleaning"] = {s: {k: c[k] for k in ("rows_raw", "rows_staging", "rows_rejected")}
                           for s, c in cleaning["tables"].items()}
        log["steps"].append({"step": "cleaning", "status": "done", "seconds": tm.seconds,
                             "staging_tables": list(cleaning["tables"]), "marts": cleaning["marts"],
                             "logged_steps": len(cleaning["log"]),
                             "rows_rejected": sum(c["rows_rejected"] for c in cleaning["tables"].values())})

        # profile, document and validate the cleaned data
        with _Timer() as tm:
            stg_settings = replace(settings, row_expr="_row")
            stg_profiles, stg_targets = {}, {}
            for stg, info in cleaning["tables"].items():
                stg_profiles[stg] = _profile_typed(con, stg, stg_settings)
                forced = text_cols.get(info["raw_table"], [])
                _force_text(stg_profiles[stg], [c for c, rc in info["lineage"].items() if rc in forced])
                _apply_cast_losses(stg_profiles[stg], info["cast_losses"])
                stg_targets[stg] = Target(name=stg, view=stg, raw=info["raw_table"], lineage=info["lineage"])
            for mart in cleaning["marts"]:
                stg_profiles[mart] = _profile_typed(con, mart, stg_settings)
                stg_targets[mart] = Target(name=mart, view=mart)
            _store_profiles(con, log["run_id"], "staging", stg_profiles, [])
            coverage = {t: table_coverage(con, t, p) for t, p in stg_profiles.items()}
            v_stg = run_validation(con, stg_profiles, stg_targets, config.validation, "staging",
                                   config.profiling.max_examples)
        log["steps"].append({"step": "validation_after_cleaning", "status": "done", "seconds": tm.seconds,
                             "result": v_stg["status"], **v_stg["summary"]})

        checks = {**v_raw["checks"], **v_stg["checks"]}
        validation = {"run_id": log["run_id"], "status": v_stg["status"], "summary": v_stg["summary"],
                      "stages": {s: {"status": v["status"], "summary": v["summary"]}
                                 for s, v in (("raw", v_raw), ("staging", v_stg))},
                      "checks": checks}
        log["validation"] = {k: validation[k] for k in ("status", "summary", "stages")}

        _store_checks(con, log["run_id"], checks)
        _store_cleaning(con, log["run_id"], cleaning, coverage)

        # 5 summary
        with _Timer() as tm:
            facts = build_facts(con, config, log, checks, cleaning, coverage, stg_profiles)
            client, why = (llm_client, "client passed in") if llm_client else make_client(config.summary)
            summary = write_summary(facts, config.summary, client, why,
                                    identifiers_of(con, raw_tables + list(stg_profiles)))
        log["summary"] = {k: summary.get(k) for k in ("source", "reason", "model")}
        log["summary"]["attempts"] = len(summary["attempts"])
        log["steps"].append({"step": "summary", "status": "done", "seconds": tm.seconds, **log["summary"]})
        _dump(out_dir / "facts.json", {"run_id": log["run_id"], "facts": facts.to_dict()})
        _dump(out_dir / "summary.json", {"run_id": log["run_id"], **summary})
        (out_dir / "summary.md").write_text(_summary_md(config.dataset, log, summary), encoding="utf-8")

        # 6 report
        with _Timer() as tm:
            html = build_report(con, config, log, checks, cleaning, coverage, stg_profiles, facts, summary)
            (out_dir / "report.html").write_text(html, encoding="utf-8")
        log["steps"].append({"step": "report", "status": "done", "seconds": tm.seconds,
                             "file": str(out_dir / "report.html"), "show_values": config.report.show_values})
        _dump(out_dir / "profile.json", {"run_id": log["run_id"], "dataset": config.dataset,
                                         "settings": config.profiling.model_dump(),
                                         "tables": raw_profiles, "staging": stg_profiles})
        _dump(out_dir / "validation.json", validation)
        _dump(out_dir / "cleaning.json", {"run_id": log["run_id"], **cleaning})
        _dump(out_dir / "coverage.json", {"run_id": log["run_id"], "tables": coverage})
        log["status"] = "success"
    except Exception as exc:  # the run log records failures too, then re-raises
        log["status"] = "failed"
        log["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        log["finished_at"] = _now()
        _write_metadata(con, log)
        con.close()
        _dump(out_dir / "run_log.json", log)

    log["db_path"] = str(db_path)
    log["checks"] = checks                    # returned to the caller, not written to run_log.json
    log["cleaning_log"] = cleaning["log"]
    log["coverage"] = coverage
    log["summary_text"] = summary["text"]
    return log


def _summary_md(dataset: str, log: dict, summary: dict) -> str:
    by = ("an LLM (" + summary["model"] + "), checked against facts.json" if summary["source"] == "llm"
          else "the built-in template")
    return (f"# Data quality report: {dataset}\n\n"
            f"_Run {log['run_id']}, {log['started_at']}. Written by {by}. Every number comes from "
            f"facts.json; none was typed by the writer._\n\n" + summary["text"])


# ---------------------------------------------------------------- cleaning

def _clean(con, config, raw_tables: list[str]) -> dict:
    by_short = {t.removeprefix("raw_"): t for t in raw_tables}
    unknown = [k for k in config.cleaning if k.removeprefix("raw_").removeprefix("stg_") not in by_short]
    if unknown:
        raise CleaningError(f"cleaning configured for unknown table(s) {unknown}; tables: {list(by_short)}")
    steps_for = {k.removeprefix("raw_").removeprefix("stg_"): v for k, v in config.cleaning.items()}

    tables, log = {}, []
    for short, raw in by_short.items():
        info = build_staging(con, raw, typed_name(raw), steps_for.get(short, []), config.profiling.max_examples)
        log.extend(info.pop("log"))
        tables[info["staging_table"]] = info
    marts = [build_mart(con, m.name, m.sql) for m in config.marts]
    for m, spec in zip(marts, config.marts):
        n = con.execute(f"SELECT COUNT(*) FROM {q(m)}").fetchall()[0][0]
        log.append({"table": m, "step": 0, "action": "build_mart", "reason": spec.description or "mart from SQL",
                    "rows_before": None, "rows_after": n, "rows_removed": 0, "values_changed": 0,
                    "columns": [], "details": " ".join(spec.sql.split())[:300], "examples": []})
    return {"tables": tables, "marts": marts, "log": log}


def _profile_typed(con, table: str, settings: ProfileSettings) -> dict:
    """Profile a typed table (staging or mart) with the same profiler as raw data:
    through a text view, so types are re-detected from the values and every
    check works the same way at both stages. Rows are numbered by _row."""
    cols = [r[0] for r in con.execute(f"DESCRIBE {q(table)}").fetchall() if r[0] != "_row"]
    view = f"_text_{table}"
    casts = ", ".join(f"CAST({q(c)} AS VARCHAR) AS {q(c)}" for c in cols)
    con.execute(f"CREATE OR REPLACE VIEW {q(view)} AS SELECT _row, {casts} FROM {q(table)}")
    prof = profile_table(con, view, settings)
    # a typed table already has real types: a VARCHAR column there is text by decision
    # (an ID, a code, a label), not a number that failed to parse
    types = {r[0]: r[1] for r in con.execute(f"DESCRIBE {q(table)}").fetchall()}
    _force_text(prof, [c for c in cols if types[c] == "VARCHAR" and prof["columns_profile"][c]["inferred_type"] != "empty"])
    return prof


def _text_columns(config, raw_tables: list[str], profiles: dict) -> dict[str, list[str]]:
    """Resolve config.text_columns to raw table names, failing loudly on typos."""
    out = {}
    for name, cols in config.text_columns.items():
        t = name if name in raw_tables else f"raw_{name.removeprefix('stg_')}"
        if t not in raw_tables:
            raise ValueError(f"text_columns: unknown table {name!r}; tables: {raw_tables}")
        missing = [c for c in cols if c not in profiles[t]["columns_profile"]]
        if missing:
            raise ValueError(f"text_columns: {t} has no column(s) {missing}")
        out[t] = cols
    return out


def _force_text(prof: dict, cols: list[str]) -> None:
    """Treat these columns as text: codes like invoice numbers (489434, C489449)
    look numeric but are identifiers, so no value 'fails' a numeric type."""
    for c in cols:
        p = prof["columns_profile"][c]
        p.update(inferred_type="text", type_share=1.0, nonconforming=0, nonconforming_examples=[],
                 type_override="text (config)")
        p.pop("numeric", None)
        p.pop("date", None)
        p.pop("mixed_dominant_type", None)
    if prof.get("main_date_column") in cols:
        prof["main_date_column"] = None


def _apply_cast_losses(prof: dict, losses: dict) -> None:
    """In staging, a value that could not be typed is NULL, so the text view
    can't see it. Put the loss back into the profile, so the generic check
    'values_fit_type' reports it at this stage too, with the original value."""
    for col, loss in losses.items():
        c = prof["columns_profile"].get(col)
        if c is None:
            continue
        c["nonconforming"] = loss["count"]
        c["nonconforming_examples"] = [{"row": e["_row"], "value": e["raw value"]} for e in loss["examples"]]
        c["non_null"] += loss["count"]
        if c["inferred_type"] == "empty":   # every value was lost: report against the staging type
            c["inferred_type"] = "numeric" if loss["type"] == "numeric" else "date" if loss["type"] == "a date" else "boolean"


# ---------------------------------------------------------------- storage

def _store_profiles(con, run_id: str, stage: str, profiles: dict, highlights: list) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS _dqp_profile_columns (
        run_id VARCHAR, "table" VARCHAR, "column" VARCHAR, position INTEGER, inferred_type VARCHAR,
        type_share DOUBLE, "rows" BIGINT, nulls BIGINT, null_share DOUBLE, distinct_values BIGINT,
        nonconforming BIGINT, details JSON)""")
    con.execute("ALTER TABLE _dqp_profile_columns ADD COLUMN IF NOT EXISTS stage VARCHAR")
    con.execute("""CREATE TABLE IF NOT EXISTS _dqp_highlights (
        run_id VARCHAR, "table" VARCHAR, level VARCHAR, text VARCHAR, facts JSON)""")
    for t, p in profiles.items():
        for pos, (name, c) in enumerate(p["columns_profile"].items(), start=1):
            con.execute("""INSERT INTO _dqp_profile_columns (run_id, "table", "column", position, inferred_type,
                type_share, "rows", nulls, null_share, distinct_values, nonconforming, details, stage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [run_id, t, name, pos, c["inferred_type"], c["type_share"], c["rows"], c["nulls"],
                         c["null_share"], c["distinct_values"], c["nonconforming"], json.dumps(c, default=str), stage])
    for h in highlights:
        con.execute("INSERT INTO _dqp_highlights VALUES (?, ?, ?, ?, ?)",
                    [run_id, h["table"], h["level"], h["text"], json.dumps(h["facts"])])


def _store_checks(con, run_id: str, checks: dict) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS _dqp_checks (
        run_id VARCHAR, check_id VARCHAR, "table" VARCHAR, name VARCHAR, origin VARCHAR, kind VARCHAR,
        severity VARCHAR, status VARCHAR, failing BIGINT, checked BIGINT, detail VARCHAR, examples JSON)""")
    con.execute("ALTER TABLE _dqp_checks ADD COLUMN IF NOT EXISTS stage VARCHAR")
    for c in checks.values():
        con.execute("""INSERT INTO _dqp_checks (run_id, check_id, "table", name, origin, kind, severity, status,
            failing, checked, detail, examples, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [run_id, c["check_id"], c["table"], c["name"], c["origin"], c["kind"], c["severity"],
                     c["status"], c["failing"], c["checked"], c["detail"],
                     json.dumps(c["examples"], default=str), c["stage"]])


def _store_cleaning(con, run_id: str, cleaning: dict, coverage: dict) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS _dqp_cleaning_log (
        run_id VARCHAR, "table" VARCHAR, step INTEGER, action VARCHAR, reason VARCHAR,
        rows_before BIGINT, rows_after BIGINT, rows_removed BIGINT, values_changed BIGINT,
        columns JSON, details VARCHAR, examples JSON)""")
    for e in cleaning["log"]:
        con.execute("INSERT INTO _dqp_cleaning_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, e["table"], e["step"], e["action"], e["reason"], e["rows_before"], e["rows_after"],
                     e["rows_removed"], e["values_changed"], json.dumps(e["columns"]), e["details"],
                     json.dumps(e["examples"], default=str)])
    con.execute("""CREATE TABLE IF NOT EXISTS _dqp_gaps (
        run_id VARCHAR, "table" VARCHAR, date_column VARCHAR, frequency VARCHAR,
        missing_period_count BIGINT, missing_periods JSON)""")
    con.execute("""CREATE TABLE IF NOT EXISTS _dqp_coverage (
        run_id VARCHAR, "table" VARCHAR, "column" VARCHAR, first_date VARCHAR, last_date VARCHAR,
        "values" BIGINT, internal_missing BIGINT, internal_missing_dates JSON)""")
    for t, cov in coverage.items():
        con.execute("INSERT INTO _dqp_gaps VALUES (?, ?, ?, ?, ?, ?)",
                    [run_id, t, cov["date_column"], cov["frequency"], cov["missing_period_count"],
                     json.dumps(cov["missing_periods"])])
        for col, c in cov["columns"].items():
            con.execute("INSERT INTO _dqp_coverage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [run_id, t, col, c["first"], c["last"], c["values"], c["internal_missing"],
                         json.dumps(c["internal_missing_dates"])])


def _write_metadata(con: duckdb.DuckDBPyConnection, log: dict) -> None:
    """Keep the run history inside the database too, so it survives re-runs
    (run_log.json only holds the latest run)."""
    con.execute(
        """CREATE TABLE IF NOT EXISTS _dqp_runs (
               run_id VARCHAR, dataset VARCHAR, config_sha256 VARCHAR, dqp_version VARCHAR,
               started_at VARCHAR, finished_at VARCHAR, status VARCHAR, error VARCHAR)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS _dqp_loads (
               run_id VARCHAR, "table" VARCHAR, source_name VARCHAR, source_type VARCHAR,
               location VARCHAR, sheet VARCHAR, "rows" BIGINT, "columns" INTEGER, sha256 VARCHAR,
               status VARCHAR, notes VARCHAR)"""
    )
    con.execute(
        "INSERT INTO _dqp_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [log["run_id"], log["dataset"], log["config_sha256"], log["dqp_version"],
         log["started_at"], log["finished_at"], log["status"], log.get("error")],
    )
    for l in log["loads"]:
        con.execute(
            "INSERT INTO _dqp_loads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [log["run_id"], l["table"], l["source_name"], l["source_type"],
             l["location"], l["sheet"], l["rows"], l["columns"], l["sha256"],
             l["status"], "; ".join(l["notes"])],
        )
