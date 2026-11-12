"""`dqp init <file>`: write a starter config by looking at the file.

What it decides (and writes as active settings):
  - csv or excel, from the file's content (so data.xlsx.xlsx or data.csv.xls work)
  - CSV: delimiter, header, lines to skip, encoding
  - Excel: which sheets hold a table, and on which row(s) each table's header is
    (title lines above it, headers spread over several rows), missing-value markers

What it only proposes (written as comments, for a person to accept):
  - columns to keep as text (IDs and codes that look numeric)
  - cleaning steps, e.g. removing footer rows or duplicates; a person must write the reason
  - validation rules suggested by the current data; keep only those that must always hold

Nothing is cleaned or judged automatically: init proposes, the person decides.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
import re
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from dqp.config import DatasetConfig, slugify
from dqp.connectors import load_source
from dqp.profiling import ProfileSettings, profile_table
from dqp.profiling.profiler import DATE_FORMATS

SCAN_ROWS = 60
PLACEHOLDERS = ("…", "..", "...", "-", "--", "n/a", "N/A", "NA", "#N/A", "null", "NULL", "?")
BUILTIN_NA = {"…", "..", "n/a", "N/A", "#N/A"}
ID_NAME = re.compile(r"(^|[\s_])(id|code|invoice|number|nr|no|zip|postcode|sku|iban|account|ean)($|[\s_])",
                     re.IGNORECASE)
_DATE_RES = [re.compile(p) for p in DATE_FORMATS.values()]


class InitError(ValueError):
    pass


# ---------------------------------------------------------------- file type

def detect_type(path: Path) -> str:
    head = path.read_bytes()[:8]
    if head.startswith(b"PK"):                         # xlsx / xlsm are zip files
        return "excel"
    if head.startswith(b"\xd0\xcf\x11\xe0"):           # legacy .xls (OLE2)
        return "excel"
    return "csv"


# ---------------------------------------------------------------- CSV

def _encoding(path: Path) -> str:
    raw = path.read_bytes()[: 2 << 20]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as exc:
        # a multi-byte character cut at the 2 MB boundary is still utf-8
        if exc.start >= len(raw) - 3:
            return "utf-8"
        return "latin-1"


DELIMITERS = (",", ";", "\t", "|")


def detect_csv(path: Path) -> dict:
    """Delimiter: the candidate that appears the same number of times on most lines.
    Lines above the first such line are title lines to skip. A footer line with a
    different number of fields does not change the outcome."""
    enc = _encoding(path)
    with open(path, encoding=enc, errors="replace", newline="") as f:
        lines = [ln.rstrip("\r\n") for _, ln in zip(range(200), f)]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        raise InitError(f"{path}: the file is empty")
    best = None
    for d in DELIMITERS:
        counts = [_count_outside_quotes(ln, d) for ln in lines]
        common = max(set(counts), key=counts.count)
        if common == 0:
            continue
        share = counts.count(common) / len(counts)
        key = (share, common)
        if best is None or key > best[0]:
            best = (key, d, counts, common)
    if best is None:
        raise InitError(f"{path}: no delimiter found; is it really a CSV file?")
    _, delim, counts, common = best
    skip = next(i for i, c in enumerate(counts) if c == common)
    first = _split(lines[skip], delim)
    header = sum(_kind(v) == "text" for v in first) >= 0.5 * len(first)
    settings = {"delimiter": delim, "header": header, "encoding": enc}
    if skip:
        settings["skip_rows"] = skip
    return settings


def _count_outside_quotes(line: str, d: str) -> int:
    n, quoted = 0, False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == d and not quoted:
            n += 1
    return n


def _split(line: str, d: str) -> list[str]:
    import csv
    return next(csv.reader([line], delimiter=d))


# ---------------------------------------------------------------- Excel

def _kind(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "empty"
    if isinstance(v, (dt.date, dt.datetime, pd.Timestamp)):
        return "date"
    if isinstance(v, bool):
        return "text"
    if isinstance(v, (int, float)):
        return "num"
    s = str(v).strip()
    if not s:
        return "empty"
    if any(r.fullmatch(s) for r in _DATE_RES):
        return "date"
    try:
        float(s.replace(",", ""))
        return "num"
    except ValueError:
        return "text"


def detect_header_rows(grid: pd.DataFrame) -> tuple[list[int] | None, str]:
    """Find the header row(s) above the first block of data rows.

    A data row has at least half the table's width filled, mostly with numbers or dates,
    and the next rows look the same. The block is then extended upward over sparse rows
    that are still mostly numbers or dates. The header is the nearest non-empty row above it,
    mostly text; rows above that are added while they are text over several columns
    (group labels of a multi-row header). Single-cell rows are titles and stop the search."""
    kinds = [[_kind(v) for v in row] for row in grid.itertuples(index=False)]
    lengths = [[len(str(v).strip()) for v, k in zip(row, kr) if k != "empty"]
               for row, kr in zip(grid.itertuples(index=False), kinds)]
    filled = [sum(k != "empty" for k in r) for r in kinds]
    width = max(filled, default=0)
    if width == 0:
        return None, "empty sheet"

    def typed_share(i):
        return sum(k in ("num", "date") for k in kinds[i]) / max(filled[i], 1)

    def text_share(i):
        return sum(k == "text" for k in kinds[i]) / max(filled[i], 1)

    def data_like(i):
        return filled[i] >= max(2, 0.5 * width) and typed_share(i) >= 0.5

    n = len(kinds)
    start = next((i for i in range(n) if data_like(i)
                  and sum(data_like(j) for j in range(i + 1, min(i + 4, n))) >= min(2, n - i - 1)), None)
    if start is None:
        return _text_table(filled, width, lengths)
    # the first data rows of a series are often sparse (a level but no return yet):
    # walk up over rows that are mostly numbers or dates, however few cells they fill
    while start > 0 and (filled[start - 1] == 0 or typed_share(start - 1) >= 0.5):
        if filled[start - 1] == 0 and (start - 2 < 0 or typed_share(start - 2) < 0.5 or filled[start - 2] == 0):
            break                       # a blank line between header and data: stop at the data
        start -= 1
    while filled[start] == 0:
        start += 1
    h = start - 1
    while h >= 0 and filled[h] == 0:
        h -= 1
    if h < 0 or filled[h] < 2 or text_share(h) < 0.5:
        return [max(start - 1, 0)] if start > 0 else [0], "no clear header row; check the column names"
    rows = [h]
    k = h - 1

    def group_label(i):
        # text over the data columns; a single cell counts only if it is not in the first
        # column (a title sits in the first column, a group label above the data)
        first_col = next((j for j, x in enumerate(kinds[i]) if x != "empty"), 0)
        return (text_share(i) >= 0.8 and filled[i] <= filled[rows[0]]
                and (filled[i] >= 2 or (filled[i] == 1 and first_col > 0)))
    while k >= 0 and filled[k] > 0 and group_label(k):
        rows.insert(0, k)
        k -= 1
    note = f"header on row {rows[0] + 1}" if len(rows) == 1 else \
        f"header spread over rows {rows[0] + 1}-{rows[-1] + 1}"
    if rows[0] > 0:
        note += f", {rows[0]} line(s) above it skipped"
    return rows, note


def _text_table(filled: list[int], width: int, lengths: list[list[int]]) -> tuple[list[int] | None, str]:
    """Fallback for tables that are mostly text (customer lists, deal stages, owners):
    the header is the first row of at least 2 filled cells that is followed by rows
    filled about as wide. Notes are not a table: one or two cells per line, or long
    sentences rather than short labels, IDs and names."""
    import statistics
    full = max(2, int(0.6 * width + 0.5))
    n = len(filled)

    def short(i):
        return bool(lengths[i]) and statistics.median(lengths[i]) <= 40
    for r in range(n):
        if filled[r] < full or not short(r):
            continue
        below = [filled[j] >= full and short(j) for j in range(r + 1, min(r + 4, n))]
        if len(below) >= 2 and sum(below) >= 2:
            note = f"text table, header assumed on row {r + 1}"
            return [r], note + (f", {r} line(s) above it skipped" if r else "")
    return None, "no block of numeric or dated rows and no text table: looks like notes"


def detect_excel(path: Path) -> tuple[list[dict], list[str]]:
    book = pd.ExcelFile(path, engine=_engine())
    specs, notes = [], []
    for sheet in book.sheet_names:
        grid = pd.read_excel(book, sheet_name=sheet, header=None, nrows=SCAN_ROWS, dtype=object)
        rows, why = detect_header_rows(grid)
        if rows is None:
            notes.append(f"sheet {sheet!r} left out: {why}")
            continue
        body = grid.iloc[rows[-1] + 1:]
        markers = sorted({str(v).strip() for v in body.to_numpy().ravel()
                          if isinstance(v, str) and v.strip() in PLACEHOLDERS} - BUILTIN_NA)
        specs.append({"sheet": sheet, "header_rows": rows if len(rows) > 1 else rows[0],
                      "na_values": markers, "note": why})
        notes.append(f"sheet {sheet!r}: {why}" + (f"; missing-value marker(s) {markers}" if markers else ""))
    if not specs:
        raise InitError(f"{path}: no sheet looks like a table ({'; '.join(notes)})")
    return specs, notes


def _engine():
    try:
        import python_calamine  # noqa: F401
        return "calamine"
    except ImportError:
        return None


# ---------------------------------------------------------------- analysis

@dataclass
class Suggestion:
    """One proposal for the config. `config` is exactly what goes into the YAML:
    a list of column names (text_columns), one cleaning step, one rule, or
    {check: setting} (generic). `note` says in plain words why it is proposed."""
    section: str                 # text_columns | cleaning | rules | generic
    table: str                   # table name as the config uses it ('' for generic)
    config: object
    note: str = ""
    needs_reason: bool = False   # a person has to write the reason (cleaning steps that remove rows)

    @property
    def id(self) -> str:
        digest = hashlib.sha1(yaml.safe_dump(self.config, sort_keys=True).encode()).hexdigest()[:8]
        return f"{self.section}:{self.table}:{digest}"


@dataclass
class Analysis:
    dataset: str
    source: dict                 # the source block of the config (the sheet link for Google Sheets)
    local_path: Path             # the file that was read (the downloaded copy for Google Sheets)
    label: str                   # how to name the input in text: 'sales.csv', 'the Google Sheet'
    notes: list[str]
    profiles: dict               # raw table -> profile
    suggestions: list[Suggestion] = field(default_factory=list)
    final_names: dict = field(default_factory=dict)   # raw table -> {column: name after suggested cleaning}

    def config(self, accepted: list[Suggestion] | None = None, description: str | None = None) -> dict:
        """The config as a dict, with the given suggestions switched on."""
        cfg: dict = {"dataset": self.dataset}
        if description:
            cfg["description"] = description
        cfg["sources"] = [self.source]
        for s in accepted or []:
            if s.section == "text_columns":
                cfg.setdefault("text_columns", {})[s.table] = list(s.config)
            elif s.section == "cleaning":
                cfg.setdefault("cleaning", {}).setdefault(s.table, []).append(s.config)
            elif s.section == "rules":
                cfg.setdefault("validation", {}).setdefault("rules", []).append({"table": s.table, **s.config})
            elif s.section == "generic":
                cfg.setdefault("validation", {}).setdefault("generic", {}).update(s.config)
        return cfg


def analyse(path: str | Path, name: str | None = None, download_dir: str | Path = "data/downloads") -> Analysis:
    """Read a file (or a Google Sheets link), detect how it is laid out, profile it
    and propose text columns, cleaning steps and rules. Nothing is written."""
    sheet_url = str(path) if str(path).startswith("https://docs.google.com/spreadsheets/") else None
    if sheet_url:
        from dqp.connectors.gsheet import GSheetError, fetch
        try:
            p, fetched = fetch(sheet_url, Path(download_dir))
        except GSheetError as exc:
            raise InitError(str(exc)) from None
        title = name or "google_sheet"
    else:
        p, fetched = Path(path), []
        if not p.is_file():
            raise InitError(f"file not found: {p}")
        title = None
    ftype = detect_type(p)
    dataset = name or title or slugify(re.sub(r"(\.[A-Za-z0-9]+)+$", "", p.name)) or "dataset"
    if not re.match(r"^[a-z]", dataset):
        dataset = "d_" + dataset
    src_name = dataset[:40]
    notes: list[str] = fetched + [f"{p.name}: read as {ftype}"]

    if ftype == "csv":
        settings = detect_csv(p)
        source = {"name": src_name, "type": "csv", "path": str(path), **settings}
        notes.append(f"delimiter {settings['delimiter']!r}, encoding {settings['encoding']}, "
                     f"header {'yes' if settings['header'] else 'no'}")
    else:
        specs, sheet_notes = detect_excel(p)
        notes += sheet_notes
        markers = sorted({m for s in specs for m in s["na_values"]})
        source = {"name": src_name, "type": "excel", "path": str(p if sheet_url else path),
                  "sheets": [{"sheet": s["sheet"], "header_rows": s["header_rows"]} for s in specs]}
        if markers:
            source["na_values"] = markers

    an = Analysis(dataset, source, p, "the Google Sheet" if sheet_url else p.name, notes, {})
    an.profiles = profile_source(an.source, dataset, notes)
    if sheet_url:                                          # the config names the sheet, not the copy
        an.source = {"name": src_name, "type": "gsheet", "url": sheet_url,
                     **{k: v for k, v in source.items() if k in ("sheets", "na_values")}}
    an.suggestions, an.final_names = suggest(an.profiles)
    return an


def profile_source(source: dict, dataset: str, notes: list[str] | None = None) -> dict:
    """Load one source block (csv or excel) into memory and profile its tables."""
    cfg = DatasetConfig.model_validate({"dataset": dataset, "sources": [source]})   # fails loudly if init got it wrong
    con = duckdb.connect()
    try:
        loads = load_source(cfg.sources[0], con)
        profiles = {l.table: profile_table(con, l.table, ProfileSettings()) for l in loads if l.table}
    finally:
        con.close()
    if notes is not None:
        notes += [f"{l.table}: {l.rows:,} rows x {l.columns} columns" for l in loads if l.table]
    return profiles


def suggest(profiles: dict, skip: set[tuple[str, str]] | None = None) -> tuple[list[Suggestion], dict]:
    """Proposals from the profiles. Rules (and later cleaning steps) use the column
    names as they are after the proposed renaming steps; `skip` holds (table, kind)
    pairs for renaming steps a person turned down, so the names stay as they are."""
    skip = skip or set()
    out: list[Suggestion] = []

    # text columns: named like an ID or code, or numbers mixed with letter codes (85123A, C489449)
    for t, prof in profiles.items():
        cols = [c for c, p in prof["columns_profile"].items()
                if p["inferred_type"] in ("numeric", "mixed") and (_id_like(c) or _code_values(p))]
        if cols:
            out.append(Suggestion("text_columns", t.removeprefix("raw_"), cols,
                                  "IDs and codes that look numeric; kept as text so 001 and C123 are not judged as numbers"))

    final_names: dict[str, dict[str, str]] = {}
    for t, prof in profiles.items():
        short, cols = t.removeprefix("raw_"), prof["columns_profile"]
        names = {c: c for c in cols}
        date_col = prof["main_date_column"]
        if date_col and re.fullmatch(r"column_\d+", date_col) and "date" not in cols:
            out.append(Suggestion("cleaning", short, {"rename": {date_col: "date"}}, "the date column has no name in the file"))
            if (short, "rename") not in skip:
                names[date_col] = "date"
        renamed = [(c, _snake(names[c])) for c in cols if names[c] != _snake(names[c])]
        if renamed:
            c0, n0 = renamed[0]
            out.append(Suggestion("cleaning", short, {"snake_case": True},
                                  f"column names to lower_snake_case, e.g. {c0!r} -> {n0}; {len(renamed)} column(s)"))
            if (short, "snake_case") not in skip:
                names = {c: _snake(n) for c, n in names.items()}
        ws = [c for c, p in cols.items() if p["whitespace_values"]]
        if ws:
            out.append(Suggestion("cleaning", short, {"trim_whitespace": True},
                                  f"values with spaces around them, e.g. in {ws[0]!r}"))
        footer_col = _footer_column(prof)
        if footer_col:
            ex = cols[footer_col]["nonconforming_examples"][0]
            out.append(Suggestion("cleaning", short,
                                  {"drop_rows": {"where": f"{_sqlname(names[footer_col])} IS NULL", "reason": ""}},
                                  f"rows under the data that are not data, e.g. row {ex['row']}: {str(ex['value'])[:40]!r}",
                                  needs_reason=True))
        if prof["duplicate_rows"]:
            out.append(Suggestion("cleaning", short, {"drop_duplicates": {"reason": ""}},
                                  f"{prof['duplicate_rows']} exact duplicate row(s)", needs_reason=True))
        final_names[t] = names

    for prof in profiles.values():
        d = prof["main_date_column"]
        if d and prof["columns_profile"][d]["date"]["duplicate_dates"] > prof["rows"] * 0.2:
            out.append(Suggestion("generic", "", {"no_duplicate_dates": "off"},
                                  "many rows per date: looks like transactions, so a repeated date is normal"))
            break

    for t, prof in profiles.items():
        short, cols, names = t.removeprefix("raw_"), prof["columns_profile"], final_names[t]
        d = prof["main_date_column"]
        if d:
            info = cols[d]["date"]
            if not info["duplicate_dates"]:
                out.append(Suggestion("rules", short, {"unique": [names[d]]}, f"one row per {names[d]}"))
            if info["frequency"] not in ("unknown", "irregular") and not info.get("missing_periods"):
                out.append(Suggestion("rules", short,
                                      {"date_coverage": {"column": names[d], "start": str(info["min"]), "no_gaps": True}},
                                      f"no missing {info['frequency']} periods from {info['min']} on"))
        nonneg = [c for c, p in cols.items() if p.get("numeric") and p["numeric"]["count"]
                  and p["numeric"]["min"] >= 0 and not _id_like(c)][:3]
        for c in nonneg:
            out.append(Suggestion("rules", short, {"expr": f"{_sqlname(names[c])} >= 0"}, f"{names[c]} is never negative"))
        # never empty today, and nothing unreadable (unreadable values are empty after typing)
        full = [names[c] for c, p in cols.items() if p["nulls"] == 0 and p["nonconforming"] == 0
                and p["inferred_type"] != "empty" and not (p["inferred_type"] in ("numeric", "mixed")
                and (_id_like(c) or _code_values(p)))][:3]
        if full:
            out.append(Suggestion("rules", short, {"not_null": full}, f"{', '.join(full)}: never empty"))
    return out, final_names


# ---------------------------------------------------------------- config text

def build_config(path: str | Path, name: str | None = None) -> tuple[str, list[str]]:
    """Return (config text, notes). The config is validated and loaded once before
    it is returned, so the person gets a file that runs."""
    an = analyse(path, name)
    text = _render(an)
    DatasetConfig.model_validate(yaml.safe_load(text))     # the written file must load as written
    return text, an.notes


def _render(an: Analysis) -> str:
    filename = an.label
    out = [
        f"# Starter config written by `dqp init` for {filename}.",
        "# Active settings were detected from the file; check them once.",
        "# Lines starting with '#' are suggestions: uncomment the ones you agree with.",
        "# Cleaning steps need a reason in your own words: it goes into the cleaning log.",
        "",
        yaml.safe_dump({"dataset": an.dataset, "description": f"TODO: what {filename} contains and where it comes from"},
                       sort_keys=False, allow_unicode=True).rstrip(),
        "",
        "# header_rows count from 0, so header_rows: 3 is the 4th row as Excel shows it."
        if an.source["type"] != "csv" else "# Detected from the first lines of the file.",
        yaml.safe_dump({"sources": [an.source]}, sort_keys=False, allow_unicode=True).rstrip(),
        "",
    ]
    by = lambda sec: [s for s in an.suggestions if s.section == sec]

    if by("text_columns"):
        out.append("# IDs and codes that look numeric; keep them as text so 001 and C123 are not judged as numbers:")
        out.append("# text_columns:")
        out += [f"#   {s.table}: {_flow(s.config)}" for s in by("text_columns")]
        out.append("")

    out.append("# ------------------------------------------------------------------ cleaning")
    steps = by("cleaning")
    if steps:
        out.append("# cleaning:")
        for table in dict.fromkeys(s.table for s in steps):
            out.append(f"#   {table}:")
            for s in (s for s in steps if s.table == table):
                out += ["#" + line[1:] for line in _step_lines(s)]
    else:
        out.append("# No cleaning suggested: no footer rows, duplicates or odd column names found.")
    out.append("")

    out.append("# ------------------------------------------------------------------ validation")
    out.append("# Suggested by the current data: keep only rules that must ALWAYS hold.")
    out.append("# Column names assume the cleaning steps above are used.")
    out.append("# validation:")
    if by("generic"):
        out.append("#   generic:")
        for s in by("generic"):
            (k, v), = s.config.items()
            out.append(f"#     {k}: {v}      # {s.note.split(':')[0]}")
    if by("rules"):
        out.append("#   rules:")
        for s in by("rules"):
            (k, v), = s.config.items()
            out += [f"#     - table: {s.table}", f"#       {k}: {_inline(v)}"]
    out.append("")
    out.append("summary:\n  provider: auto            # an LLM if an API key is set, else the built-in template")
    return "\n".join(out) + "\n"


def _step_lines(s: Suggestion) -> list[str]:
    (kind, v), = s.config.items()
    if isinstance(v, dict) and kind != "rename":
        lines = [f"    - {kind}:".ljust(39) + f"# {s.note}"]
        for k, x in v.items():
            if k == "reason" and not x:
                x = "TODO: why these rows are not data (footer notes?)" if kind == "drop_rows" \
                    else "TODO: why a repeated row is a mistake here"
            lines.append(f"        {k}: {_inline(x)}")
        return lines
    return [f"    - {kind}: {_inline(v)}".ljust(39) + f"# {s.note}"]


def _inline(v) -> str:
    return yaml.safe_dump(v, default_flow_style=True, allow_unicode=True, width=10**6).strip().removesuffix("\n...")


def _footer_column(prof: dict) -> str | None:
    """A date column (or mostly-date column) whose unreadable values all sit in the
    last rows of the table: the classic footer note under the data."""
    cols = prof["columns_profile"]
    cands = [prof["main_date_column"]] + [c for c, p in cols.items() if p.get("mixed_dominant_type") == "date"]
    for c in (x for x in cands if x):
        p = cols[c]
        n = p["nonconforming"]
        if n and n <= 5 and all(e["row"] > prof["rows"] - n - 2 for e in p["nonconforming_examples"]):
            return c
    return None


def _id_like(name: str) -> bool:
    """'Customer ID', 'invoice_no', 'StockCode' (camelCase is split first)."""
    return bool(ID_NAME.search(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)))


def _code_values(p: dict) -> bool:
    """Values that fail as numbers are short letter+digit codes, not notes or text."""
    ex = [str(e["value"]) for e in p.get("nonconforming_examples", [])]
    return bool(ex) and all(re.fullmatch(r"[A-Za-z]{0,3}\d+[A-Za-z]{0,3}", v.strip()) for v in ex)


def _snake(name: str) -> str:
    from dqp.cleaning import snake
    return snake(name)


def _q(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else yaml.safe_dump(name).strip().removesuffix("\n...")


def _sqlname(name: str) -> str:
    return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) else '"' + name.replace('"', '""') + '"'


def _flow(items: list[str]) -> str:
    return "[" + ", ".join(_q(i) for i in items) + "]"


def write_config(path: str | Path, out: str | Path | None = None, name: str | None = None,
                 force: bool = False) -> tuple[Path, list[str]]:
    text, notes = build_config(path, name)
    dataset = yaml.safe_load(text)["dataset"]
    target = Path(out) if out else (Path("configs") if Path("configs").is_dir() else Path(".")) / f"{dataset}.yaml"
    if target.exists() and not force:
        raise InitError(f"{target} already exists; pass --force to overwrite it or --out to choose another file")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, notes
