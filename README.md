# market-data-lab

**Multi-source market data pipeline & data quality lab.** Pulls the same 70-ticker universe from Yahoo Finance, Financial Modeling Prep and Polygon (or from three deterministic synthetic vendors with injected faults), caches every response as Parquet, rebuilds split- and dividend-adjusted series from raw prices + corporate actions, reconciles the vendors against each other, runs 16 assertion-style quality checks, and emits an HTML/Markdown data quality report.

```
python -m mdlab.pipeline --mode synthetic          # zero setup, ~30 s, full report
python -m mdlab.pipeline --mode live --vendors yahoo   # real data, no API key needed
```

Sample output: [`reports/sample/synthetic/data_quality_report.md`](reports/sample/synthetic/data_quality_report.md) · [`reports/sample/yahoo/data_quality_report.md`](reports/sample/yahoo/data_quality_report.md) · real findings in [`docs/discrepancies.md`](docs/discrepancies.md).

| | |
|---|---|
| Tests | `pytest` — 30 tests, incl. 100 % recall / 100 % precision on 8 injected defect types |
| Runtime | 70 tickers × 3 synthetic vendors × 2 years (104 k bars): fetch 10 s, 16 checks 7 s, report 10 s |
| Adjustment engine | reproduces Yahoo `Adj Close` to **0.0 bp** median gap on KO, WMT, NVDA, AAPL |
| Stack | pandas · pyarrow · pandera · tenacity · yfinance · requests · matplotlib |

---

## 1. Project overview

Every backtest is a function of its input data, and daily OHLCV data from retail-accessible vendors is quietly inconsistent: one vendor's "unadjusted" close is split-adjusted, another's "adjusted" close ignores dividends, symbols get recycled to different companies, feeds freeze and carry the last print forward, and adjusted history is rewritten every time a dividend goes ex. None of this throws an exception. It shows up months later as a strategy that "worked" in research and doesn't in production.

This project builds the piece of infrastructure that sits between the vendor APIs and any research code: a **vendor-agnostic ingestion layer** (one `DataSource` contract, retry/backoff, rate limiting, Parquet cache), a **corporate-action engine** that puts every vendor on the same price basis, a **reconciliation layer** that uses cross-vendor agreement as the error model, and a **check registry** where each data quality rule is a pure function returning a boolean Series — composable, testable, and scored against ground truth.

The synthetic mode is not a toy: three fake vendors mirror the real vendors' conventions (Yahoo-like split-adjusted closes, Polygon-like split-only adjustments, FMP-like raw closes) and each injects *recorded* defects, so the checks are measured by recall and precision instead of eyeballed.

## 2. Real-world finance use case

**Who does this job.** On the buy side, "data engineering" is a team, not a script — quant funds run dedicated market-data groups whose entire remit is vendor onboarding, reconciliation, corporate-action processing and point-in-time correctness. Bloomberg, Refinitiv, FactSet and the exchanges themselves disagree on the same daily bar more often than people expect; a fund with three vendors uses the disagreement as a signal of which one is wrong.

**What goes wrong without it.**

- **Survivorship bias.** A universe built from today's S&P 500 constituents never held Enron, Lehman, TWTR, ATVI. Published estimates put the CAGR inflation at 1–4 %/yr for long-only equity strategies; for small-cap or "value" screens it is larger because the names that disappeared are exactly the ones the screen would have bought.
- **Split mishandling.** An unadjusted 10:1 split is a −90 % daily return. A momentum strategy shorts it; a mean-reversion strategy buys it with 10× leverage. Either way the backtest P&L is fiction.
- **Dividend mishandling.** Price-return vs total-return is ~1.5–2 %/yr for the S&P 500, 3–5 %/yr for utilities/REITs/tobacco. A 20-year backtest that drops dividends understates terminal wealth by 30–100 % and systematically under-ranks high-yield names.
- **Look-ahead in adjusted levels.** Adjusted prices embed *future* dividends. Any feature based on price *level* (distance from 52-week high, moving-average crossovers) computed on an adjusted series is peeking.
- **Stale and zero-volume prints.** A frozen feed produces a perfectly flat, zero-volatility series — the best-looking Sharpe ratio you will ever see.

**What this lab delivers.** A cached, reconciled, basis-normalised panel plus a report that says, per vendor and per ticker, exactly what to distrust — before a single signal is computed.

