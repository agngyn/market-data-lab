# Market Data Quality Report

70 tickers · synth_a, synth_b, synth_c · 2023-01-01 → 2024-12-31 · mode: synthetic

| metric | value |
|---|---|
| bars checked | 104,392 |
| bar failure rate | 1.07% |
| bars with ≥1 failure | 1,117 |
| ticker-level issues | 24 |
| clean series | 206 |
| checks run (10 bar + 6 ticker) | 16 |
| cache hit rate | 100% |
| fetch time | 3s |

## Coverage by vendor

_Only tickers where at least one vendor is short are shown. Long bars = truncated history or a vendor that does not carry the name._

## Fetch failures (vendor returned nothing or payload rejected)

> NoData = delisted / renamed / not carried. SchemaErrors = pandera rejected physically impossible bars at ingest.

| source | ticker | error |
|---|---|---|
| synth_a | PLD | NoData: empty response |
| synth_a | FB | NoData: empty response |
| synth_a | TWTR | NoData: empty response |
| synth_a | ATVI | NoData: empty response |
| synth_b | PEP | SchemaErrors: pandera rejected payload: close outside [low, high], high < low |
| synth_b | FB | NoData: empty response |
| synth_b | TWTR | NoData: empty response |
| synth_b | ATVI | NoData: empty response |
| synth_c | FB | NoData: empty response |
| synth_c | TWTR | NoData: empty response |
| synth_c | ATVI | NoData: empty response |

## Vendor reconciliation — re-based split-adjusted close (fair comparison)

> Every vendor's close converted to the same split basis using one reference action table. Tolerance 1.0%. Residual disagreement here is data error or restatement timing.

| pair | n_days | n_both | coverage_gap_pct | median_abs_bps | p99_abs_bps | max_abs_pct | pct_beyond_tol | mean_signed_bps |
|---|---|---|---|---|---|---|---|---|
| synth_a|synth_b | 34,136 | 33,796 | 1.00 | 0.00 | 300.36 | 200.37 | 1.24 | 169.09 |
| synth_a|synth_c | 34,637 | 34,201 | 1.26 | 0.00 | 284.72 | 200.37 | 1.22 | 167.28 |
| synth_b|synth_c | 34,638 | 34,545 | 0.27 | 0.00 | 0.00 | 50.00 | 0.01 | 0.29 |

## Vendor price-difference distribution

_Healthy vendors pile up at 0 bp. A tail beyond the dashed tolerance is a data problem on one side._

## Largest disagreements

| date | ticker | pair | diff_pct |
|---|---|---|---|
| 2023-12-12 | WMT | synth_a|synth_b | 200.37 |
| 2023-12-12 | WMT | synth_a|synth_c | 200.37 |
| 2023-04-10 | WMT | synth_a|synth_b | 200.00 |
| 2023-04-13 | WMT | synth_a|synth_b | 200.00 |
| 2023-11-08 | WMT | synth_a|synth_b | 200.00 |
| 2023-12-27 | WMT | synth_a|synth_b | 200.00 |
| 2024-01-11 | WMT | synth_a|synth_b | 200.00 |
| 2024-01-16 | WMT | synth_a|synth_b | 200.00 |
| 2024-01-19 | WMT | synth_a|synth_b | 200.00 |
| 2024-02-14 | WMT | synth_a|synth_b | 200.00 |
| 2023-04-10 | WMT | synth_a|synth_c | 200.00 |
| 2023-04-13 | WMT | synth_a|synth_c | 200.00 |
| 2023-11-08 | WMT | synth_a|synth_c | 200.00 |
| 2023-12-27 | WMT | synth_a|synth_c | 200.00 |
| 2024-01-11 | WMT | synth_a|synth_c | 200.00 |
| 2024-01-16 | WMT | synth_a|synth_c | 200.00 |
| 2024-01-19 | WMT | synth_a|synth_c | 200.00 |
| 2024-02-14 | WMT | synth_a|synth_c | 200.00 |
| 2023-01-05 | WMT | synth_a|synth_b | 200.00 |
| 2023-01-09 | WMT | synth_a|synth_b | 200.00 |
| 2023-01-11 | WMT | synth_a|synth_b | 200.00 |
| 2023-01-18 | WMT | synth_a|synth_b | 200.00 |
| 2023-01-25 | WMT | synth_a|synth_b | 200.00 |
| 2023-01-26 | WMT | synth_a|synth_b | 200.00 |
| 2023-02-01 | WMT | synth_a|synth_b | 200.00 |

