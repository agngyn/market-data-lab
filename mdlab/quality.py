"""
Assertion-style data quality checks.

Design
------
Every check is a plain function registered with ``@bar_check`` or
``@ticker_check``. It receives the :class:`~mdlab.panel.Panel` (+ settings)
and returns a **boolean Series where ``True`` means FAIL**:

* bar-level checks   -> indexed by ``(source, ticker, date)``
* ticker-level checks-> indexed by ``(source, ticker)``

Keeping checks as boolean Series (rather than printing warnings) means they
compose: the report stacks them into a long ``failures`` table, pivots it into
heatmaps, and — in tests — compares them against the ground-truth defects the
synthetic vendors injected to measure recall and precision.

The checks (bar level)
----------------------
missing_bar                 expected NYSE session absent for this vendor while another vendor has it
zero_volume                 volume == 0 on a session (feed gap or halted name)
price_jump                  |1-day return| > threshold on the *re-based split-adjusted* close, not on a split date
split_adjustment_mismatch   the re-based close still jumps on a reference split date -> vendor's declared close basis is wrong
stale_print                 >= N consecutive identical closes (feed frozen / last-price carried forward)
ohlc_integrity              high < low, or close/open outside [low, high]
non_positive_price          close <= 0 or NaN where the bar exists
vendor_disagreement         |close_split / cross-vendor median - 1| > tolerance (>= 2 vendors)
volume_outlier              split-adjusted volume > 25x trailing-60d median (unit / adjustment error, or a real event)
duplicate_bar               same (source, ticker, date) appears twice

The checks (ticker level)
-------------------------
stale_series                last bar older than N business days before the requested end
late_start                  first bar > 4N sessions after the requested start (IPO, vendor gap, or a REUSED ticker)
no_data                     vendor returned nothing for the ticker (delisted / renamed / not carried)
ingest_rejected             pandera schema rejected the vendor payload (physically impossible bars)
ticker_change               symbol not in SEC's current ticker->CIK map (renamed or delisted), or known rename
coverage_mismatch           vendor has >2% fewer bars than the best vendor for this ticker
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .calendar import nyse_sessions
from .config import Settings
from .panel import Panel
from .universe import KNOWN_TICKER_CHANGES, reference_splits

log = logging.getLogger(__name__)

BAR_CHECKS: dict[str, Callable] = {}
TICKER_CHECKS: dict[str, Callable] = {}
BAR_KEY = ["source", "ticker", "date"]
TKR_KEY = ["source", "ticker"]


def bar_check(fn: Callable) -> Callable:
    BAR_CHECKS[fn.__name__] = fn
    return fn


def ticker_check(fn: Callable) -> Callable:
    TICKER_CHECKS[fn.__name__] = fn
    return fn


def _indexed(panel: Panel) -> pd.DataFrame:
    return panel.long.set_index(BAR_KEY).sort_index()


# ==========================================================================
# bar-level checks
# ==========================================================================
@bar_check
def missing_bar(panel: Panel, settings: Settings) -> pd.Series:
    """A session is 'expected' for (source, ticker) if it is an NYSE session
    between the ticker's first and last bar *across all vendors*. This avoids
    flagging pre-IPO dates while still catching a vendor that silently drops days."""
    long = panel.long
    out = []
    span = long.groupby("ticker")["date"].agg(["min", "max"])
    for (src, tkr), grp in long.groupby(TKR_KEY):
        # gaps *inside* this vendor's own series only; a series that ends early
        # is a different defect (stale_series / coverage_mismatch), not N missing bars
        lo = span.loc[tkr, "min"]
        hi = min(span.loc[tkr, "max"], grp["date"].max())
        expected = nyse_sessions(lo, hi)
        present = pd.DatetimeIndex(grp["date"])
        missing = expected.difference(present)
        out.append(pd.Series(True, index=pd.MultiIndex.from_product([[src], [tkr], missing], names=BAR_KEY)))
    if not out:
        return _empty_bar()
    return pd.concat(out).astype(bool)


@bar_check
def zero_volume(panel: Panel, settings: Settings) -> pd.Series:
    df = _indexed(panel)
    return (df["volume"].fillna(-1) == 0).rename("zero_volume")


@bar_check
def price_jump(panel: Panel, settings: Settings) -> pd.Series:
    """Large 1-day move on the re-based close, excluding reference split dates
    (those are handled by ``split_adjustment_mismatch``)."""
    col = "close_split" if "close_split" in panel.long.columns else "close"
    df = _indexed(panel)
    ret = df.groupby(level=[0, 1], sort=False)[col].pct_change(fill_method=None)
    flag = ret.abs() > settings.price_jump_threshold
    split_dates = _split_date_mask(df.index)
    return (flag & ~split_dates).fillna(False).astype(bool).rename("price_jump")


@bar_check
def split_adjustment_mismatch(panel: Panel, settings: Settings) -> pd.Series:
    """If our rebase (which trusts the vendor's declared ``close_basis``) still
    leaves a >20% jump exactly on a reference split date, the declaration is
    wrong — the classic 'vendor said adjusted, shipped raw' bug."""
    col = "close_split" if "close_split" in panel.long.columns else "close"
    df = _indexed(panel)
    ret = df.groupby(level=[0, 1], sort=False)[col].pct_change(fill_method=None)
    flag = (ret.abs() > settings.price_jump_threshold) & _split_date_mask(df.index)
    return flag.fillna(False).astype(bool).rename("split_adjustment_mismatch")


@bar_check
def stale_print(panel: Panel, settings: Settings) -> pd.Series:
    """>= ``stale_run_length`` consecutive bars with an identical close.
    Genuine unchanged closes happen (~0.5% of days for large caps) but runs of
    3+ are vanishingly rare for liquid names and usually mean a frozen feed."""
    df = _indexed(panel)
    n = settings.stale_run_length

    def _runs(s: pd.Series) -> pd.Series:
        same = s.eq(s.shift(1)) & s.notna()
        # run id increments whenever the 'same' streak breaks
        run_id = (~same).cumsum()
        run_len = same.groupby(run_id).cumsum()
        # a run of n identical closes == n-1 consecutive 'same' flags
        hit = run_len >= (n - 1)
        # back-fill so every bar in the run (incl. the first repeat) is flagged
        return hit.groupby(run_id).transform("max") & same

    flags = df.groupby(level=[0, 1], sort=False)["close"].transform(_runs)
    return flags.fillna(False).astype(bool).rename("stale_print")


@bar_check
def ohlc_integrity(panel: Panel, settings: Settings) -> pd.Series:
    df = _indexed(panel)
    tol = 1e-4
    bad = (
        (df["high"] < df["low"] * (1 - tol))
        | (df["close"] > df["high"] * (1 + tol)) | (df["close"] < df["low"] * (1 - tol))
        | (df["open"] > df["high"] * (1 + tol)) | (df["open"] < df["low"] * (1 - tol))
    )
    return bad.fillna(False).astype(bool).rename("ohlc_integrity")


@bar_check
def non_positive_price(panel: Panel, settings: Settings) -> pd.Series:
    df = _indexed(panel)
    return (df["close"].isna() | (df["close"] <= 0)).astype(bool).rename("non_positive_price")


@bar_check
def vendor_disagreement(panel: Panel, settings: Settings) -> pd.Series:
    """Deviation of each vendor's re-based close from the cross-vendor median.
    With only two vendors the median is their mean, so both get flagged — the
    report groups by date so the pair is still obvious."""
    col = "close_split" if "close_split" in panel.long.columns else "close"
    wide = panel.wide(col)
    if wide.columns.get_level_values(0).nunique() < 2:
        return _empty_bar()
    med = wide.T.groupby(level="ticker").median().T          # date × ticker
    n_src = wide.T.groupby(level="ticker").count().T          # how many vendors that day
    rel = wide.div(med.reindex(columns=wide.columns.get_level_values(1)).set_axis(wide.columns, axis=1)) - 1
    enough = n_src.reindex(columns=wide.columns.get_level_values(1)).set_axis(wide.columns, axis=1) >= 2
    flag = (rel.abs() > settings.vendor_disagreement_threshold) & enough
    s = flag.stack(level=[0, 1], future_stack=True)
    s = s[s.index.get_level_values(0).notna()]
    s.index = s.index.reorder_levels([1, 2, 0]).set_names(BAR_KEY)
    # drop (source,ticker,date) combos where the vendor had no bar at all
    present = _indexed(panel).index
    s = s.reindex(present).fillna(False)
    return s.astype(bool).rename("vendor_disagreement")


@bar_check
def volume_outlier(panel: Panel, settings: Settings) -> pd.Series:
    """Volume > 25x its trailing-60-session median. Uses the split-adjusted
    volume so a raw-basis vendor's legitimate 10x post-split volume jump is
    not flagged (that would be 30 false positives per split)."""
    col = "volume_split" if "volume_split" in panel.long.columns else "volume"
    df = _indexed(panel)
    med = df.groupby(level=[0, 1], sort=False)[col].transform(
        lambda s: s.rolling(60, min_periods=20).median().shift(1)
    )
    flag = (df[col] > 25 * med) & med.gt(0)
    return flag.fillna(False).astype(bool).rename("volume_outlier")


@bar_check
def duplicate_bar(panel: Panel, settings: Settings) -> pd.Series:
    dup = panel.long.duplicated(subset=BAR_KEY, keep=False)
    s = pd.Series(dup.values, index=pd.MultiIndex.from_frame(panel.long[BAR_KEY]))
    return s[~s.index.duplicated()].astype(bool).rename("duplicate_bar")


# ==========================================================================
# ticker-level checks
# ==========================================================================
@ticker_check
def stale_series(panel: Panel, settings: Settings, **_) -> pd.Series:
    """Last bar older than N business days before the requested end date."""
    if not panel.end:
        return _empty_tkr()
    end = pd.Timestamp(panel.end)
    sessions = nyse_sessions(end - pd.Timedelta(days=60), end)
    cutoff = sessions[-settings.stale_series_bdays - 1] if len(sessions) > settings.stale_series_bdays else sessions[0]
    last = panel.long.groupby(TKR_KEY)["date"].max()
    return (last < cutoff).astype(bool).rename("stale_series")


@ticker_check
def late_start(panel: Panel, settings: Settings, **_) -> pd.Series:
    """First bar arrives more than N sessions after the requested start: an IPO,
    a vendor gap — or a *reused ticker* (the symbol now belongs to a different
    company, e.g. FB after Meta's rename). Pair with ``ticker_change``/EDGAR."""
    if not panel.start:
        return _empty_tkr()
    start = pd.Timestamp(panel.start)
    sessions = nyse_sessions(start, start + pd.Timedelta(days=90))
    cutoff = sessions[min(settings.stale_series_bdays * 4, len(sessions) - 1)]
    first = panel.long.groupby(TKR_KEY)["date"].min()
    return (first > cutoff).astype(bool).rename("late_start")


@ticker_check
def no_data(panel: Panel, settings: Settings, **_) -> pd.Series:
    f = panel.failures
    if f.empty:
        return _empty_tkr()
    hit = f[f["error"].str.contains("NoData", na=False)]
    if hit.empty:
        return _empty_tkr()
    return pd.Series(True, index=pd.MultiIndex.from_frame(hit[TKR_KEY]).drop_duplicates()).rename("no_data")


@ticker_check
def ingest_rejected(panel: Panel, settings: Settings, **_) -> pd.Series:
    f = panel.failures
    if f.empty:
        return _empty_tkr()
    hit = f[f["error"].str.contains("SchemaError", na=False)]
    if hit.empty:
        return _empty_tkr()
    return pd.Series(True, index=pd.MultiIndex.from_frame(hit[TKR_KEY]).drop_duplicates()).rename("ingest_rejected")


@ticker_check
def ticker_change(panel: Panel, settings: Settings, edgar_map: Optional[dict] = None, **_) -> pd.Series:
    """Flag symbols that SEC's live ticker->CIK map no longer lists (renamed or
    delisted), falling back to the curated ``KNOWN_TICKER_CHANGES`` offline."""
    tickers = set(panel.long["ticker"].unique()) | set(panel.failures["ticker"].unique() if len(panel.failures) else [])
    sources = list(panel.sources) or ["-"]
    flagged = []
    for t in sorted(tickers):
        key = t.replace("-", ".").upper()   # BRK-B (Yahoo) vs BRK.B (SEC)
        if edgar_map:
            if key not in edgar_map and t.upper() not in edgar_map:
                flagged.append(t)
        elif t in KNOWN_TICKER_CHANGES:
            flagged.append(t)
    if not flagged:
        return _empty_tkr()
    idx = pd.MultiIndex.from_product([sources, flagged], names=TKR_KEY)
    return pd.Series(True, index=idx).rename("ticker_change")


@ticker_check
def coverage_mismatch(panel: Panel, settings: Settings, **_) -> pd.Series:
    rows = panel.long.groupby(TKR_KEY).size()
    best = rows.groupby(level="ticker").transform("max")
    return ((best - rows) / best > 0.02).astype(bool).rename("coverage_mismatch")


# ==========================================================================
# runner + results
# ==========================================================================
@dataclass
class QualityResult:
    bar_flags: pd.DataFrame          # (source,ticker,date) × check  (bool)
    ticker_flags: pd.DataFrame       # (source,ticker) × check (bool)
    settings: Settings
    timings: dict[str, float] = field(default_factory=dict)

    # -- long / tidy view --------------------------------------------------
    def failures(self) -> pd.DataFrame:
        """Long table of every failing (source, ticker, date|None, check)."""
        parts = []
        if len(self.bar_flags):
            b = self.bar_flags.stack(future_stack=True)
            b = b[b.astype(bool)].reset_index()
            b.columns = BAR_KEY + ["check", "flag"]
            parts.append(b.drop(columns="flag"))
        if len(self.ticker_flags):
            t = self.ticker_flags.stack(future_stack=True)
            t = t[t.astype(bool)].reset_index()
            t.columns = TKR_KEY + ["check", "flag"]
            t["date"] = pd.NaT
            parts.append(t[BAR_KEY + ["check"]])
        if not parts:
            return pd.DataFrame(columns=BAR_KEY + ["check"])
        return pd.concat(parts, ignore_index=True)

    # -- summaries ---------------------------------------------------------
    def summary_by_check(self) -> pd.DataFrame:
        """check × source failure counts + failure rate (% of bars)."""
        f = self.failures()
        n_bars = self.bar_flags.groupby(level="source").size() if len(self.bar_flags) else pd.Series(dtype=int)
        if f.empty:
            return pd.DataFrame(columns=["check", "source", "failures", "fail_rate_pct"])
        counts = f.groupby(["check", "source"]).size().rename("failures").reset_index()
        counts["fail_rate_pct"] = counts.apply(
            lambda r: 100 * r["failures"] / n_bars.get(r["source"], np.nan) if r["check"] in BAR_CHECKS else np.nan,
            axis=1,
        )
        return counts.sort_values(["failures"], ascending=False).reset_index(drop=True)

    def summary_by_ticker(self, top: int = 20) -> pd.DataFrame:
        f = self.failures()
        if f.empty:
            return pd.DataFrame(columns=["ticker", "failures"])
        return f.groupby("ticker").size().rename("failures").sort_values(ascending=False).head(top).reset_index()

    def matrix(self, source: str, check: Optional[str] = None) -> pd.DataFrame:
        """ticker × date boolean/count matrix for heatmaps. ``check=None`` -> any check."""
        if not len(self.bar_flags):
            return pd.DataFrame()
        b = self.bar_flags.xs(source, level="source")
        vals = b[check] if check else b.any(axis=1)
        m = vals.rename("flag").reset_index().pivot_table(index="ticker", columns="date", values="flag", aggfunc="max")
        return m.fillna(False).astype(int)

    def scorecard(self) -> pd.Series:
        """Headline numbers for the top of the report."""
        f = self.failures()
        n_bars = int(len(self.bar_flags))
        return pd.Series(
            {
                "bars_checked": n_bars,
                "bar_checks": len(BAR_CHECKS),
                "ticker_checks": len(TICKER_CHECKS),
                "bars_with_any_failure": int(self.bar_flags.any(axis=1).sum()) if n_bars else 0,
                "bar_failure_rate_pct": round(100 * self.bar_flags.any(axis=1).mean(), 3) if n_bars else 0.0,
                "ticker_level_failures": int(len(f[f["check"].isin(TICKER_CHECKS)])),
                "clean_series": int((~self.ticker_flags.any(axis=1)).sum()) if len(self.ticker_flags) else 0,
            }
        )


def run_checks(panel: Panel, settings: Optional[Settings] = None, *, edgar_map: Optional[dict] = None,
               checks: Optional[list[str]] = None) -> QualityResult:
    """Run every registered check (or the named subset) and assemble a :class:`QualityResult`."""
    import time

    settings = settings or Settings()
    base_index = _indexed(panel).index
    tkr_index = pd.MultiIndex.from_frame(panel.long[TKR_KEY].drop_duplicates()) if len(panel.long) else pd.MultiIndex.from_arrays([[], []], names=TKR_KEY)
    timings: dict[str, float] = {}

    bar_cols, tkr_cols = {}, {}
    for name, fn in BAR_CHECKS.items():
        if checks and name not in checks:
            continue
        t0 = time.perf_counter()
        try:
            s = fn(panel, settings)
            s = s.reindex(base_index.union(s.index[s.astype(bool)]), fill_value=False) if len(s) else pd.Series(False, index=base_index)
        except Exception as exc:  # a broken check must never take down the report
            log.exception("check %s failed: %s", name, exc)
            s = pd.Series(False, index=base_index)
        bar_cols[name] = s.astype(bool)
        timings[name] = time.perf_counter() - t0

    full_index = base_index
    for s in bar_cols.values():
        full_index = full_index.union(s.index)
    bar_flags = pd.DataFrame({k: v.reindex(full_index, fill_value=False) for k, v in bar_cols.items()}, index=full_index)
    bar_flags.index = bar_flags.index.set_names(BAR_KEY)
    bar_flags = bar_flags.sort_index()

    for name, fn in TICKER_CHECKS.items():
        if checks and name not in checks:
            continue
        t0 = time.perf_counter()
        try:
            s = fn(panel, settings, edgar_map=edgar_map)
        except Exception as exc:
            log.exception("check %s failed: %s", name, exc)
            s = _empty_tkr()
        tkr_cols[name] = s.astype(bool)
        timings[name] = time.perf_counter() - t0

    t_index = tkr_index
    for s in tkr_cols.values():
        t_index = t_index.union(s.index[s.astype(bool)]) if len(s) else t_index
    ticker_flags = pd.DataFrame({k: v.reindex(t_index, fill_value=False) for k, v in tkr_cols.items()}, index=t_index)
    ticker_flags.index = ticker_flags.index.set_names(TKR_KEY)
    ticker_flags = ticker_flags.sort_index()

    res = QualityResult(bar_flags=bar_flags, ticker_flags=ticker_flags, settings=settings, timings=timings)
    log.info("quality: %s", res.scorecard().to_dict())
    return res


# ==========================================================================
# scoring against injected ground truth (synthetic mode)
# ==========================================================================
DEFECT_TO_CHECK = {
    "missing_bar": "missing_bar",
    "zero_volume": "zero_volume",
    "stale_print": "stale_print",
    "price_spike": "price_jump",
    "truncated_series": "stale_series",
    "mislabeled_split_basis": "split_adjustment_mismatch",
    "ohlc_violation": "ingest_rejected",
    "missing_ticker": "no_data",
}


def score_against_injected(result: QualityResult, injected: pd.DataFrame,
                           expected_absent: tuple[str, ...] = ()) -> pd.DataFrame:
    """Recall / precision of each check vs the synthetic vendors' injected defects.

    * Defects injected into a series that was later rejected at ingest (or never
      returned) cannot be detected bar-by-bar, so they are excluded from truth.
    * ``expected_absent`` lists probe tickers (delisted names) whose ``no_data``
      flags are expected and therefore not counted as false positives.
    * A price spike produces two jumps (the spike and its reversion); a flag on
      the following bar counts as a true positive.
    """
    rows = []
    fails = result.failures()
    dead = set(map(tuple, fails[fails["check"].isin(["ingest_rejected", "no_data"])][TKR_KEY].values))
    injected = injected[~injected.apply(lambda r: (r["source"], r["ticker"]) in dead and r["kind"] not in ("ohlc_violation", "missing_ticker"), axis=1)]
    for kind, check in DEFECT_TO_CHECK.items():
        truth = injected[injected["kind"] == kind]
        if truth.empty:
            continue
        if check in BAR_CHECKS:
            got = fails[fails["check"] == check]
            if kind == "mislabeled_split_basis":
                # defect is per ticker; detection is on the split date -> compare on (source,ticker)
                truth_keys = set(map(tuple, truth[TKR_KEY].values))
                got_keys = set(map(tuple, got[TKR_KEY].values))
            else:
                truth_keys = set(map(tuple, truth[BAR_KEY].assign(date=pd.to_datetime(truth["date"])).values))
                got_keys = set(map(tuple, got[BAR_KEY].values))
                if kind == "price_spike":
                    # fold the reversion bar back onto the spike date
                    bars = result.bar_flags.index
                    remap = {}
                    for src, tkr, d in truth_keys:
                        dates = bars[(bars.get_level_values(0) == src) & (bars.get_level_values(1) == tkr)].get_level_values(2)
                        pos = dates.searchsorted(d)
                        if pos + 1 < len(dates):
                            remap[(src, tkr, dates[pos + 1])] = (src, tkr, d)
                    got_keys = {remap.get(k, k) for k in got_keys}
        else:
            if check == "no_data":
                fails_ = fails[~fails["ticker"].isin(expected_absent)]
                got = fails_[fails_["check"] == check]
                truth_keys = set(map(tuple, truth[TKR_KEY].values))
                got_keys = set(map(tuple, got[TKR_KEY].values))
                tp = len(truth_keys & got_keys)
                rows.append({"defect": kind, "check": check, "injected": len(truth_keys), "detected": tp,
                             "recall": tp / len(truth_keys) if truth_keys else np.nan, "flagged_total": len(got_keys),
                             "precision_vs_injected": tp / len(got_keys) if got_keys else np.nan})
                continue
            truth_keys = set(map(tuple, truth[TKR_KEY].values))
            got = fails[fails["check"] == check]
            got_keys = set(map(tuple, got[TKR_KEY].values))
        tp = len(truth_keys & got_keys)
        rows.append(
            {
                "defect": kind, "check": check, "injected": len(truth_keys), "detected": tp,
                "recall": tp / len(truth_keys) if truth_keys else np.nan,
                "flagged_total": len(got_keys),
                "precision_vs_injected": tp / len(got_keys) if got_keys else np.nan,
            }
        )
    return pd.DataFrame(rows)


# ==========================================================================
# helpers
# ==========================================================================
def _empty_bar() -> pd.Series:
    return pd.Series(dtype=bool, index=pd.MultiIndex.from_arrays([[], [], []], names=BAR_KEY))


def _empty_tkr() -> pd.Series:
    return pd.Series(dtype=bool, index=pd.MultiIndex.from_arrays([[], []], names=TKR_KEY))


def _split_date_mask(index: pd.MultiIndex) -> pd.Series:
    """True where (ticker, date) is a reference split date (snapped to the bar)."""
    mask = pd.Series(False, index=index)
    tickers = index.get_level_values("ticker").unique()
    for t in tickers:
        spl = reference_splits(t)
        if spl.empty:
            continue
        sub = index[index.get_level_values("ticker") == t]
        dates = pd.DatetimeIndex(sub.get_level_values("date")).unique().sort_values()
        for d in spl.index:
            pos = dates.searchsorted(d)
            if pos < len(dates):
                mask.loc[(slice(None), t, dates[pos])] = True
    return mask
