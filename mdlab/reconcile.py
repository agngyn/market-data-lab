"""
Cross-vendor reconciliation.

The question this module answers: *for the same ticker on the same day, do the
vendors agree?* — and when they don't, is it (a) a genuine data error, (b) a
methodology difference (adjustment basis), or (c) a timing difference (one
vendor restated, the other hasn't yet)?

Procedure
---------
1. Outer-join the chosen field across sources -> ``date × (source, ticker)``.
2. For every vendor pair compute ``pct_diff = a / b - 1``.
3. Summarise per pair and per ticker: median |diff|, p99 |diff|, share of days
   beyond a tolerance, and worst offenders.

Which field to compare matters:

* ``close_split`` (our re-based split-adjusted close) is the fair comparison —
  it removes the raw vs split-adjusted convention gap.
* ``adj_close`` (each vendor's *own* adjusted series) is the *unfair* comparison
  that exposes methodology: Polygon (split-only) vs Yahoo/FMP (total-return)
  will diverge by the cumulative dividend yield. Deliberately reporting both
  is how the "documented discrepancy" is produced.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .panel import Panel

log = logging.getLogger(__name__)


@dataclass
class Reconciliation:
    field: str
    wide: pd.DataFrame                 # date × (source, ticker)
    pair_diffs: pd.DataFrame           # long: date, ticker, pair, pct_diff
    pair_summary: pd.DataFrame         # per pair stats
    ticker_summary: pd.DataFrame       # per (pair, ticker) stats
    tolerance: float

    def worst(self, n: int = 15) -> pd.DataFrame:
        """The n largest absolute disagreements — the first thing to eyeball."""
        d = self.pair_diffs.dropna(subset=["pct_diff"]).copy()
        if d.empty:
            return pd.DataFrame(columns=["date", "ticker", "pair", "diff_pct"])
        d["pct_diff"] = d["pct_diff"].astype(float)
        d["abs_diff"] = d["pct_diff"].abs()
        out = d.nlargest(n, "abs_diff")[["date", "ticker", "pair", "pct_diff"]].reset_index(drop=True)
        out["diff_pct"] = 100 * out.pop("pct_diff")
        return out


def reconcile(panel: Panel, field: str = "close_split", tolerance: float = 0.01) -> Reconciliation:
    """Pairwise vendor comparison of ``field``.

    Returns a :class:`Reconciliation`. ``pct_diff`` is ``a/b - 1`` where the pair
    label is ``"a|b"`` in sorted source order, so the sign is interpretable.
    """
    if field not in panel.long.columns:
        raise KeyError(f"{field!r} not in panel; run add_rebased_closes() for close_split/close_tr")
    wide = panel.wide(field)
    sources = sorted(wide.columns.get_level_values(0).unique())
    if len(sources) < 2:
        log.warning("reconcile: need >=2 sources, got %s", sources)
        empty = pd.DataFrame(columns=["date", "ticker", "pair", "pct_diff"])
        return Reconciliation(field, wide, empty, pd.DataFrame(), pd.DataFrame(), tolerance)

    rows = []
    for a, b in itertools.combinations(sources, 2):
        wa, wb = wide[a], wide[b]
        common = wa.columns.intersection(wb.columns)
        if len(common) == 0:
            continue
        diff = wa[common] / wb[common] - 1.0
        stacked = diff.stack(future_stack=True).rename("pct_diff").reset_index()
        stacked.columns = ["date", "ticker", "pct_diff"]
        stacked["pair"] = f"{a}|{b}"
        # keep rows where at least one vendor has a value: a NaN here is a
        # coverage gap, which is also information
        both_nan = wa[common].isna() & wb[common].isna()
        keep = ~both_nan.stack(future_stack=True).values
        rows.append(stacked.loc[keep])

    pair_diffs = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["date", "ticker", "pct_diff", "pair"])
    pair_diffs = pair_diffs[["date", "ticker", "pair", "pct_diff"]]

    def _stats(g: pd.DataFrame) -> pd.Series:
        d = g["pct_diff"]
        present = d.notna()
        absd = d.abs()
        return pd.Series(
            {
                "n_days": int(len(g)),
                "n_both": int(present.sum()),
                "coverage_gap_pct": float(100 * (1 - present.mean())) if len(g) else np.nan,
                "median_abs_bps": float(1e4 * absd.median()) if present.any() else np.nan,
                "p99_abs_bps": float(1e4 * absd.quantile(0.99)) if present.any() else np.nan,
                "max_abs_pct": float(100 * absd.max()) if present.any() else np.nan,
                "pct_beyond_tol": float(100 * (absd > tolerance).sum() / max(present.sum(), 1)),
                "mean_signed_bps": float(1e4 * d.mean()) if present.any() else np.nan,
            }
        )

    pair_summary = pair_diffs.groupby("pair").apply(_stats, include_groups=False).reset_index() if len(pair_diffs) else pd.DataFrame()
    ticker_summary = (
        pair_diffs.groupby(["pair", "ticker"]).apply(_stats, include_groups=False).reset_index()
        if len(pair_diffs) else pd.DataFrame()
    )
    return Reconciliation(field, wide, pair_diffs, pair_summary, ticker_summary, tolerance)


def methodology_gap(panel: Panel, tolerance: float = 0.01) -> pd.DataFrame:
    """Compare each vendor's *own* ``adj_close`` to our total-return rebase.

    A vendor that adjusts for splits only (Polygon) shows a systematic, slowly
    growing gap that equals the cumulative dividend factor — a fingerprint that
    distinguishes methodology from noise. Returns per-(source, ticker) stats.
    """
    if "close_tr" not in panel.long.columns:
        raise KeyError("run add_rebased_closes() first")
    df = panel.long.dropna(subset=["adj_close", "close_tr"]).copy()
    # normalise each series to its last observation so level differences cancel
    def _norm(g):
        g = g.sort_values("date")
        last_adj, last_tr = g["adj_close"].iloc[-1], g["close_tr"].iloc[-1]
        ratio = (g["adj_close"] / last_adj) / (g["close_tr"] / last_tr) - 1.0
        return pd.Series(
            {
                "median_gap_bps": 1e4 * ratio.abs().median(),
                "gap_at_start_bps": 1e4 * ratio.iloc[0],
                "pct_days_beyond_tol": 100 * (ratio.abs() > tolerance).mean(),
                "n": len(g),
            }
        )

    out = df.groupby(["source", "ticker"]).apply(_norm, include_groups=False).reset_index()
    out["vendor_adj_basis"] = out["source"].map(lambda s: panel.sources.get(s, {}).get("adj_close_basis"))
    return out.sort_values(["source", "median_gap_bps"], ascending=[True, False])