## Vendor reconciliation — each vendor's OWN adj_close (methodology comparison)

> Deliberately unfair: vendors define 'adjusted' differently (split-only vs split+dividend). A systematic mean offset with a fat distribution is the fingerprint of a methodology gap, not bad data.

| pair | n_days | n_both | coverage_gap_pct | median_abs_bps | p99_abs_bps | max_abs_pct | pct_beyond_tol | mean_signed_bps |
|---|---|---|---|---|---|---|---|---|
| synth_a|synth_b | 34,136 | 33,796 | 1.00 | 0.00 | 0.00 | 33.33 | 0.39 | -0.16 |
| synth_a|synth_c | 34,637 | 34,201 | 1.26 | 50.00 | 393.07 | 8.16 | 36.76 | -103.97 |
| synth_b|synth_c | 34,638 | 34,545 | 0.27 | 50.00 | 393.07 | 47.02 | 36.56 | -103.72 |

## Vendor adjusted-close difference distribution

_Compare with the previous chart: the extra spread is dividends, not errors._

## Methodology gap per vendor (vendor adj_close vs our total-return rebase)

> Median across tickers. ~0 bp = vendor adjusts for dividends the same way we do; a gap that grows with lookback = split-only adjustment.

| source | vendor_adj_basis | median_gap_bps | gap_at_start_bps | pct_days_beyond_tol |
|---|---|---|---|---|
| synth_a | total_return | 0.00 | 0.00 | 0.00 |
| synth_b | total_return | 0.00 | 0.00 | 0.00 |
| synth_c | split | 202.51 | 357.11 | 78.84 |

## Methodology drift — WMT

_Both series normalised to their last bar. Flat at 0 = same method. A staircase drifting away from 0 = one vendor drops dividends._

## Adjusted vs unadjusted — NVDA (synth_b) around the 10:1 split

_Source synth_b declares close_basis='raw'. Left: three bases on a log axis. Right: the raw series shows a −90% 'return' that never happened._

## Failures by check and vendor

_Bar-level checks count bars; ticker-level checks count (vendor, ticker) series._

## Failure summary

| check | source | failures | fail_rate_pct |
|---|---|---|---|
| vendor_disagreement | synth_a | 417 | 1.20 |
| missing_bar | synth_a | 345 | 1.00 |
| stale_print | synth_a | 207 | 0.60 |
| zero_volume | synth_b | 207 | 0.60 |
| missing_bar | synth_c | 70 | 0.20 |
| price_jump | synth_b | 4 | 0.01 |
| no_data | synth_a | 4 |  |
| ticker_change | synth_c | 3 |  |
| no_data | synth_c | 3 |  |
| no_data | synth_b | 3 |  |
| ticker_change | synth_b | 3 |  |
| ticker_change | synth_a | 3 |  |
| coverage_mismatch | synth_c | 2 |  |
| stale_series | synth_c | 2 |  |
| vendor_disagreement | synth_b | 2 | 0.01 |
| split_adjustment_mismatch | synth_a | 1 | 0.00 |
| ingest_rejected | synth_b | 1 |  |

## Failure heatmap — synth_a

_Vertical stripes = a bad day across many names (feed outage / holiday mis-handled). Horizontal streaks = one bad series._

## Failure heatmap — synth_b

_Vertical stripes = a bad day across many names (feed outage / holiday mis-handled). Horizontal streaks = one bad series._

## Failure heatmap — synth_c

_Vertical stripes = a bad day across many names (feed outage / holiday mis-handled). Horizontal streaks = one bad series._

## Ticker-level issues

> stale_series / late_start / no_data / ingest_rejected / ticker_change / coverage_mismatch

