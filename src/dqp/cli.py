"""Command line entry point: `dqp run configs/copper.yaml`."""

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
    args = parser.parse_args(argv)
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
