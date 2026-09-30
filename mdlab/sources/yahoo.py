"""
Yahoo Finance adapter (via ``yfinance``).

Vendor quirks worth knowing (all handled here)
----------------------------------------------
* ``auto_adjust=False`` gives ``Close`` **and** ``Adj Close``. Despite the name,
  Yahoo's ``Close`` is already *split-adjusted* (Yahoo rewrites history on every
  split); ``Adj Close`` additionally adjusts for cash dividends (total-return
  basis, CRSP-style multiplicative factors). Volume is split-adjusted too.
* Recent yfinance versions return MultiIndex columns ``(field, ticker)`` even for
  a single ticker — we flatten defensively.
* Delisted / renamed symbols (FB, TWTR) return an empty frame plus a logged
  "possibly delisted" message rather than an HTTP error.
* Yahoo has no published rate limit but will throttle aggressive scraping; a
  modest self-imposed limit keeps batch jobs reliable.
"""
from __future__ import annotations

import logging

import pandas as pd

from .base import DataSource, NoDataError, TransientError

log = logging.getLogger(__name__)


class YahooSource(DataSource):
    name = "yahoo"
    close_basis = "split_adjusted"      # <- the important, non-obvious fact
    adj_close_basis = "total_return"
    actions_basis = "split_adjusted"    # Yahoo restates historical dividends in post-split shares
    rate_limit = (120, 60.0)            # 2 req/s self-imposed
    provides_actions = True

    _RENAME = {
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Adj Close": "adj_close", "Volume": "volume",
        "Dividends": "dividends", "Stock Splits": "splits",
    }

    def _fetch_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        import yfinance as yf  # imported lazily so offline users never need it

        # yfinance's ``end`` is exclusive -> push by one day to make ours inclusive
        end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            df = yf.download(
                ticker, start=start, end=end_excl,
                auto_adjust=False, actions=True, progress=False, threads=False,
            )
        except Exception as exc:  # yfinance wraps requests errors in a variety of ways
            msg = str(exc).lower()
            if any(k in msg for k in ("timed out", "connection", "429", "too many")):
                raise TransientError(f"yfinance transient error: {exc}") from exc
            raise

        if df is None or df.empty:
            raise NoDataError(f"no bars for {ticker} (delisted / renamed / unknown symbol?)")

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=self._RENAME)
        # Yahoo occasionally returns all-NaN rows on holidays; drop them
        df = df.dropna(subset=["close"], how="all")
        return df

    def _fetch_actions(self, ticker: str, start: str, end: str) -> tuple[pd.Series, pd.Series]:
        import yfinance as yf

        t = yf.Ticker(ticker)
        actions = t.actions  # columns: Dividends, Stock Splits ; index tz-aware
        if actions is None or actions.empty:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        idx = actions.index.tz_convert(None) if actions.index.tz is not None else actions.index
        actions = actions.set_axis(idx.normalize())
        mask = (actions.index >= pd.Timestamp(start)) & (actions.index <= pd.Timestamp(end))
        actions = actions.loc[mask]
        # a company that never split / never paid has no such column
        div = actions["Dividends"] if "Dividends" in actions else pd.Series(dtype=float)
        spl = actions["Stock Splits"] if "Stock Splits" in actions else pd.Series(dtype=float)
        return div, spl
