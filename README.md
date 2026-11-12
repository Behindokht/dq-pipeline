# dq-pipeline

Give it any tabular dataset and it profiles, validates, cleans, documents and
summarises it, with an AI summary layer that only works on validated, computed numbers.

> **Status: work in progress.** Layers 1–5 and the HTML report are built; GitHub Actions and Pages are next.
> The table below is kept honest: nothing is marked done before it runs and is tested.

| Layer | What it does | Status |
|---|---|---|
| 1. Input | CSV and Excel (.xlsx and .xls, multi-sheet, messy layouts) into DuckDB raw tables, YAML config, run log | ✅ done |
| 1b. More inputs | Google Sheets, Parquet, JSON, SQL | ⏳ planned |
| 2. Profiling | types and values that don't fit them, missing values, duplicates, outliers, date frequency and gaps | ✅ done |
| 3. Validation | generic checks + rules from config, pass / warn / fail with failing rows | ✅ done |
| 4. Cleaning | raw → staging → mart, every step logged with a reason, removed rows kept, gaps documented | ✅ done |
| 5. AI summary | plain-language report; the LLM never types a number, a checker enforces it; template fallback | ✅ done (LLM path tested with fake clients, see below) |
| 6. Output | HTML report ✅ · GitHub Actions and Pages ⏳ | 🟡 in progress |

141 tests cover the layers (`pytest`).

## Example: what profiling found in my thesis data

Run on 6 source files from my MSc thesis (Artprice, Nareit, World Bank, FRED, OECD):

```
! [raw_artprice__data] 'column_1': 2 value(s) don't look date (row 114: 'Contact : econometrics@artprice.com '; ...)
! [raw_artprice__data] 'Global Index (USD)': 2 value(s) don't look numeric (row 114: '1900-04-09 00:00:00'; ...)
- [raw_master__sheet1] 10 column(s) are at least half empty: 'eth_usd_ret_real_pct' (67%), ...
```

- The Artprice download has a contact note and a description under the data, and
  Excel turned two footer numbers into dates in 1900. Nobody told the profiler
  where the data ends: it found these rows because they don't fit their column's type.
- On the first run it also flagged 52 values of `...` in World Bank columns such as
  Barley. That is a second "missing" marker next to `…`. The fix was one line in
  the dataset config (`na_values: ["..."]`), so the decision is visible and versioned.
- Copper (800 months) profiles clean: no gaps, no duplicate months, no type problems.
- Numbers were cross-checked against an independent pandas calculation.

## Example: validation before and after cleaning

| Dataset | On arrival (raw) | After cleaning (staging + marts) |
|---|---|---|
| Copper (800 months) | PASSED, 26 checks | PASSED, 39 checks |
| Thesis, 6 source files | FAILED: 2 Artprice footer rows with no valid date (1 FAIL, 4 WARN) | PASSED, 384 checks |

The run is judged on the cleaned data, because that is what moves on to the summary.
The arrival result is kept as a record of what the sources looked like.

What the rules check, beyond ranges:
- **Arithmetic inside the data.** Copper: `ratio = cu_usd / al_usd` and `ratio_ma12` is the
  12-month average. Thesis: log level = ln(index), return = 100 × change in log level,
  real = nominal − inflation, excess = real − risk-free.
- **Known exceptions are written down, not ignored.** The first copper run flagged
  October 2025 without a real price. That is a documented gap (no US CPI that month
  because of the federal shutdown), so the rule allows exactly that month and
  flags any other.
- **The master sheet traces back to its sources.** Two rules join my thesis master
  sheet to marts built from the raw downloads: the art columns equal the Artprice file
  in all 112 quarters, and the REIT index equals the Nareit quarter-end value.
- **The rules can fail.** On a deliberately damaged copy of the copper data (wrong ratio,
  deleted month, negative price, missing value, duplicated month) all five damages
  were caught. Changing one Artprice value by 0.5 makes the reconciliation rule fail
  and point at 2008Q4.

## Example: what cleaning did

