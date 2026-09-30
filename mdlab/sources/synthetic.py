"""
Deterministic synthetic vendors with **fault injection**.

Purpose
-------
1. Offline development: the whole pipeline runs with zero API keys / network.
2. Testing the quality checks: because every defect is *injected on purpose and
   recorded*, we can compute detection recall/precision for each check instead
   of eyeballing a report and hoping.

How it works
------------
A shared :class:`TrueMarket` generates one "true" raw price path per ticker
(geometric Brownian motion over real NYSE sessions, seeded by the ticker name),
with reference splits from ``universe.REFERENCE_SPLITS`` and synthetic quarterly
dividends. Each :class:`SyntheticSource` renders that truth onto a vendor's
conventions (raw vs split-adjusted close, split-only vs total-return adj_close)
and then corrupts it according to a :class:`DefectProfile`.

Three ready-made vendors mimic the real ones' conventions:

* ``synth_a`` — Yahoo-like  (close split-adjusted, adj_close total-return); drops bars, stale prints
* ``synth_b`` — FMP-like    (close raw, adj_close total-return); zero-volume days, one fat-finger spike
* ``synth_c`` — Polygon-like(close raw, adj_close split-only); truncated history, mislabels its split basis on one name
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from ..adjust import adjust_bars
from ..calendar import nyse_sessions
from ..universe import reference_splits
from .base import DataSource, NoDataError

log = logging.getLogger(__name__)


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------
class TrueMarket:
    """Vendor-independent 'truth' — the thing every vendor is a noisy view of."""

    def __init__(self, annual_vol: float = 0.30, annual_drift: float = 0.08, dividend_yield: float = 0.02,
                 delisted: tuple[str, ...] = ("FB", "TWTR", "ATVI")) -> None:
        self.annual_vol = annual_vol
        self.annual_drift = annual_drift
        self.dividend_yield = dividend_yield
        self.delisted = set(delisted)
        self._cache: dict[tuple, pd.DataFrame] = {}

    def actions(self, ticker: str, start: str, end: str) -> tuple[pd.Series, pd.Series]:
        sessions = nyse_sessions(start, end)
        splits = reference_splits(ticker)
        splits = splits[(splits.index >= sessions[0]) & (splits.index <= sessions[-1])]
        # quarterly dividends for ~half the names (deterministic choice)
        divs = pd.Series(dtype="float64", name="dividends")
        if _seed(ticker, "pays") % 2 == 0 and len(sessions) > 0:
            raw = self.raw_bars(ticker, start, end)
            ex_dates = sessions[(sessions.month % 3 == 2) & (sessions.day <= 7)]
            ex_dates = ex_dates[~ex_dates.to_series().dt.to_period("M").duplicated().values]
            prev_close = raw["close"].shift(1).reindex(ex_dates)
            divs = (prev_close * self.dividend_yield / 4).round(4).dropna().rename("dividends")
        return divs, splits

    def raw_bars(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Truly raw (unadjusted) OHLCV as traded, including split jumps and ex-div drops."""
        key = (ticker, start, end)
        if key in self._cache:
            return self._cache[key]
        if ticker in self.delisted:
            raise NoDataError(f"{ticker} is delisted in the synthetic market")

        sessions = nyse_sessions(start, end)
        n = len(sessions)
        rng = np.random.default_rng(_seed(ticker, "path"))
        dt = 1 / 252
        vol = self.annual_vol * (0.6 + 0.8 * rng.random())
        rets = rng.normal((self.annual_drift - 0.5 * vol**2) * dt, vol * np.sqrt(dt), n)
        p0 = 20 + 480 * rng.random()
        # split-adjusted "economic" path first...
        econ = p0 * np.exp(np.cumsum(rets))
        econ_close = pd.Series(econ, index=sessions)
        # ...then re-introduce splits so the raw path has real jumps
        splits = reference_splits(ticker)
        splits = splits[(splits.index >= sessions[0]) & (splits.index <= sessions[-1])]
        raw_close = econ_close.copy()
        for d, r in splits.items():
            raw_close.loc[raw_close.index < d] *= r
        # ex-dividend drops (approximate: price falls by D at open of ex-date)
        if _seed(ticker, "pays") % 2 == 0:
            ex_dates = sessions[(sessions.month % 3 == 2) & (sessions.day <= 7)]
            ex_dates = ex_dates[~ex_dates.to_series().dt.to_period("M").duplicated().values]
            for d in ex_dates:
                prior = raw_close.loc[raw_close.index < d]
                if prior.empty:
                    continue
                amt = round(prior.iloc[-1] * self.dividend_yield / 4, 4)
                raw_close.loc[raw_close.index >= d] *= (1 - amt / prior.iloc[-1])

        intraday = rng.normal(0, 0.006, size=(n, 3))
        open_ = raw_close.shift(1).fillna(raw_close.iloc[0]) * (1 + intraday[:, 0])
        high = np.maximum(open_, raw_close) * (1 + np.abs(intraday[:, 1]))
        low = np.minimum(open_, raw_close) * (1 - np.abs(intraday[:, 2]))
        shares_scale = 1e6 * (1 + 20 * rng.random())
        volume = (rng.lognormal(0, 0.5, n) * shares_scale).round()
        # raw volume is in *then-current* shares -> smaller before a split
        vol_series = pd.Series(volume, index=sessions)
        for d, r in splits.items():
            vol_series.loc[vol_series.index < d] /= r

        df = pd.DataFrame(
            {"open": open_.values, "high": high, "low": low, "close": raw_close.values,
             "volume": vol_series.round().values},
            index=sessions,
        )
        df.index.name = "date"
        self._cache[key] = df
        return df