| source | ticker | check |
|---|---|---|
| synth_a | ATVI | no_data |
| synth_a | ATVI | ticker_change |
| synth_a | FB | no_data |
| synth_a | FB | ticker_change |
| synth_a | PLD | no_data |
| synth_a | TWTR | no_data |
| synth_a | TWTR | ticker_change |
| synth_b | ATVI | no_data |
| synth_b | ATVI | ticker_change |
| synth_b | FB | no_data |
| synth_b | FB | ticker_change |
| synth_b | PEP | ingest_rejected |
| synth_b | TWTR | no_data |
| synth_b | TWTR | ticker_change |
| synth_c | ATVI | no_data |
| synth_c | ATVI | ticker_change |
| synth_c | DUK | stale_series |
| synth_c | DUK | coverage_mismatch |
| synth_c | F | stale_series |
| synth_c | F | coverage_mismatch |
| synth_c | FB | no_data |
| synth_c | FB | ticker_change |
| synth_c | TWTR | no_data |
| synth_c | TWTR | ticker_change |

## Worst tickers

| ticker | failures |
|---|---|
| WMT | 299 |
| CAT | 18 |
| KO | 18 |
| DUK | 16 |
| COST | 15 |
| AMT | 15 |
| ABT | 15 |
| CSCO | 15 |
| ABBV | 15 |
| NEE | 15 |
| PG | 15 |
| ORCL | 15 |
| NKE | 15 |
| META | 15 |
| TMUS | 15 |
| COP | 15 |
| CMCSA | 15 |
| LLY | 15 |
| JPM | 15 |
| LRCX | 15 |

## Ticker → CIK resolution (SEC EDGAR)

> status=listed: symbol found in SEC's current map. changed/not_found: renamed, acquired or delisted — a universe built from today's symbols would never contain these (survivorship bias).

| ticker | status | new_ticker | effective | reason | edgar_live |
|---|---|---|---|---|---|
| AAPL | unknown_offline |  |  |  | False |
| MSFT | unknown_offline |  |  |  | False |
| GOOGL | unknown_offline |  |  |  | False |
| AMZN | unknown_offline |  |  |  | False |
| META | unknown_offline |  |  |  | False |
| NVDA | unknown_offline |  |  |  | False |
| AVGO | unknown_offline |  |  |  | False |
| ORCL | unknown_offline |  |  |  | False |
| CRM | unknown_offline |  |  |  | False |
| ADBE | unknown_offline |  |  |  | False |
| CSCO | unknown_offline |  |  |  | False |
| INTC | unknown_offline |  |  |  | False |
| AMD | unknown_offline |  |  |  | False |
| QCOM | unknown_offline |  |  |  | False |
| TXN | unknown_offline |  |  |  | False |
| NFLX | unknown_offline |  |  |  | False |
| DIS | unknown_offline |  |  |  | False |
| CMCSA | unknown_offline |  |  |  | False |
| TMUS | unknown_offline |  |  |  | False |
| VZ | unknown_offline |  |  |  | False |
| JPM | unknown_offline |  |  |  | False |
| BAC | unknown_offline |  |  |  | False |
| WFC | unknown_offline |  |  |  | False |
| GS | unknown_offline |  |  |  | False |
| MS | unknown_offline |  |  |  | False |
| BLK | unknown_offline |  |  |  | False |
| V | unknown_offline |  |  |  | False |
| MA | unknown_offline |  |  |  | False |
| AXP | unknown_offline |  |  |  | False |
| BRK-B | unknown_offline |  |  |  | False |
| JNJ | unknown_offline |  |  |  | False |
| UNH | unknown_offline |  |  |  | False |
| PFE | unknown_offline |  |  |  | False |
| MRK | unknown_offline |  |  |  | False |
| ABBV | unknown_offline |  |  |  | False |
| LLY | unknown_offline |  |  |  | False |
| TMO | unknown_offline |  |  |  | False |
| ABT | unknown_offline |  |  |  | False |
| AMGN | unknown_offline |  |  |  | False |
| PG | unknown_offline |  |  |  | False |

_showing 40 of 73 rows_

## Detection scorecard vs injected defects (synthetic mode)

> Every defect was injected on purpose and recorded; recall = share detected, precision = share of flags that were injected defects.

| defect | check | injected | detected | recall | flagged_total | precision_vs_injected |
|---|---|---|---|---|---|---|
| ohlc_violation | ingest_rejected | 1 | 1 | 1 | 1 | 1 |

## Fetch telemetry

> Cache: {'hits': 218, 'misses': 1, 'writes': 0, 'hit_rate': 0.9954, 'mb_written': 0.0}

