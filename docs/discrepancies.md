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
