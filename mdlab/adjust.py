"""
Corporate-action adjustment: rebuild split- and dividend-adjusted series from
raw prices + an actions table.

The mental model
----------------
An adjustment is a *multiplicative factor* applied to every bar **before** an
event so that the series is continuous across the event.

* **Split** (ratio ``r``, e.g. 10.0 for a 10-for-1 on date ``d``): every price
  before ``d`` is multiplied by ``1/r``; every volume before ``d`` by ``r``.
  Nothing about shareholder wealth changed, so the *price-return* series must
  show no jump.

* **Cash dividend** (``D`` per share, ex-date ``d``): on the ex-date the stock
  opens lower by ~``D``. A price-return series shows a drop; a *total-return*
  series must not, because the holder received ``D`` in cash. CRSP / Yahoo
  convention: every price before ``d`` is multiplied by
  ``1 - D / P_{d-1}`` where ``P_{d-1}`` is the last close before the ex-date
  (both on the same split basis).

Factors compound backwards in time: the factor for bar ``t`` is the product of
the factors of every event *after* ``t``. The most recent bar always has factor
1.0, so adjusted prices equal actual prices today and differ increasingly as
you go back — which is why an adjusted series is **not point-in-time**: it is
rewritten every time a new dividend goes ex.

Total return vs price return
----------------------------
Ignoring dividends understates equity returns by roughly the dividend yield
(~1.5-2%/yr for the S&P 500, 3-5% for utilities/REITs). Over a 20-year backtest
that compounds to a 30-100% difference in terminal wealth.
"""
from __future__ import annotations

import logging
from typing import Literal

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

Basis = Literal["raw", "split", "total_return"]


def _align_events(index: pd.DatetimeIndex, events: pd.Series) -> pd.Series:
    """Snap each event date onto the first bar >= event date (ex-dates that fall
    on a holiday/weekend in a vendor table land on the next session)."""
    if events is None or len(events) == 0:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="date"))
    events = events[events != 0].dropna()
    pos = index.searchsorted(pd.DatetimeIndex(events.index))
    keep = pos < len(index)
    snapped = pd.Series(events.values[keep], index=index[pos[keep]])
    return snapped.groupby(level=0).prod() if events.name == "splits" else snapped.groupby(level=0).sum()


def split_factors(index: pd.DatetimeIndex, splits: pd.Series) -> pd.Series:
    """Per-bar price multiplier that removes splits. 1.0 after the last split.

    >>> idx = pd.to_datetime(["2024-06-06","2024-06-07","2024-06-10","2024-06-11"])
    >>> split_factors(idx, pd.Series([10.0], index=pd.to_datetime(["2024-06-10"]), name="splits")).tolist()
    [0.1, 0.1, 1.0, 1.0]
    """
    f = pd.Series(1.0, index=index)
    ev = _align_events(index, splits.rename("splits") if splits is not None else splits)
    for d, r in ev.items():
        if r and r > 0 and r != 1.0:
            f.loc[f.index < d] /= r
    return f


def dividend_factors(close_split_adj: pd.Series, dividends_split_adj: pd.Series) -> pd.Series:
    """Per-bar multiplier that removes dividend drops (CRSP convention).

    Both inputs must be on the *same split basis* — pass the split-adjusted
    close and dividends scaled by the same split factors.
    """
    f = pd.Series(1.0, index=close_split_adj.index)
    ev = _align_events(close_split_adj.index, dividends_split_adj.rename("dividends")
                       if dividends_split_adj is not None else dividends_split_adj)
    for d, amount in ev.items():
        if not amount or amount <= 0:
            continue
        prior = close_split_adj.loc[close_split_adj.index < d]
        if prior.empty or pd.isna(prior.iloc[-1]) or prior.iloc[-1] <= 0:
            continue
        ratio = 1.0 - amount / prior.iloc[-1]
        if ratio <= 0:  # dividend larger than price = data error; skip loudly
            log.warning("dividend %.4f >= prior close %.4f on %s — skipped", amount, prior.iloc[-1], d.date())
            continue
        f.loc[f.index < d] *= ratio
    return f


def adjust_bars(
    bars: pd.DataFrame,
    splits: pd.Series,
    dividends: pd.Series,
    *,
    input_basis: Literal["raw", "split_adjusted"] = "raw",
    target: Basis = "total_return",
) -> pd.DataFrame:
    """Return a copy of ``bars`` with open/high/low/close/volume re-based.

    Parameters
    ----------
    bars : canonical bar frame (see ``schema.py``)
    splits, dividends : event Series indexed by (ex-)date, in *raw* share terms
    input_basis : what ``bars['close']`` currently is (Yahoo -> "split_adjusted")
    target : "raw" | "split" | "total_return"

    Notes
    -----
    Dividends in an actions table are quoted per *then-outstanding* share. When
    a vendor already split-adjusted its prices (Yahoo), it also split-adjusts
    the dividend amounts, so we scale dividends by the same split factor before
    computing dividend factors.
    """
    out = bars.copy()
    if out.empty:
        return out
    idx = out.index
    price_cols = [c for c in ("open", "high", "low", "close") if c in out.columns]

    sf = split_factors(idx, splits)                      # raw -> split-adjusted
    if input_basis == "split_adjusted":
        # undo the vendor's split adjustment first so every path starts from raw
        out[price_cols] = out[price_cols].div(sf, axis=0)
        if "volume" in out:
            out["volume"] = out["volume"].mul(sf, axis=0)

    if target == "raw":
        return out

    out[price_cols] = out[price_cols].mul(sf, axis=0)
    if "volume" in out:
        out["volume"] = out["volume"].div(sf, axis=0)
    if target == "split":
        return out

    div_adj = (dividends.copy() if dividends is not None else pd.Series(dtype=float))
    if len(div_adj):
        div_adj = _align_events(idx, div_adj.rename("dividends"))
        div_adj = div_adj * sf.reindex(div_adj.index).fillna(1.0)
    df_ = dividend_factors(out["close"], div_adj)
    out[price_cols] = out[price_cols].mul(df_, axis=0)
    return out


def total_return_index(close_split_adj: pd.Series, dividends_split_adj: pd.Series, base: float = 100.0) -> pd.Series:
    """Growth of ``base`` with dividends reinvested on the ex-date at the close."""
    factors = dividend_factors(close_split_adj, dividends_split_adj)
    adj = close_split_adj * factors
    return base * adj / adj.dropna().iloc[0]


def compare_bases(bars: pd.DataFrame, splits: pd.Series, dividends: pd.Series, input_basis: str) -> pd.DataFrame:
    """Side-by-side close on all three bases — the payload for the overlay chart."""
    frame = pd.DataFrame(index=bars.index)
    for basis in ("raw", "split", "total_return"):
        frame[basis] = adjust_bars(bars, splits, dividends, input_basis=input_basis, target=basis)["close"]
    return frame