```
cleaning: 10 change(s), 2 row(s) moved to rejected_ tables
 - [stg_artprice__data] drop_rows: rows where date IS NULL [2 row(s) removed]
     reason: footer notes under the data in the Artprice download (a contact line and a
     description); Excel stored numbers next to them as 1900 dates
 - [mart_reit_quarter_end] build_mart: last month of each complete quarter [216 rows]
 gap  [stg_monthly_series] 10 column(s) have values missing inside their date range,
      documented, not filled: cu_real (2025-10-01), al_real (2025-10-01), ...
```

- **Nothing disappears.** The two Artprice footer rows are in `rejected_artprice__data`,
  exactly as they were in the file, with the step and reason.
- **Gaps are documented, not filled.** Copper's October 2025 hole (no US CPI) is listed
  for every real-price column. Bitcoin in the thesis panel is recorded as a series that
  starts in 2014Q4, not as missing data.
- **A rule caught a mistake in my own mart.** The first REIT mart included 2026Q1,
  which held only January. The mart now keeps only complete quarters, which also
  removed 1971Q4 (Nareit starts in December 1971), and a rule guards it.

## The report

Every run writes `outputs/<dataset>/report.html`, one self-contained file (no server, no
JavaScript library, charts drawn as inline SVG), in light and dark mode and readable on a phone:

- **Verdict:** validation on arrival next to validation after cleaning
- **Summary:** the plain-language summary, every number taken from `facts.json`
- **Checks:** filter by "needs attention", stage or text; failing checks show example rows
- **Cleaning:** each step with its reason and row counts, and the rejected rows as they were
- **Gaps:** a coverage chart per table, with every missing value inside a series marked
- **Headline numbers:** latest, lowest, highest and one-year change, with a chart
- **Columns and lineage:** the profile of every cleaned column, and which file each table came from

`report.show_values: false` turns it into a quality-only report (statuses, counts, row numbers,
dates and reasons, no data values). The thesis config uses this because Artprice and Nareit
data is licensed.

## Example: the summary

From `outputs/copper/summary.md` (written by the template; no API key set):

```
- After cleaning, validation **passed**: 39 passed, 0 warning(s), 0 failure(s), 0 error(s) out of 39 checks.
## Gaps (documented, not filled)
- `stg_monthly_series`: 10 column(s) have values missing inside their own date range: `cu_real` (2025-10-01); ...
## Headline numbers
- `cu_usd`: latest 14,326 (2026-08-01); lowest 607 (1961-01-01), highest 14,326 (2026-08-01) since 1960-01-01; +48.1% since 2025-08-01.
```

**How the AI layer is kept honest**

1. The pipeline builds `facts.json`: every number the summary may use, each with an ID
   (`final.fail`, `clean1.rows_removed`, `key.monthly_series.cu_usd.latest`).
2. The LLM gets the facts and must write placeholders, `{{final.fail}}`, never numbers.
   The code fills in the values.
3. A checker rejects a draft with:
   - any digit or number word it wrote itself ("6", "about 24%", "doubled", "a third")
   - an unknown fact ID
   - a missing *required* fact: failures, problem counts, removed rows and documented gaps
     must be mentioned, so a summary cannot hide a problem by leaving it out
   - a causal claim ("because", "due to", "driven by"): the facts hold no causes
   - a headline number in a sentence that doesn't name its role (the lowest value in a
     sentence without "low/lowest/minimum"), which catches "copper is now at {{lowest}}"
4. A rejected draft gets one retry with the checker's feedback, then the built-in template
   takes over. Every draft, violation and the reason are kept in `summary.json`.
5. If validation after cleaning **failed**, the LLM is not asked at all and no headline
   numbers are given: the template reports the failures instead.

The template goes through the same checker, so the guarantee holds with or without an LLM.
The headline numbers were cross-checked against an independent pandas calculation.

**What the checker cannot do:** it guarantees every number is computed and every required
problem is mentioned. It cannot judge whether a sentence that passes these rules is still
misleading, or its tone. Say "every number in the summary is computed, not generated", not
"the summary is guaranteed correct".

**What is tested, honestly:** the checker, the retry and every fallback are tested with fake
LLM clients (one that follows the rules, four that break them in different ways, one whose
API call fails). The real Anthropic / OpenAI calls are small standard-library HTTP clients
that have not yet been run against the live APIs from this repo.

**Using an LLM:** set `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` plus `summary.model`) as an
environment variable or a GitHub Actions secret. Keys are never read from the config.
Only the fact pack is sent (aggregates, check names, cleaning reasons), never raw rows.

