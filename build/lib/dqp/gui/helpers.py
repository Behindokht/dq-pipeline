"""Plain functions behind the GUI, kept apart from the page so they can be tested
and reused without Streamlit running."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from dqp.init import Analysis


def rule_label(rule: dict) -> str:
    kind, v = next((k, x) for k, x in rule.items() if k not in ("table", "severity", "name"))
    if kind == "not_null":
        return f"{', '.join(v)}: never empty"
    if kind == "unique":
        return f"{', '.join(v)}: no value twice"
    if kind == "between":
        return f"{v['column']}: between {v.get('min')} and {v.get('max')}"
    if kind == "allowed":
        return f"{v['column']}: one of {', '.join(map(str, v['values']))}"
    if kind == "date_coverage":
        return f"{v['column']}: no missing periods from {v.get('start')}"
    return f"{kind}: {v}"


def sql_name(c: str) -> str:
    return c if c.replace("_", "a").isalnum() and c.islower() and not c[0].isdigit() else '"' + c.replace('"', '""') + '"'


def sql_text(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def portable(cfg: dict, an: Analysis) -> str:
    """The config as a file to keep: the data file next to it, outputs in outputs/."""
    cfg = copy.deepcopy(cfg)
    cfg.pop("output_dir", None)
    src = cfg["sources"][0]
    if src["type"] in ("csv", "excel"):
        src["path"] = f"data/{Path(src['path']).name}"
    head = (f"# Written by the dqp GUI for {an.label}.\n"
            "# Run it again with:  dqp run <this file>"
            + ("   (put the data file in data/ next to it)\n" if src["type"] != "gsheet" else "\n"))
    return head + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)


def parse_rows(txt: str):
    txt = txt.strip().replace("–", "-")
    if "-" in txt:
        a, b = (int(x) for x in txt.split("-", 1))
        return list(range(a - 1, b))
    return int(txt) - 1


def is_number(x: str) -> bool:
    try:
        float(x)
        return True
    except ValueError:
        return False