| vendor | requests | cache_hits | errors | no_data | seconds | rate_limit_sleep_s |
|---|---|---|---|---|---|---|
| synth_a | 0 | 73 | 0 | 0.00 | 0.00 | 0.00 |
| synth_b | 1 | 72 | 1 | 0.00 | 0.09 | 0.00 |
| synth_c | 0 | 73 | 0 | 0.00 | 0.00 | 0.00 |

## Check timings (seconds)

| check | seconds |
|---|---|
| missing_bar | 2.44 |
| zero_volume | 0.02 |
| price_jump | 0.08 |
| split_adjustment_mismatch | 0.07 |
| stale_print | 0.64 |
| ohlc_integrity | 0.02 |
| non_positive_price | 0.01 |
| vendor_disagreement | 0.22 |
| volume_outlier | 0.33 |
| duplicate_bar | 0.02 |
| stale_series | 0.02 |
| late_start | 0.02 |
| no_data | 0.00 |
| ingest_rejected | 0.00 |
| ticker_change | 0.00 |
| coverage_mismatch | 0.01 |

## Documented discrepancies

# Documented discrepancies

Real findings from running the lab against live Yahoo Finance data (universe of
71 tickers, 2022-01-03 → 2025-08-29, pulled 2026-09-25) and against the
synthetic vendor trio. Each entry: what we saw, why it happens, and what a
backtest would have done with it.

---

## 1. Yahoo's "unadjusted" close is already split-adjusted (and so is its volume)

**Observed.** `yf.download(..., auto_adjust=False)` returns a `Close` column
that is *not* the price that traded. NVDA on 2024-06-07 (the last session
before its 10-for-1 split) closed at **$1,208.88**; Yahoo's `Close` says
**$120.888** and `Volume` says 412 M shares (the tape printed ~41 M).

```
            Close   Adj Close     Volume  Stock Splits
2024-06-07  120.888  120.544  412386000           0.0
2024-06-10  121.790  121.444  313434100          10.0
```

**Cause.** Yahoo rewrites price *and volume* history on every split. Only
`Adj Close` is documented as adjusted, so users assume `Close` is raw. Polygon
(`adjusted=false`) and FMP (`close`) are truly raw.

**Consequence.** Mixing Yahoo `Close` with a raw vendor gives a 10× disagreement
on every pre-split bar; using Yahoo `Close` with a *separately sourced* split
table double-adjusts. The pipeline records `close_basis="split_adjusted"` on the
Yahoo adapter and re-bases every vendor onto one convention before comparing.
In the synthetic run the same bug is injected deliberately (vendor claims
split-adjusted, ships raw) and the `split_adjustment_mismatch` check catches it
on the split date with 100 % recall.

---

## 2. "Adjusted" means different things: Polygon is split-only, Yahoo/FMP are total-return

**Observed (synthetic, mirroring vendor documentation).** A vendor whose
`adj_close` adjusts only for splits drifts away from a total-return series by
exactly the cumulative dividend yield — a staircase with one step per ex-date,
~50 bp per step for a 2 %-yield name, ~400 bp at a 2-year lookback.

**Cause.** Polygon's aggregates endpoint documents `adjusted` as "adjusted for
splits". Yahoo and FMP apply CRSP-style multiplicative dividend factors
(`1 − D / P_{t−1}`) on top.