## How it works so far

```
config.yaml
   │
   ▼
1 connectors ──► raw_<t>        all text, exactly as loaded (never changed)
   │
2 profiling  ──► facts per column: type, bad values, nulls, outliers, dates, gaps
   │
3 validation ──► on arrival: generic checks + rules with stage: raw
   │
4 cleaning   ──► stg_<t>        typed, cleaned by config steps, _row = line in source
   │             rejected_<t>   every removed row, as it was, with step and reason
   │             mart_<name>    analysis tables built with SQL
   │
   ├─ profiling + gap documentation of the cleaned data
   └─ validation after cleaning: generic checks + rules   ◄── this result counts
   │
5 summary    ──► facts.json ──► LLM draft with {{fact}} placeholders, or the template
                              ──► checker ──► summary.md
```

Everything lands in `outputs/<dataset>/`:

| File | Contents |
|---|---|
| `<dataset>.duckdb` | `raw_`, `typed_`, `stg_`, `rejected_`, `mart_` tables, plus `_dqp_runs`, `_dqp_loads`, `_dqp_profile_columns`, `_dqp_highlights`, `_dqp_checks`, `_dqp_cleaning_log`, `_dqp_gaps`, `_dqp_coverage` (history across runs) |
| `profile.json` | every computed fact, raw and cleaned |
| `validation.json` | every check at both stages, failing rows, examples |
| `cleaning.json` | every step: what, why, rows and values affected, column lineage |
| `coverage.json` | missing periods, and per column the first/last date and internal gaps |
| `facts.json` | every number the summary may use, by ID |
| `summary.md` | the plain-language summary, numbers rendered from `facts.json` |
| `summary.json` | template or LLM, model, checker results, rejected drafts and why |
| `report.html` | the visual report: one self-contained file, opens offline in any browser |
| `run_log.json` | what ran, on which inputs (file fingerprints), with what result |

Design choices:

- **One internal format.** Every connector turns its source into DuckDB tables. The
  later layers never need to know whether the data came from Excel or a database,
  so adding a source type means writing one connector and nothing else.
- **Raw means raw.** Raw tables store every value as text, exactly as it arrived.
  Leading zeros (`001`), decimal commas (`1,50`) and odd date formats are not
  "fixed" silently at load time; typing happens in staging, where it is logged.
- **Real spreadsheets, not just clean grids.** Title lines above the data, headers
  spread over several rows with merged group labels, blank spacer columns, empty
  sheets, `…` placeholders, legacy `.xls` and names like `data.xlsx.xlsx` are all
  handled. Every change to the shape of the data is written to the run log.
- **Rules see types, failures point to rows.** Validation runs on `typed_<table>` views
  (numbers as numbers, dates as dates) and every failing check lists example rows
  with their row number in the original file.
- **A broken rule doesn't break the run.** A typo in a rule gives status ERROR with
  the database message; the other checks still run. `dqp run --strict` exits with
  code 2 on any FAIL or ERROR, for CI.
- **Cleaning is configuration, and every step needs a reason.** A step without a
  `reason` is a config error. Removed rows move to `rejected_<t>`; values that could
  not be typed are counted per column with their original text; filled values get a
  `<column>_filled` flag. A step that cannot run stops the run, so half-cleaned data
  never reaches the marts.
- **Lineage.** `_row` in every staging table (and marts that select it) is the row
  number in the source file, and `cleaning.json` maps every staging column back to
  its raw column.
- **Profiling describes, it doesn't judge.** It reports facts (e.g. "48 of 800 months
  missing"); whether that is a failure is a validation rule (layer 3), because it
  depends on the dataset.
- **Every number has an address.** Each fact in `profile.json` is reachable by its
  path, e.g. `tables.raw_artprice__data.columns_profile.column_1.nonconforming`.
  The AI layer will only be allowed to quote numbers through these IDs.
- **Counting runs in SQL.** Type checks, null counts, quantiles and duplicate
  detection run inside DuckDB. Measured: a 1,000,000-row, 8-column CSV runs end to end
  in about 10 seconds. Excel files go through pandas and are slower.
- **Strict config.** The YAML is validated before anything runs. A misspelled
  option is an error, not a silently ignored setting.
