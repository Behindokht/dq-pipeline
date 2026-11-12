"""Command line entry points:
    dqp init data/sales.xlsx             write a starter config for a data file
    dqp run configs/copper.yaml          run the pipeline
    dqp site --out site outputs/copper   build the GitHub Pages site from finished runs
    dqp export outputs/my_sales --format xlsx
                                         cleaned tables to csv / xlsx / parquet
    dqp snapshot outputs/thesis_assets --out snapshots/thesis_assets
                                         quality-only copy of a local run, for the site
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from dqp.pipeline import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dqp", description="Generic data quality & insight pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="run the pipeline for one or more dataset configs")
    run_cmd.add_argument("configs", nargs="+", help="path(s) to YAML config files")
    run_cmd.add_argument("--strict", action="store_true",
                         help="exit with code 2 if any check FAILs or ERRORs (for CI)")
    run_cmd.add_argument("-v", "--verbose", action="store_true", help="also print profile highlights")
    site_cmd = sub.add_parser("site", help="build the static site (GitHub Pages) from finished runs")
    site_cmd.add_argument("runs", nargs="+", help="output folders of finished runs, e.g. outputs/copper")
    site_cmd.add_argument("--out", default="site", help="folder to write the site to (default: site)")
    site_cmd.add_argument("--repo-url", default=None, help="link to the code, shown on the index page")
    snap_cmd = sub.add_parser("snapshot", help="copy a quality-only report of a local run for the site")
    snap_cmd.add_argument("run", help="output folder of a finished run, e.g. outputs/thesis_assets")
    snap_cmd.add_argument("--out", required=True, help="folder to write the snapshot to")
    exp_cmd = sub.add_parser("export", help="export the cleaned tables of a finished run")
    exp_cmd.add_argument("run", help="output folder of a finished run, e.g. outputs/my_sales")
    exp_cmd.add_argument("--format", default="csv", choices=["csv", "xlsx", "parquet"])
    exp_cmd.add_argument("--tables", default="all", choices=["all", "staging", "marts"],
                         help="staging tables, marts, or both (default)")
    exp_cmd.add_argument("--out", default=None, help="folder to write to (default: <run>/export)")
    exp_cmd.add_argument("--include-rejected", action="store_true", help="also export the rejected_ tables")
    exp_cmd.add_argument("--keep-row-numbers", action="store_true",
                         help="keep _row, the line number in the source file")
    exp_cmd.add_argument("--allow-failed", action="store_true",
                         help="export even if validation after cleaning failed")
    init_cmd = sub.add_parser("init", help="write a starter config for a data file")
    init_cmd.add_argument("file", help="a CSV or Excel file")
    init_cmd.add_argument("--name", default=None, help="dataset name (default: from the file name)")
    init_cmd.add_argument("--out", default=None, help="config file to write (default: configs/<name>.yaml)")
    init_cmd.add_argument("--force", action="store_true", help="overwrite an existing config")
    args = parser.parse_args(argv)
    if args.command == "init":
        from dqp.init import InitError, write_config
        try:
            target, notes = write_config(args.file, args.out, args.name, args.force)
        except (InitError, FileNotFoundError) as exc:
            print(f"[failed] {exc}", file=sys.stderr)
            return 1
        print(f"wrote {target}")
        for n in notes:
            print(f"  - {n}")
        print(f"next: check it, then  dqp run {target}")
        return 0
    if args.command == "export":
        from dqp.export import ExportError, export_run
        try:
            m = export_run(args.run, args.format, args.tables, args.out, args.allow_failed,
                           args.keep_row_numbers, args.include_rejected)
        except ExportError as exc:
            print(f"[not exported] {exc}", file=sys.stderr)
            return 1
        note = " (validation FAILED: exported because --allow-failed)" if m["exported_despite_failure"] else ""
        print(f"exported {len(m['files'])} table(s) to {m['out']}/{note}")
        for f in m["files"]:
            print(f"  {f['file']:<48} {f['rows']:>10,} rows")
        return 0
    if args.command == "snapshot":
        from dqp.site import make_snapshot
        try:
            out = make_snapshot(args.run, args.out)
        except (FileNotFoundError, ValueError) as exc:
            print(f"[failed] {exc}", file=sys.stderr)
            return 1
        print(f"snapshot written to {out}/")
        return 0
    if args.command == "site":
        from dqp.site import build_site
        try:
            out = build_site(args.out, args.runs, args.repo_url)
        except (FileNotFoundError, ValueError) as exc:
            print(f"[failed] {exc}", file=sys.stderr)
            return 1
        print(f"site written to {out}/ ({len(args.runs)} report(s))")
        return 0
    invalid = 0

    failed = 0
    for path in args.configs:
        try:
            log = run(path)
        except ValidationError as exc:
            print(f"[config error] {path}\n{exc}", file=sys.stderr)
            failed += 1
            continue
        except Exception as exc:
            print(f"[failed] {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
            continue

        print(f"[{log['status']}] {log['dataset']}  run {log['run_id']}  -> {log['db_path']}")
        for l in log["loads"]:
            if l["status"] == "skipped":
                print(f"  [skipped] sheet {l['sheet']!r}: {'; '.join(l['notes'])}")
                continue
            print(f"  {l['table']:<48} {l['rows']:>9,} rows x {l['columns']:>3} cols")
            for note in l["notes"]:
                print(f"      - {note}")
        if args.verbose and log.get("highlights"):
            print("  profile highlights:")
            for h in log["highlights"]:
                mark = "!" if h["level"] == "attention" else "-"
                print(f"   {mark} [{h['table']}] {h['text']}")

        v = log["validation"]
        _print_stage("on arrival (raw)", v["stages"]["raw"], log["checks"], "raw", args.verbose)

        changes = [e for e in log["cleaning_log"]
                   if e["action"] not in ("load_typed",) and (e["rows_removed"] or e["values_changed"]
                                                              or e["action"] in ("build_mart", "rename", "snake_case",
                                                                                 "drop_columns"))]
        rejected = sum(c["rows_rejected"] for c in log["cleaning"].values())
        print(f"  cleaning: {len(changes)} change(s), {rejected} row(s) moved to rejected_ tables")
        for e in changes:
            counts = []
            if e["rows_removed"]:
                counts.append(f"{e['rows_removed']} row(s) removed")
            if e["values_changed"]:
                counts.append(f"{e['values_changed']} value(s) changed")
            if e["action"] == "build_mart":
                counts.append(f"{e['rows_after']} rows")
            text = e["details"] if e["action"] != "build_mart" else (e["reason"] or "")
            print(f"   - [{e['table']}] {e['action']}: {_cut(text, 150)}" + (f" [{', '.join(counts)}]" if counts else ""))
            if e["action"] in ("drop_rows", "drop_duplicates", "replace", "drop_columns", "fill_forward", "type_cast"):
                print(f"       reason: {_cut(e['reason'], 150)}")

        for t, c in log["coverage"].items():
            if c["missing_period_count"]:
                shown = ", ".join(c["missing_periods"][:4]) + (" ..." if c["missing_period_count"] > 4 else "")
                print(f"   gap  [{t}] {c['missing_period_count']} missing {c['frequency']} period(s): {shown}")
            holes = {col: v for col, v in c["columns"].items() if v["internal_missing"]}
            if holes:
                first = list(holes.items())[:3]
                shown = ", ".join(f"{col} ({', '.join(v['internal_missing_dates'][:2])})" for col, v in first)
                print(f"   gap  [{t}] {len(holes)} column(s) have values missing inside their date range, "
                      f"documented, not filled: {shown}" + (" ..." if len(holes) > 3 else ""))

        _print_stage("after cleaning", v["stages"]["staging"], log["checks"], "staging", True)
        if v["status"] == "failed":
            invalid += 1

    if failed:
        return 1
    return 2 if (args.strict and invalid) else 0


def _print_stage(title: str, result: dict, checks: dict, stage: str, show_details: bool) -> None:
    counts = "  ".join(f"{k} {n}" for k, n in result["summary"].items() if n)
    print(f"  validation {title}: {result['status'].upper().replace('_', ' ')}  ({counts})")
    order = {"ERROR": 0, "FAIL": 1, "WARN": 2, "INFO": 3}
    flagged = sorted((c for c in checks.values() if c["stage"] == stage and c["status"] != "PASS"),
                     key=lambda c: (order[c["status"]], c["table"]))
    for c in flagged:
        print(f"   {c['status']:<5} [{c['table']}] {c['name']}: {c['detail']}")
        if show_details:
            for e in c["examples"][:2]:
                print(f"           e.g. {_short(e)}")


def _cut(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _short(example: dict, width: int = 110) -> str:
    text = ", ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in example.items())
    return text if len(text) <= width else text[: width - 3] + "..."


if __name__ == "__main__":
    sys.exit(main())