**Consequence.** A momentum or mean-reversion backtest that pulls half its
universe from a split-only source and half from a total-return source
systematically under-ranks the dividend payers. The `methodology_gap` table
fingerprints this per vendor: ~0 bp median gap = same convention as ours.
Our own total-return rebase reproduces Yahoo's `Adj Close` to **0.0 bp median
gap** on KO, WMT, NVDA and AAPL once dividends are put on the right share basis
(see #3).

---

## 3. Yahoo restates historical dividends in post-split shares

**Observed.** Walmart paid $0.57 per share quarterly before its 3-for-1 split
(Feb 2024). Yahoo's dividend history shows **$0.19**. NVDA's 2024-03-05
dividend shows as **$0.004** (declared: $0.04).

**Cause.** Yahoo divides every historical dividend by the cumulative split
factor so that `dividend / Close` stays a sensible yield on its split-adjusted
price series.

**Consequence.** Feeding Yahoo's dividend table into an adjustment engine that
expects declared amounts (per then-outstanding share) understates every
pre-split dividend factor by the split ratio. The first version of this
pipeline had exactly that bug: WMT's total-return series disagreed with Yahoo's
by ~0.5 %/yr before Feb 2024. Fix: each adapter declares `actions_basis`, and
`fetch_panel` normalises dividends to raw share terms before storing the
reference action table.

---

## 4. Adjusted prices are not point-in-time

**Observed.** The "adjusted close" of KO on **2024-06-07** depends on *when you
download it*:

| anchored at | adjusted close for 2024-06-07 |
|---|---|
| 2024-12-31 (our rebase, window ending Dec 2024) | 62.51 |
| 2025-08-29 (our rebase, window ending Aug 2025) | 61.62 |
| 2026-09-25 (Yahoo `Adj Close`, downloaded today) | 59.56 |

The raw close (63.91) never changes.

**Cause.** Adjustment factors compound *backwards* from the latest bar, so every
new ex-dividend date rewrites all history below it. Over three years the
Coca-Cola series drifted ~7 % — roughly its dividend yield × time.

**Consequence.** Any feature computed on adjusted prices (moving averages,
price levels, "% below 52-week high") is subtly look-ahead: it embeds
dividends that had not been declared yet. Returns are fine (factors cancel);
levels are not. Store raw + actions and adjust at query time with an explicit
`as_of` — never persist an adjusted series as if it were data.

---

## 5. A ticker is not an identity: `FB` now belongs to an ETF

**Observed.** Requesting `FB` for 2022-01-01 → 2025-08-29 from Yahoo returns
**46 bars starting 2025-06-26**, closes around $40, volume ~10 k shares/day.
Meta traded as FB until 2022-06-08 at ~$190 on 20 M+ shares. The symbol was
re-issued in June 2025 to *ProShares S&P 500 Dynamic Daily Buffer ETF*.

**Cause.** Exchanges recycle symbols. Yahoo keys history by the *current*
holder of the symbol, so Meta's FB-era history is only reachable under `META`,
and a fresh `FB` request returns the ETF.

**Consequence.** A backtest keyed on tickers would (a) drop Meta's
2012-2022 history from a universe that contained "FB", and (b) in a
point-in-time universe, splice an S&P buffer ETF onto the end of Meta. The
`late_start` check flags the 3½-year gap before the first bar; `ticker_change`
(SEC EDGAR ticker → CIK map, curated table offline) explains it. `TWTR` and
`ATVI` return nothing at all — the survivorship-bias case: a universe built
from *today's* constituents never contained them.

---

## 6. `price_jump` and `stale_print` need a second vendor to be useful

**Observed.** On the 71-ticker Yahoo run, `price_jump` (|return| > 20 %) fired
**35 times** — every one a real event (META −26 % Feb-2022, NFLX −35 % Apr-2022,
NVDA +24 % May-2023, INTC −26 % Aug-2024, UNH −22 % Apr-2025, TSLA/AMD +20 %
Apr-9-2025). `stale_print` fired on AMZN 2023-09-26/27/28: three identical
closes of **$125.98** on 50–73 M shares each day — also real.

**Cause.** Single-vendor checks can only detect *implausibility*, not
*incorrectness*. A −26 % earnings gap and a −26 % bad print look identical
from inside one feed.

**Consequence.** These checks are triage screens, not error detectors; their
precision on real data is low by construction. The `vendor_disagreement` check
is what separates a bad print (one vendor deviates) from a real move (all
vendors agree). That is the whole argument for pulling the same universe from
three sources: the cross-section is the error model.

---

## 7. pandera rejects physically impossible bars at ingest — by design

**Observed (synthetic).** A vendor payload with one bar where `high < low`
is refused at `get_ohlcv` with a `SchemaErrors` listing both failed
dataframe checks. The ticker lands in `panel.failures` as `ingest_rejected`
rather than partially loading.

**Why reject rather than repair.** A bar that violates `low ≤ open, close ≤
high` cannot be *fixed* from the bar itself — you do not know which field is
wrong. Loading 500 good bars plus 1 impossible one means downstream code has
to defend against it forever. Failing loudly at the boundary and re-fetching
(or falling back to another vendor) is cheaper.