# --------------------------------------------------------------------------
# Defects
# --------------------------------------------------------------------------
@dataclass
class DefectProfile:
    """Knobs for corrupting a vendor's view. Every injected defect is recorded."""

    drop_bar_frac: float = 0.0          # randomly delete this fraction of bars
    zero_volume_frac: float = 0.0       # set volume=0 on this fraction of bars
    stale_runs_per_ticker: int = 0      # number of flat-close runs to inject
    stale_run_length: int = 4
    spike_tickers: tuple[str, ...] = ()  # one +50% fat-finger close on these
    spike_size: float = 0.5
    truncate_last_n: int = 0            # drop the final N bars (dead feed)
    truncate_tickers: tuple[str, ...] = ()
    mislabel_split_tickers: tuple[str, ...] = ()   # claim split-adjusted, deliver raw
    ohlc_violation_tickers: tuple[str, ...] = ()   # high < low on one bar -> schema reject
    missing_ticker: tuple[str, ...] = ()           # vendor simply lacks the symbol


@dataclass
class InjectedDefect:
    source: str
    ticker: str
    date: Optional[pd.Timestamp]
    kind: str


class SyntheticSource(DataSource):
    """A fake vendor: ``TrueMarket`` rendered onto vendor conventions + defects."""

    provides_actions = True

    def __init__(self, name: str, market: TrueMarket, defects: DefectProfile,
                 close_basis: str = "raw", adj_close_basis: str = "total_return", **kw) -> None:
        super().__init__(**kw)
        self.name = name
        self.market = market
        self.defects = defects
        self.close_basis = close_basis
        self.adj_close_basis = adj_close_basis
        self.actions_basis = "split_adjusted" if close_basis == "split_adjusted" else "raw"
        self.injected: list[InjectedDefect] = []
        self.rate_limit = None

    # -- contract ----------------------------------------------------------
    def _fetch_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        d = self.defects
        if ticker in d.missing_ticker:
            self.injected.append(InjectedDefect(self.name, ticker, None, "missing_ticker"))
            raise NoDataError(f"{self.name} does not carry {ticker}")

        raw = self.market.raw_bars(ticker, start, end).copy()
        divs, splits = self.market.actions(ticker, start, end)
        raw["dividends"] = divs.reindex(raw.index).fillna(0.0)
        raw["splits"] = splits.reindex(raw.index).fillna(0.0)

        # vendor's own adjusted close, on its declared basis
        target = "total_return" if self.adj_close_basis == "total_return" else "split"
        raw["adj_close"] = adjust_bars(raw, splits, divs, input_basis="raw", target=target)["close"]

        # vendor's "unadjusted" close on its declared basis
        deliver_split_adjusted = self.close_basis == "split_adjusted"
        if ticker in d.mislabel_split_tickers and deliver_split_adjusted:
            deliver_split_adjusted = False       # says split-adjusted, ships raw
            self.injected.append(InjectedDefect(self.name, ticker, None, "mislabeled_split_basis"))
        if deliver_split_adjusted:
            raw = _to_split_adjusted_keep_extra(raw, splits, divs)

        rng = np.random.default_rng(_seed(self.name, ticker))
        n = len(raw)

        if d.truncate_last_n and ticker in d.truncate_tickers and n > d.truncate_last_n:
            raw = raw.iloc[: -d.truncate_last_n]
            self.injected.append(InjectedDefect(self.name, ticker, raw.index[-1], "truncated_series"))
            n = len(raw)

        if d.drop_bar_frac > 0:
            k = int(round(n * d.drop_bar_frac))
            drop_idx = raw.index[rng.choice(n, size=k, replace=False)] if k else raw.index[:0]
            for dt in drop_idx:
                self.injected.append(InjectedDefect(self.name, ticker, dt, "missing_bar"))
            raw = raw.drop(drop_idx)
            n = len(raw)

        if d.zero_volume_frac > 0:
            k = int(round(n * d.zero_volume_frac))
            pos = rng.choice(n, size=k, replace=False) if k else []
            for p in pos:
                raw.iloc[p, raw.columns.get_loc("volume")] = 0.0
                self.injected.append(InjectedDefect(self.name, ticker, raw.index[p], "zero_volume"))

        for _ in range(d.stale_runs_per_ticker):
            if n <= d.stale_run_length + 2:
                break
            s = int(rng.integers(1, n - d.stale_run_length - 1))
            cols = ["open", "high", "low", "close", "adj_close"]
            for p in range(s + 1, s + d.stale_run_length):
                raw.iloc[p, [raw.columns.get_loc(c) for c in cols]] = raw.iloc[s][cols].values
                self.injected.append(InjectedDefect(self.name, ticker, raw.index[p], "stale_print"))

        if ticker in d.spike_tickers and n > 10:
            p = int(rng.integers(5, n - 5))
            for c in ("open", "high", "low", "close", "adj_close"):
                raw.iloc[p, raw.columns.get_loc(c)] *= (1 + d.spike_size)
            self.injected.append(InjectedDefect(self.name, ticker, raw.index[p], "price_spike"))

        if ticker in d.ohlc_violation_tickers and n > 10:
            p = int(rng.integers(5, n - 5))
            hi, lo = raw.columns.get_loc("high"), raw.columns.get_loc("low")
            raw.iloc[p, hi], raw.iloc[p, lo] = raw.iloc[p, lo], raw.iloc[p, hi]  # swap -> high < low
            self.injected.append(InjectedDefect(self.name, ticker, raw.index[p], "ohlc_violation"))

        return raw

    def _fetch_actions(self, ticker: str, start: str, end: str) -> tuple[pd.Series, pd.Series]:
        if ticker in self.defects.missing_ticker or ticker in self.market.delisted:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        div, spl = self.market.actions(ticker, start, end)
        if self.actions_basis == "split_adjusted" and len(div):
            from ..adjust import split_factors

            div = div * split_factors(pd.DatetimeIndex(div.index), spl)   # restate in post-split shares, like Yahoo
        return div, spl

    def injected_frame(self) -> pd.DataFrame:
        cols = ["source", "ticker", "date", "kind"]
        if not self.injected:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame([vars(x) for x in self.injected], columns=cols)