- **Traceable runs.** Each run records a run ID, the config's hash and a SHA-256
  fingerprint of every input file, so you can tell when the data changed.

## Run it

```bash
pip install -e ".[dev]"
dqp run configs/copper.yaml            # add --strict in CI, -v for profile highlights
pytest
```

## Adding a dataset

```yaml
dataset: thesis_assets
sources:
  - name: thesis
    type: excel
    path: data/thesis/assets.xlsx
    sheets: all          # or a list of names, or per-sheet settings:
  - name: nareit
    type: excel
    path: data/thesis/nareit.xls
    sheets:
      - sheet: Index Data
        header_rows: [5, 6, 7]   # 3-row header -> "All REITs Total Return"
    na_values: ["-"]             # extra 'missing' markers ("…", "n/a" are built in)
  - name: fx
    type: csv
    path: data/thesis/fx.csv
    delimiter: ";"
cleaning:                                          # per table; every step needs a reason
  fx:
    - snake_case: true
    - drop_rows: {where: "date IS NULL", reason: "footer lines"}   # rows go to rejected_fx
    # also: rename, trim_whitespace, drop_duplicates, replace, drop_columns, fill_forward
marts:
  - name: fx_monthly
    sql: "SELECT date_trunc('month', date) AS month, avg(rate) AS rate FROM stg_fx GROUP BY 1"
validation:
  rules:
    - table: fx
      expr: "amount >= 0"                          # any SQL condition
    - table: fx
      unique: [merchant_id, date]
      severity: warn                               # fail (default) | warn | info
    - table: fx
      date_coverage: {column: date, start: "1998-01-01", no_gaps: true}
    - table: fx
      sql: "SELECT _row FROM {table} WHERE ..."    # returns the failing rows
    - table: fx
      stage: raw                                   # check the data as it arrived
      not_null: [date]
    # also: not_null, between, allowed, row_count
  generic:
    no_missing_periods: off                        # change or switch off a generic check
report:
  show_values: true              # false: quality results only, no data values (licensed data)
summary:
  provider: auto                 # auto | anthropic | openai | none
  model: null                    # anthropic default claude-sonnet-5-5; required for openai
  base_url: null                 # any OpenAI-compatible server (Azure, Ollama, ...)
  key_columns:                   # headline numbers; default: first 5 numeric columns of each mart
    fx: [rate]
profiling:                       # optional, these are the defaults
  type_threshold: 0.95           # share of values that must fit a type
  outlier_iqr_k: 3.0             # outlier outside Q1 - 3*IQR .. Q3 + 3*IQR
  max_examples: 3
```

## Datasets

| Dataset | Input type | Status |
|---|---|---|
| Copper and metals, monthly 1960 onwards (800 months) | CSV | loads, profiles, validates, cleans, summarises ✅ |
| Thesis multi-asset data, 1998–2025 (6 files: master sheet, Artprice, Nareit, World Bank, FRED/OECD) | Excel .xlsx/.xls + CSV | loads, profiles, validates, cleans, summarises ✅ (data kept private: licensed sources) |
| Public payments / transactions | Parquet or large CSV | ⏳ |
| CRM / sales pipeline | Google Sheet | ⏳ |

The copper file is the monthly series exported from
[copper-market-analysis](https://github.com/behindokht/copper-market-analysis).

## Known limits

Found by testing on purpose, kept here so nothing is overclaimed:

- **Sources:** CSV and Excel only for now. Google Sheets, Parquet, JSON and SQL are planned.
- **Frequency detection** uses the median gap between dates. A series with very large gaps
  (fewer rows than missing periods) is reported as `irregular` and its gaps are not listed.
- **Outliers** use the IQR rule on levels, so a price that trends upward is flagged as an
  outlier. They are reported as information, never as a failure.
- **CSV lines with more fields than the header** stop the load, with a hint. Lines with
  fewer fields (footers, notes) are loaded and flagged.
- **`replace` in cleaning** works on typed values, so it cannot target a value that was never
  readable (that is what `na_values` at load time is for).
- **`snake_case`** drops symbols, so `Crude oil, WTI ($/bbl)` becomes `crude_oil_wti_bbl`;
  the original name stays in the lineage in `cleaning.json`.
- **The live LLM APIs** have not been called from this repo yet (see the summary section).

