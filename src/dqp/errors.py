"""One plain sentence for anything that goes wrong, plus the details for a bug report.

The GUI shows the sentence and puts the details in a fold; the command line prints the
sentence and the details only with -v. Known cases get a sentence that says what to do;
anything else says that it is unexpected, so it is never mistaken for a data problem."""

from __future__ import annotations

import re
import traceback
import zipfile


def sql_problem(message: str) -> str:
    """A DuckDB error message as a short explanation of what is wrong in the SQL."""
    first = str(message).strip().splitlines()[0] if str(message).strip() else str(message)
    m = re.search(r'Referenced column "([^"]+)" not found', first)
    if m:
        return f"there is no column called {m.group(1)!r}"
    m = re.search(r"syntax error at or near (.+)$", first)
    if m:
        return f"the SQL is not complete or not valid (near {m.group(1)})"
    if "syntax error at end of input" in first:
        return "the SQL stops too early, something is missing at the end"
    m = re.search(r"Cannot compare values of type (\w+) and (?:type )?(\w+)", first) or \
        re.search(r"Cannot compare (\w+) and (\w+)", first)
    if m:
        return f"it compares a {_typename(m.group(1))} with a {_typename(m.group(2))}"
    m = re.search(r"Could not convert string '([^']*)' to (\w+)", first)
    if m:
        return f"the text {m.group(1)!r} cannot be read as a {_typename(m.group(2))}"
    if "No function matches" in first:
        return "a function or operator is used on the wrong kind of value: " + _after_colon(first)
    return _after_colon(first)


def _typename(t: str) -> str:
    t = t.upper()
    if t in ("VARCHAR", "STRING", "TEXT"):
        return "text"
    if t in ("DATE", "TIMESTAMP", "TIMESTAMP_NS", "TIME"):
        return "date"
    if t == "BOOLEAN":
        return "true/false value"
    return "number"


def _after_colon(line: str) -> str:
    # "Binder Error: ..." -> "..."
    return re.sub(r"^\w+ Error:\s*", "", line)


def explain(exc: BaseException) -> tuple[str, str]:
    """(sentence, details). The sentence is for a person; details is the traceback."""
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return _sentence(exc), details


def _sentence(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc).strip()
    first = msg.splitlines()[0] if msg else ""

    if name in ("CalamineError", "BadZipFile", "InvalidFileException", "XLRDError") \
            or isinstance(exc, zipfile.BadZipFile) or "Cannot detect file format" in msg \
            or "encrypted" in msg.lower() or "password" in msg.lower():
        return ("The workbook could not be opened. It may be damaged, not really an Excel file, or "
                "protected with a password. Open it in Excel, save a copy without a password, and try that copy.")
    if name == "CleaningError":
        m = re.match(r"(?:stg_)?(\S+?), cleaning step (\d+) \((\w+)\): (.*)", first)
        if m:
            table, step, kind, rest = m.groups()
            return (f"Cleaning step {step} ({kind.replace('_', ' ')}) on table {table!r} could not be "
                    f"applied: {sql_problem(rest)}. Check that step's condition or column names.")
        return f"A cleaning step could not be applied: {first}"
    if name == "ValidationError" and hasattr(exc, "errors"):
        lines = [f"{'.'.join(str(p) for p in e.get('loc', ())) or 'settings'}: {e.get('msg')}"
                 for e in exc.errors()[:5]]
        return "The settings do not fit together: " + "; ".join(lines) + "."
    if name in ("InitError", "CsvLoadError", "ExportError", "GSheetError", "ExcelLoadError"):
        return first if first.endswith(".") else first + "."
    if isinstance(exc, FileNotFoundError):
        return (first[0].upper() + first[1:] if first else "A file was not found") + "."
    if isinstance(exc, MemoryError) or "Out of Memory" in msg:
        return "There was not enough memory for this data. Close other programs or try a smaller file."
    if type(exc).__module__.startswith("duckdb") or name.endswith("Exception") and "Error:" in first:
        return f"The database could not run a query: {sql_problem(first)}."
    return (f"Something unexpected went wrong ({name}: {first[:200]}). This is probably a bug in dqp; "
            "the details below help to report it.")