def _to_split_adjusted_keep_extra(raw: pd.DataFrame, splits: pd.Series, divs: pd.Series) -> pd.DataFrame:
    adj = adjust_bars(raw, splits, divs, input_basis="raw", target="split")
    adj["adj_close"] = raw["adj_close"]
    # Yahoo also split-adjusts the dividend amounts it reports
    from ..adjust import split_factors

    adj["dividends"] = raw["dividends"] * split_factors(raw.index, splits)
    adj["splits"] = raw["splits"]
    return adj


# --------------------------------------------------------------------------
# Ready-made trio
# --------------------------------------------------------------------------
def make_synthetic_trio(cache=None, market: Optional[TrueMarket] = None) -> list[SyntheticSource]:
    """Three vendors whose conventions mirror Yahoo / FMP / Polygon."""
    market = market or TrueMarket()
    a = SyntheticSource(
        "synth_a", market, close_basis="split_adjusted", adj_close_basis="total_return", cache=cache,
        defects=DefectProfile(drop_bar_frac=0.01, stale_runs_per_ticker=1, missing_ticker=("PLD",),
                              mislabel_split_tickers=("WMT",)),
    )
    b = SyntheticSource(
        "synth_b", market, close_basis="raw", adj_close_basis="total_return", cache=cache,
        defects=DefectProfile(zero_volume_frac=0.005, spike_tickers=("KO", "CAT"),
                              ohlc_violation_tickers=("PEP",)),
    )
    c = SyntheticSource(
        "synth_c", market, close_basis="raw", adj_close_basis="split", cache=cache,
        defects=DefectProfile(truncate_last_n=12, truncate_tickers=("DUK", "F"),
                              drop_bar_frac=0.002),
    )
    return [a, b, c]