## 3. System architecture

```
                 ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐
   vendors       │ YahooSource  │  │  FMPSource   │  │PolygonSource │  │ SyntheticSource ×3 │
                 │close: split- │  │close: raw    │  │close: raw    │  │ (fault injection)  │
                 │adjusted      │  │adj: total-ret│  │adj: split    │  │                    │
                 └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └─────────┬──────────┘
                        └─────────────────┴─────────┬───────┴───────────────────┘
                                                    ▼
   sources/base.py         DataSource.get_ohlcv():  rate limit → tenacity retry → standardize()
                                                    → pandera validate → ParquetCache (source,ticker,start,end)
                                                    ▼
   panel.py                fetch_panel()  → long panel (source, ticker, date, o/h/l/c/adj/vol/div/split)
                           actions table  → one reference (dividends, splits) per ticker, RAW share basis
                           add_rebased_closes() → close_split, close_tr, volume_split  (adjust.py)
                                                    ▼
                    ┌───────────────────────────────┼──────────────────────────────┐
                    ▼                               ▼                              ▼
   reconcile.py                          quality.py                          edgar.py
   pairwise vendor % diffs               10 bar-level + 6 ticker-level       ticker → CIK map,
   (fair: close_split;                   checks, each -> bool Series          former names,
    unfair: vendor adj_close)            (True = FAIL); scoring vs            renames/delistings
   methodology_gap()                     injected defects
                    └───────────────────────────────┼──────────────────────────────┘
                                                    ▼
   viz.py + report.py       heatmaps · diff distributions · adjustment overlay · HTML (base64 figs) + Markdown
```

Design decisions worth calling out:

- **Long panel, not MultiIndex columns, for storage.** pyarrow cannot write MultiIndex *columns*; a long `(source, ticker, date)` frame round-trips through Parquet losslessly and pivots to `date × (source, ticker)` on demand (`Panel.wide(field)`).
- **Raw + actions is the source of truth.** Adjusted series are derived at query time. This is what makes point-in-time queries possible later (section 15).
- **Every vendor declares its conventions** (`close_basis`, `adj_close_basis`, `actions_basis`) as class attributes. The rebase logic reads them; nothing downstream special-cases a vendor name.
- **Errors are data.** A vendor failure is a row in `panel.failures` and a ticker-level check failure — not an exception that kills a 210-series batch.
- **Checks are pure functions** registered by decorator. Adding a check is one function; the runner, report, heatmaps and scoring pick it up automatically.

## 4. Required APIs and data sources

| Source | Auth | Free-tier limits | What we use | Quirk the pipeline handles |
|---|---|---|---|---|
| **Yahoo Finance** (`yfinance`) | none | unofficial; self-throttled to 2 req/s | daily OHLCV, `Adj Close`, dividends, splits | `Close` and `Volume` are split-adjusted; dividends restated in post-split shares; delisted symbols return empty |
| **Financial Modeling Prep** | `FMP_API_KEY` | 250 req/day | `/stable/historical-price-eod/full`, `/dividend-adjusted`, `/dividends`, `/splits` | raw close; adjusted = split + dividend; errors arrive as HTTP 200 with an `Error Message` body |
| **Polygon.io** | `POLYGON_API_KEY` | 5 req/min, 2 yr history | `/v2/aggs/ticker/{T}/range/1/day`, `/v3/reference/splits`, `/v3/reference/dividends` | `adjusted=true` is **split-only**; timestamps are UTC ms that must be converted to New York date |
| **SEC EDGAR** | descriptive `User-Agent` with email (mandatory) | 10 req/s | `company_tickers.json` (ticker → CIK), `submissions/CIK##.json` (former names, all tickers) | 403 without User-Agent; ticker `BRK-B` (Yahoo) is `BRK.B` (SEC) |

