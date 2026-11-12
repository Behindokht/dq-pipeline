"""Dataset configuration.

Every dataset is described by one YAML file. The YAML is validated with pydantic
before anything runs, so a typo in a config fails loudly at the start instead of
producing a half-finished run.

Relative paths in the config are resolved against the current working directory
(the repo root when you run `dqp run configs/<name>.yaml`).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class _Strict(BaseModel):
    # Unknown keys are an error: a misspelled option should never be silently ignored.
    model_config = ConfigDict(extra="forbid")


class CsvSource(_Strict):
    type: Literal["csv"]
    name: str = Field(pattern=NAME_PATTERN, description="Becomes the table name raw_<name>.")
    path: Path
    delimiter: str = ","
    header: bool = True
    encoding: Literal["utf-8", "utf-16", "latin-1"] = "utf-8"
    skip_rows: int = Field(0, ge=0)


HeaderRows = Union[int, list[int]]


def _as_header_list(value: HeaderRows) -> list[int]:
    rows = [value] if isinstance(value, int) else list(value)
    if not rows or any(r < 0 for r in rows):
        raise ValueError("header_rows must be one or more 0-based row numbers")
    if rows != sorted(set(rows)):
        raise ValueError("header_rows must be increasing and without repeats")
    return rows


class SheetSpec(_Strict):
    """Per-sheet settings, for workbooks where every sheet has its own layout."""
    sheet: str
    header_rows: HeaderRows | None = Field(
        None, description="Row(s) holding column names, 0-based. Several rows are joined, e.g. [5, 6, 7].")

    @field_validator("header_rows")
    @classmethod
    def _check_rows(cls, v):
        return None if v is None else _as_header_list(v)


class ExcelSource(_Strict):
    """Reads .xlsx and legacy .xls; the format is detected from the file content,
    so names like data.xlsx.xlsx work."""
    type: Literal["excel"]
    name: str = Field(pattern=NAME_PATTERN, description="Prefix for the tables raw_<name>__<sheet>.")
    path: Path
    sheets: Union[Literal["all"], list[Union[str, SheetSpec]]] = "all"
    header_rows: HeaderRows = Field(
        0, description="Default header row(s) for sheets without their own setting. Rows above are title lines.")
    na_values: list[str] = Field(
        default_factory=list, description="Extra cell values that mean 'missing', e.g. ['…', 'n/a'].")

    @field_validator("header_rows")
    @classmethod
    def _check_rows(cls, v):
        return _as_header_list(v)

    def sheet_specs(self, available: list[str]) -> list[SheetSpec]:
        if self.sheets == "all":
            return [SheetSpec(sheet=s) for s in available]
        return [SheetSpec(sheet=s) if isinstance(s, str) else s for s in self.sheets]


Source = Annotated[Union[CsvSource, ExcelSource], Field(discriminator="type")]


class ProfilingConfig(_Strict):
    type_threshold: float = Field(0.95, gt=0.5, le=1.0,
                                  description="Share of values that must fit a type to call the column that type.")
    outlier_iqr_k: float = Field(3.0, gt=0, description="Outlier if outside Q1 - k*IQR .. Q3 + k*IQR.")
    max_examples: int = Field(3, ge=1, le=20)


Severity = Literal["fail", "warn", "info"]


class Between(_Strict):
    column: str
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _one_bound(self):
        if self.min is None and self.max is None:
            raise ValueError("between needs min, max or both")
        return self


class Allowed(_Strict):
    column: str
    values: list[Union[str, float, int, bool]] = Field(min_length=1)


class RowCount(_Strict):
    min: int | None = Field(None, ge=0)
    max: int | None = Field(None, ge=0)


class DateCoverage(_Strict):
    column: str
    start: str | None = Field(None, description="Data must start on or before this date.")
    end: str | None = Field(None, description="Data must reach at least this date.")
    no_gaps: bool = False


RULE_KINDS = ("expr", "unique", "not_null", "between", "allowed", "row_count", "date_coverage", "sql")


class Rule(_Strict):
    """One user-defined check. Exactly one of the rule kinds must be given:

      expr:          SQL condition every row must meet, e.g.  "amount >= 0"
      unique:        columns that together identify a row, e.g. [merchant_id, date]
      not_null:      columns that must always have a valid value
      between:       {column, min, max}
      allowed:       {column, values}
      row_count:     {min, max}
      date_coverage: {column, start, end, no_gaps}
      sql:           a query returning the FAILING rows; {table} is replaced
                     by the table being checked (for checks across rows or tables)

    Rules see typed values (numbers as numbers, dates as dates). A value that
    could not be typed is NULL; NULLs are not counted as failing an expr,
    between or allowed rule (use not_null for that)."""
    table: str
    name: str | None = None
    stage: Literal["staging", "raw"] = Field(
        "staging", description="staging (default): check the cleaned table or a mart. raw: check the data as it arrived.")
    severity: Severity = "fail"
    max_failing: int = Field(0, ge=0, description="Failing rows tolerated before the rule triggers.")
    description: str = ""

    expr: str | None = None
    unique: list[str] | None = None
    not_null: list[str] | None = None
    between: Between | None = None
    allowed: Allowed | None = None
    row_count: RowCount | None = None
    date_coverage: DateCoverage | None = None
    sql: str | None = None

    @model_validator(mode="after")
    def _exactly_one_kind(self):
        given = [k for k in RULE_KINDS if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(f"a rule needs exactly one of {list(RULE_KINDS)}, got {given or 'none'}")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in RULE_KINDS if getattr(self, k) is not None)


GENERIC_CHECKS = ("table_not_empty", "no_duplicate_rows", "values_fit_type", "no_empty_columns",
                  "no_duplicate_dates", "no_missing_periods")


class ValidationConfig(_Strict):
    rules: list[Rule] = Field(default_factory=list)
    generic: dict[str, Union[Severity, Literal["off"]]] = Field(
        default_factory=dict, description="Change the severity of a generic check, or switch it off.")

    @field_validator("generic")
    @classmethod
    def _known_checks(cls, v):
        unknown = sorted(set(v) - set(GENERIC_CHECKS))
        if unknown:
            raise ValueError(f"unknown generic checks {unknown}; known: {list(GENERIC_CHECKS)}")
        return v


# ---------------------------------------------------------------- cleaning

Scalar = Union[str, float, int, bool, None]
Reason = Annotated[str, Field(min_length=3, description="Why this change is made. Required: it goes into the cleaning log.")]


class DropRows(_Strict):
    where: str = Field(description="SQL condition on the staging table; matching rows move to rejected_<table>.")
    reason: Reason


class DropDuplicates(_Strict):
    columns: list[str] | None = Field(None, description="Key columns. Omit for exact duplicates (all columns).")
    reason: Reason = "exact duplicate of an earlier row"


class Replace(_Strict):
    column: str
    values: dict[str, Scalar] = Field(min_length=1, description="old value -> new value")
    reason: Reason


class DropColumns(_Strict):
    columns: list[str] = Field(min_length=1)
    reason: Reason


class FillForward(_Strict):
    column: str
    order_by: str = Field(description="Column that orders the rows, usually the date.")
    limit: int | None = Field(None, ge=1, description="Fill at most this many consecutive missing values.")
    reason: Reason


STEP_KINDS = ("rename", "snake_case", "trim_whitespace", "drop_rows", "drop_duplicates",
              "replace", "drop_columns", "fill_forward")


class CleaningStep(_Strict):
    """One cleaning step. Exactly one kind per step; steps run in the order written.

      rename:          {old name: new name}
      snake_case:      true   -> every column name to lower_snake_case
      trim_whitespace: true   -> strip spaces around text values
      drop_rows:       {where, reason}            rows move to rejected_<table>
      drop_duplicates: {columns, reason}          later copies move to rejected_<table>
      replace:         {column, values, reason}   e.g. {"n.a.": null}
      drop_columns:    {columns, reason}
      fill_forward:    {column, order_by, limit, reason}  adds <column>_filled flag
    """
    rename: dict[str, str] | None = None
    snake_case: Literal[True] | None = None
    trim_whitespace: Literal[True] | None = None
    drop_rows: DropRows | None = None
    drop_duplicates: DropDuplicates | None = None
    replace: Replace | None = None
    drop_columns: DropColumns | None = None
    fill_forward: FillForward | None = None

    @model_validator(mode="after")
    def _exactly_one_kind(self):
        given = [k for k in STEP_KINDS if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(f"a cleaning step needs exactly one of {list(STEP_KINDS)}, got {given or 'none'}")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in STEP_KINDS if getattr(self, k) is not None)


class Mart(_Strict):
    name: str = Field(pattern=NAME_PATTERN, description="Becomes the table mart_<name>.")
    description: str = ""
    sql: str = Field(description="SELECT over stg_<table> tables (and earlier marts).")


class SummaryConfig(_Strict):
    """Layer 5: the plain-language summary.

    provider auto: use Anthropic if ANTHROPIC_API_KEY is set, else an
    OpenAI-compatible API if OPENAI_API_KEY is set, else the template.
    API keys are read from environment variables only, never from this file."""
    provider: Literal["auto", "none", "anthropic", "openai"] = "auto"
    model: str | None = Field(None, description="Model name; anthropic defaults to claude-sonnet-5-5.")
    base_url: str | None = Field(None, description="For OpenAI-compatible servers (Azure, Ollama, ...).")
    api_key_env: str | None = Field(None, description="Env var holding the key; default per provider.")
    max_retries: int = Field(1, ge=0, le=3, description="Retries with the checker's feedback before falling back.")
    max_words: int = Field(300, ge=50, le=2000)
    key_columns: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Headline numbers per table (staging or mart), e.g. {monthly_series: [cu_usd]}. "
                    "Default: the first 5 numeric columns of each mart.")


class DatasetConfig(_Strict):
    dataset: str = Field(pattern=NAME_PATTERN)
    description: str = ""
    sources: list[Source] = Field(min_length=1)
    profiling: ProfilingConfig = Field(default_factory=ProfilingConfig)
    cleaning: dict[str, list[CleaningStep]] = Field(
        default_factory=dict, description="Steps per table, keyed by table name (e.g. artprice__data).")
    marts: list[Mart] = Field(default_factory=list)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    output_dir: Path | None = None

    @field_validator("marts")
    @classmethod
    def _unique_marts(cls, marts: list) -> list:
        names = [m.name for m in marts]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"mart names must be unique, duplicated: {dupes}")
        return marts

    @field_validator("sources")
    @classmethod
    def _unique_source_names(cls, sources: list) -> list:
        names = [s.name for s in sources]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"source names must be unique, duplicated: {dupes}")
        return sources

    @model_validator(mode="after")
    def _default_output_dir(self) -> "DatasetConfig":
        if self.output_dir is None:
            self.output_dir = Path("outputs") / self.dataset
        return self


def load_config(path: str | Path) -> tuple[DatasetConfig, str]:
    """Read and validate a YAML config. Returns the config and a short hash of the
    file contents, which goes into the run log so every output can be traced back
    to the exact config that produced it."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: config must be a YAML mapping")
    config = DatasetConfig.model_validate(raw)
    return config, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def slugify(text: str) -> str:
    """Turn a sheet name like 'Prices 1998-2025' into 'prices_1998_2025'."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "sheet"