Get keys at [financialmodelingprep.com](https://site.financialmodelingprep.com/developer/docs) and [polygon.io](https://polygon.io/). Set them as environment variables, in a `.env` file (see `.env.example`), or in Colab's Secrets panel — `Settings` checks all three.

## 5. Required Python libraries

```
pandas>=2.1      panel manipulation, MultiIndex pivots, groupby-transform checks
numpy>=1.24      vectorised math, GBM paths for synthetic vendors
pyarrow>=12      Parquet cache (zstd), lossless dtype/index round-trip
yfinance         Yahoo adapter
requests         FMP / Polygon / EDGAR HTTP, one Session per vendor
tenacity         retry with exponential backoff + jitter, retry only on transient errors
pandera          declarative bar schema: dtypes, ranges, high>=low, unique DatetimeIndex
matplotlib       all figures (headless Agg backend)
pytest           30 tests
```

`pip install -r requirements.txt` or `pip install -e .` (exposes the `mdlab` CLI).

## 6. Folder / file structure

```
market-data-lab/
├── README.md                     this document
├── pyproject.toml                package metadata, `mdlab` CLI entry point
├── requirements.txt
├── .env.example                  key template (never commit .env)
├── mdlab/
│   ├── config.py                 Settings (keys, paths, thresholds), Colab secret support
│   ├── calendar.py               NYSE trading calendar (holidays, observance rules, ad-hoc closures)
│   ├── schema.py                 canonical bar schema + pandera validation
│   ├── cache.py                  ParquetCache keyed on (source, ticker, start, end), TTL, atomic writes
│   ├── sources/
│   │   ├── base.py               DataSource ABC: rate limiter, tenacity retry, cache, telemetry, errors
│   │   ├── yahoo.py              YahooSource
│   │   ├── fmp.py                FMPSource (stable + legacy payloads)
│   │   ├── polygon.py            PolygonSource (pagination, tz handling)
│   │   └── synthetic.py          TrueMarket + SyntheticSource + DefectProfile (fault injection)
│   ├── adjust.py                 split / dividend factors, rebase to raw | split | total_return
│   ├── universe.py               70-ticker universe, reference splits, known ticker changes
│   ├── panel.py                  fetch_panel, Panel (long <-> wide), reference action table
│   ├── reconcile.py              pairwise vendor diffs, methodology_gap
│   ├── quality.py                check registry, run_checks, QualityResult, scoring vs injected defects
│   ├── edgar.py                  EdgarClient: ticker -> CIK, former names, rename/delist resolution
│   ├── viz.py                    six figure types, fixed vendor palette
│   ├── report.py                 HTML (embedded PNG) + Markdown report
│   └── pipeline.py               run_pipeline() orchestration + CLI
├── notebooks/
│   └── market_data_lab.ipynb     Colab walkthrough, one section per cell
├── tests/
│   ├── test_adjust.py            corporate-action math
│   ├── test_quality.py           recall/precision vs injected defects, split-date exclusions
│   └── test_sources_and_cache.py calendar, schema, cache, retry semantics, vendor payload parsing
├── docs/
│   └── discrepancies.md          seven documented real-world discrepancies with numbers
├── reports/sample/               committed example output (synthetic + Yahoo)
└── cache/                        Parquet cache (git-ignored)
```

In Colab the same layout is used: the notebook clones the repo (or `%%writefile`s it) and imports `mdlab` — nothing is defined inline that would drift from the tested package.

## 7. Step-by-step build guide

The order below is the order the code was actually built and tested in; each step is runnable on its own.

1. **Canonical schema first** (`schema.py`). Decide what a bar *is* before touching a vendor: `date` index (tz-naive, normalised, unique), `open/high/low/close/adj_close/volume/dividends/splits`. Write the pandera schema. Everything else is a transformation into this shape.
2. **Trading calendar** (`calendar.py`). "Missing bar" is meaningless without an expected-session list. Encode NYSE rules with `pandas.tseries.holiday`; add Juneteenth (2022+) and ad-hoc closures. Unit-test Good Friday and July 4th observance.
3. **Cache** (`cache.py`). Parquet + zstd, key = `(source, ticker, start, end)` — the window is part of the key because vendors restate history. Atomic write (tmp + rename), JSON sidecar with `written_at` and `close_basis`, TTL, hit/miss stats.
4. **`DataSource` base class** (`sources/base.py`). Template method: `get_ohlcv` = cache lookup → rate-limit token → `_fetch_ohlcv` under tenacity retry → `standardize` → clip to window → pandera validate → cache put. Map HTTP status to `TransientError` (retry) vs `PermanentError` (don't) vs `NoDataError` (empty frame, not an exception).
5. **Synthetic vendors** (`sources/synthetic.py`) *before* the real ones. A shared `TrueMarket` (GBM over real NYSE sessions, real split dates, synthetic quarterly dividends) rendered onto each vendor's conventions, then corrupted per a `DefectProfile`. Every defect is appended to `injected`. This is what lets you test the checks with numbers.
6. **Yahoo adapter** (`sources/yahoo.py`). Flatten MultiIndex columns, treat empty frames as `NoDataError`, declare `close_basis="split_adjusted"` and `actions_basis="split_adjusted"` — both discovered by comparing NVDA's 2024-06-07 bar to the tape.
7. **FMP and Polygon adapters.** Written against the documented payload shapes and tested with fixture JSON (`tests/test_sources_and_cache.py`) so they are verifiable without keys. Polygon needs UTC-ms → New York date conversion and pagination via `next_url`; FMP returns errors as HTTP 200.
8. **Adjustment engine** (`adjust.py`). Split factors (`1/r` before the split date), dividend factors (`1 − D/P_{t−1}`, CRSP), compounding backwards. `adjust_bars(bars, splits, dividends, input_basis, target)` handles the four combinations of vendor convention × desired basis. Tested against hand-computed cases.
9. **Panel + reference action table** (`panel.py`). Stack frames long; pick one action table per ticker (from bars when the vendor carries events, else a call; fall back across vendors); **normalise dividends to raw share basis**; attach `close_split`, `close_tr`, `volume_split`.
10. **Reconciliation** (`reconcile.py`). Pivot wide, pairwise `a/b − 1`, summarise (median, p99, % beyond tolerance, signed mean). Run it twice: on `close_split` (fair) and on vendors' own `adj_close` (deliberately unfair — exposes methodology).
11. **Quality checks** (`quality.py`). One function per rule, `@bar_check` / `@ticker_check`, boolean Series out, `True = FAIL`. Runner reindexes to the full panel, times each check, never lets a broken check take down the report. `score_against_injected` turns synthetic mode into a unit test.
12. **EDGAR** (`edgar.py`). Cached ticker→CIK map, 10 req/s throttle, curated fallback table when offline.
13. **Figures and report** (`viz.py`, `report.py`). Fixed vendor→colour map, sequential ramp for heatmaps, base64-embedded PNGs so the HTML is one file.
14. **Pipeline + CLI** (`pipeline.py`). `run_pipeline()` returns every intermediate object; `python -m mdlab.pipeline` for scripts and CI.
15. **Document a real discrepancy** (`docs/discrepancies.md`). Run live, look at what fired, explain each with numbers.

## 8. Data collection pipeline

```python
from mdlab import Settings, run_pipeline

res = run_pipeline(
    tickers=None,            # default 70-ticker universe incl. split names + delisted probes
    start="2022-01-01", end="2025-08-29",
    mode="live", vendors=["yahoo", "fmp", "polygon"],   # vendors without keys are skipped with a warning
    settings=Settings.from_env(),
)
res.panel.long.head()        # long panel
res.panel.wide("close_tr")   # date × (source, ticker)
res.report_paths["html"]
```

What happens per `(source, ticker)`:

1. **Cache lookup** — hit returns immediately (`cache_hit_rate` is reported; a second run of the 70-ticker universe is 100 % hits, 0 requests).
2. **Rate limit** — token bucket per vendor (Polygon 5/min, FMP 250/min, Yahoo self-imposed 120/min). Sleep time is accounted in telemetry.
3. **Retry** — tenacity, up to 5 attempts, exponential backoff with jitter capped at 30 s, only on `TransientError` / connection errors / 429 / 5xx. 401/403 fail immediately with a message about the key.
4. **Standardise + validate** — rename, coerce, tz-strip, dedupe, sort; pandera rejects impossible bars (`high < low`, negative prices, duplicate dates) and the ticker is recorded as `ingest_rejected`.
5. **Cache write** — atomic, with metadata.

Fetching adjusted series from FMP/Polygon doubles their call count; `fetch_adjusted=False` halves quota use and lets `adjust.py` rebuild the adjusted series from raw + actions (which is what you want anyway).

## 9. Data cleaning & feature engineering

The "features" here are the derived price bases every downstream consumer needs, computed once from raw + actions:

| Column | Definition | Use |
|---|---|---|
| `close` | vendor's unadjusted close, on the vendor's declared basis | reconciliation input |
| `adj_close` | vendor's own adjusted close (basis declared per vendor) | methodology comparison |
| `close_split` | our rebase: raw × split factors | fair cross-vendor comparison, `price_jump`, `vendor_disagreement` |
| `close_tr` | our rebase: split × dividend factors (total return) | backtest returns |
| `volume_split` | volume in post-split shares | `volume_outlier` without split artefacts |

Cleaning rules applied at ingest: tz-aware → tz-naive New York date; duplicate dates keep the last; all-NaN holiday rows dropped; bars outside the requested window clipped (vendors often return one bar either side). Cleaning rules deliberately **not** applied: no forward-filling of missing bars, no winsorising of jumps, no repair of OHLC violations — those are *findings*, and a repaired series hides them.

Corporate-action handling in one paragraph: a split with ratio `r` on date `d` multiplies every price before `d` by `1/r` and every volume by `r`; a dividend `D` with ex-date `d` multiplies every price before `d` by `1 − D/P_{d−1}` (both on the same split basis); factors compound backwards so the latest bar always has factor 1. The engine reproduces Yahoo's `Adj Close` to 0.0 bp median once dividends are converted from Yahoo's post-split restatement to declared amounts.

## 10. Core models / algorithms

There is no ML here on purpose — the "model" is a set of explicit, falsifiable rules, each a pure function `Panel → bool Series (True = FAIL)`:

**Bar-level (indexed by `source, ticker, date`)**

| check | rule | catches |
|---|---|---|
| `missing_bar` | NYSE session inside the vendor's own span with no bar | dropped days, holiday mis-handling |
| `zero_volume` | `volume == 0` | halted names, feed gaps |
| `price_jump` | `|Δ close_split| > 20 %` on a non-split date | bad prints, unadjusted splits with wrong dates, real events (see discrepancy #6) |
| `split_adjustment_mismatch` | `|Δ close_split| > 20 %` **on** a reference split date | vendor's declared `close_basis` is wrong |
| `stale_print` | ≥ 3 consecutive identical closes | frozen feed / carried-forward last |
| `ohlc_integrity` | `high < low`, `close ∉ [low, high]`, `open ∉ [low, high]` | corrupted bars that slipped past ingest (e.g. loaded from an old cache) |
| `non_positive_price` | `close ≤ 0` or NaN | placeholder rows |
| `vendor_disagreement` | `|close_split / cross-vendor median − 1| > 1 %` with ≥ 2 vendors | the one vendor that is wrong |
| `volume_outlier` | `volume_split > 25 × trailing-60-session median` | unit errors, un-split-adjusted volume, real events |
| `duplicate_bar` | same `(source, ticker, date)` twice | concat bugs, overlapping windows |

**Ticker-level (indexed by `source, ticker`)**

| check | rule | catches |
|---|---|---|
| `stale_series` | last bar > 5 sessions before requested end | dead feed, delisting |
| `late_start` | first bar > 20 sessions after requested start | IPO, vendor gap, **reused ticker** |
| `no_data` | vendor returned nothing | delisted / renamed / not carried |
| `ingest_rejected` | pandera refused the payload | impossible bars |
| `ticker_change` | symbol absent from SEC's current ticker→CIK map (or in the curated rename table) | renames, acquisitions, delistings |
| `coverage_mismatch` | vendor has > 2 % fewer bars than the best vendor for the ticker | truncated history |

**Reconciliation statistics** per vendor pair and per ticker: `n_both`, coverage gap %, median |Δ| bp, p99 |Δ| bp, max |Δ| %, % of bars beyond tolerance, signed mean bp. **Methodology gap**: `(adj_close/adj_close_last) / (close_tr/close_tr_last) − 1`, whose *shape* (flat vs staircase) identifies split-only vendors.

**Detection scoring** (synthetic mode): each injected defect kind maps to the check that should catch it; recall and precision are computed on `(source, ticker, date)` keys, with the two legitimate ambiguities handled explicitly (a price spike also produces a reversion-day jump; defects inside a rejected payload are unobservable).

## 11. Visualizations & dashboard components

All figures use one fixed vendor→colour mapping (a vendor never changes colour between charts), a single-hue sequential ramp for magnitude, neutral ink for text, hairline grids, and a legend whenever ≥ 2 series are drawn.

| Figure | What it shows | How to read it |
|---|---|---|
| **Failure heatmap** (tickers × dates, one per vendor) | every bar failing any check; rows sorted worst-first; row totals on the right | vertical stripe = bad day across names (feed outage); horizontal streak = one bad series; a solid block ending on a split date = basis mismatch |
| **Vendor price-difference distribution** (per pair, log-y) | histogram of `a/b − 1` in bp, tolerance lines, tail count folded into edge bins | a spike at 0 is health; a second mode is methodology; a smear is noise |
| **Adjusted vs unadjusted overlay** (split date) | raw / split-adjusted / total-return on a log axis + daily-return panel | the −90 % "return" that never happened |
| **Methodology drift** | vendor `adj_close` vs our total-return rebase, normalised to the last bar | flat = same method; staircase = split-only vendor |
| **Failures by check × vendor** (stacked bars, symlog) | where the problems concentrate | |
| **Coverage shortfall** | sessions each vendor is missing vs the best vendor, worst tickers | |

The HTML report opens with a KPI tile row (bars checked, failure rate, ticker-level issues, clean series, cache hit rate, fetch time), then coverage → reconciliation → corporate actions → quality → EDGAR → telemetry → documented discrepancies. It is a single file with embedded PNGs, so it can be attached to a PR or emailed. The Markdown twin renders on GitHub.

## 12. Performance metrics

**Data quality metrics (reported per run)**

- bar failure rate (% of bars failing ≥ 1 check) and per-check failure counts by vendor
- ticker-level issue count, clean-series count
- per vendor pair: coverage gap %, median / p99 |Δ| bp, % beyond tolerance, signed mean bp
- methodology gap: median bp, gap at start of window, % of days beyond tolerance
- synthetic mode: **recall and precision per check vs injected defects** — currently 8/8 defect kinds at 1.00 / 1.00

**Pipeline metrics (reported per run)**

- requests, cache hits, no-data, errors, wall time and rate-limit sleep **per vendor**
- cache hit rate, MB written
- per-check execution time

**Reference numbers** (this machine, pandas 3.0): synthetic 70 tickers × 3 vendors × 2 years = 104,392 bars — fetch/generate 10 s, rebase 2 s, 16 checks 7 s (slowest: `missing_bar` 1.8 s, `stale_print` 0.5 s), report with 9 figures 10 s. Live Yahoo, 71 tickers × 3.7 years: 64,306 bars in 41 s cold, 0 requests warm. Parquet cache: ~40 KB per ticker-window, 0.9 MB for the whole Yahoo universe.

**Live Yahoo findings** (71 tickers, 2022-01 → 2025-08): 0.064 % bar failure rate; 35 `price_jump` flags (all real earnings/macro moves), 6 `stale_print` bars (AMZN's real three identical closes, CSCO, F), 2 `no_data` (TWTR, ATVI), 3 `ticker_change`, and 1 `late_start` — the `FB` symbol now resolving to a ProShares ETF listed June 2025. Details with numbers in `docs/discrepancies.md`.

## 13. Final deliverable

- `mdlab` — an installable Python package with a CLI, 30 passing tests, and a documented vendor contract that makes adding a fourth vendor a ~60-line file.
- `notebooks/market_data_lab.ipynb` — a Colab notebook that walks the pipeline stage by stage (one section per cell), runs offline by default and switches to live vendors when keys are present.
- `reports/sample/` — committed HTML + Markdown reports for both modes, with figures.
- `docs/discrepancies.md` — seven real discrepancies (Yahoo's split-adjusted "raw" close, split-only vs total-return adjustment, dividend share-basis restatement, non-point-in-time adjusted prices with a 3-year drift table, the recycled `FB` ticker, the low precision of single-vendor jump/stale checks, ingest-time rejection) each with cause and backtest consequence.

## 14. Resume description

> **Multi-Source Market Data Pipeline & Data Quality Lab** — Python, pandas, pyarrow, pandera, tenacity, matplotlib
> Built a vendor-agnostic ingestion layer (Yahoo Finance, FMP, Polygon) with rate limiting, exponential-backoff retry and a Parquet cache; implemented a corporate-action engine that reproduces Yahoo's total-return adjusted close to 0.0 bp and normalises three vendors' conflicting price conventions onto one basis; designed a registry of 16 assertion-based data quality checks (missing bars, stale prints, >20 % jumps, split-adjustment mismatches, cross-vendor disagreement, reused tickers via SEC EDGAR) validated at 100 % recall / 100 % precision against fault-injected synthetic vendors; shipped an automated HTML/Markdown data quality report with failure heatmaps and vendor-difference distributions, and documented seven real vendor discrepancies including a recycled ticker symbol and a 7 % point-in-time drift in adjusted prices.

Shorter, for a one-line bullet: *Built a three-vendor equity data pipeline with Parquet caching, corporate-action adjustment and 16 assertion-based quality checks scored at 100 % recall on injected faults; documented seven real vendor discrepancies (Python/pandas/pandera).*

## 15. Potential upgrades

Ordered by value-per-hour, in my judgement.

1. **Point-in-time query API.** `panel.as_of(date)` that rebuilds adjusted series using only actions known on that date — the raw + actions storage already makes this possible; it is the single most valuable feature for a research platform.
2. **Fourth and fifth vendors** (Tiingo, Alpha Vantage, EODHD, Nasdaq Data Link). More vendors make the cross-sectional median a better error model; the base class makes each one a short file.
3. **Universe history from EDGAR + index constituents.** Replace the static ticker list with CIK-keyed entities and a dated constituent table — the actual cure for survivorship bias, not just its detection.
4. **Incremental refresh.** Cache keyed on `(source, ticker, start, end)` is safe but re-downloads the whole window; add a tail-merge mode that fetches only the last N sessions and appends, with restatement detection (compare overlap to the cached copy and flag changed bars — vendors silently revise volume for weeks).
5. **Intraday bars.** Polygon minute aggregates; the calendar module needs session open/close times and half-days; `missing_bar` becomes `missing_minute`.
6. **DuckDB over the Parquet cache** for ad-hoc SQL across all vendors/tickers without loading into pandas.
7. **Great-Expectations-style expectation suites** with per-ticker thresholds learned from history (a 20 % move is normal for SMCI and alarming for KO).
8. **Alerting.** Run nightly via cron/GitHub Actions; diff the failure table against yesterday's; post new failures to Slack.
9. **Corporate-action reconciliation across vendors** — the same pairwise-diff treatment for dividend and split tables, which disagree more often than prices do (ex-date vs pay-date, special dividends, spin-offs treated as dividends).
10. **Spin-offs and mergers.** Currently unmodelled; a spin-off looks like a large special dividend and a merger looks like a delisting. Needs an event table keyed by CIK.

---

## Quant concepts this project exercises

*Organised as chunks — what problem each solves, what it groups with, and where it differs from its neighbours.*

**Chunk 1 — Price bases (what number are we even looking at?)**
Raw, split-adjusted and total-return closes are three views of one security. They belong together because they differ only by *multiplicative factors that compound backwards from today*. Raw is what traded; split-adjusted removes share-count changes (no wealth effect, so returns must be continuous); total-return additionally reinvests cash dividends (a wealth effect, so price return ≠ total return). The key relationship: `factor_t = Π over events after t`. The key distinction: split factors are exact (`1/r`), dividend factors depend on the prior close (`1 − D/P_{t−1}`), so they carry price-level dependence.

**Chunk 2 — Biases that come from the universe, not the prices.**
Survivorship bias (today's constituents never contained the losers), look-ahead bias (adjusted levels embed future dividends; today's ticker map hides yesterday's), and point-in-time data (the version of the dataset that existed on the decision date) are one chunk: all three are *time-of-knowledge* problems. They differ in what leaks — the universe, the price level, or the identity — and are cured by the same principle: store raw observations with the date they became known and reconstruct at query time.

**Chunk 3 — Data quality as measurement error.**
Missing bars, stale prints, zero volume, bad prints and basis mismatches are all deviations between the vendor's series and the true series. Single-vendor checks can only test *plausibility*; a cross-section of vendors turns this into an *estimation* problem (the median is the estimate, disagreement is the residual). This is why "three vendors" is a design requirement, not an extravagance.

*Active-recall questions:* If a vendor's adjusted close for a date in 2020 changes between two downloads a year apart, which chunk is that and what caused it? A vendor's `adj_close` drifts from your total-return rebase in steps of ~50 bp — split-only or total-return vendor? Why is a −26 % `price_jump` flag on META in Feb 2022 not a false positive of the *check*, yet not an error in the *data*?

---

## License

MIT.
