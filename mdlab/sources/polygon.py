"""
Polygon.io adapter.

Endpoints
---------
* Aggregates: ``GET /v2/aggs/ticker/{T}/range/1/day/{from}/{to}?adjusted=false|true``
      -> {"results": [{"t": epoch_ms, "o","h","l","c","v","vw","n"}], "next_url": ...}
* Splits:    ``GET /v3/reference/splits?ticker=T``     -> split_from / split_to / execution_date
* Dividends: ``GET /v3/reference/dividends?ticker=T``  -> cash_amount / ex_dividend_date

Semantics — the trap this project exists to catch
-------------------------------------------------
``adjusted=true`` on Polygon adjusts for **splits only**. It is *not* comparable
to Yahoo's or FMP's dividend-adjusted series. A backtest that mixes Polygon
"adjusted" with Yahoo "Adj Close" silently drops every dividend from one leg.

Free tier: 5 requests/minute, 2 years of history, end-of-day only. With
``fetch_adjusted=True`` each ticker costs 2 calls => ~24 tickers/10 minutes.
"""
from __future__ import annotations

import logging

import pandas as pd

from .base import DataSource, NoDataError, PermanentError

log = logging.getLogger(__name__)


class PolygonSource(DataSource):
    name = "polygon"
    close_basis = "raw"                 # adjusted=false is truly unadjusted
    adj_close_basis = "split"           # adjusted=true is split-only
    rate_limit = (5, 60.0)              # free tier
    provides_actions = True

    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str, fetch_adjusted: bool = True, **kw) -> None:
        if not api_key:
            raise PermanentError("POLYGON_API_KEY is required for PolygonSource")
        super().__init__(**kw)
        self.api_key = api_key
        self.fetch_adjusted = fetch_adjusted

    def _call(self, url: str, **params) -> dict:
        params["apiKey"] = self.api_key
        payload = self._get_json(url, params=params)
        if not isinstance(payload, dict):
            raise PermanentError("Polygon returned an unexpected payload")
        if payload.get("status") == "ERROR":
            raise PermanentError(f"Polygon error: {payload.get('error') or payload}")
        return payload

    def _aggs(self, ticker: str, start: str, end: str, adjusted: bool) -> pd.DataFrame:
        url = f"{self.BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        rows: list = []
        payload = self._call(url, adjusted=str(adjusted).lower(), sort="asc", limit=50000)
        rows.extend(payload.get("results") or [])
        # paginate (rarely needed for daily bars, but correct)
        while payload.get("next_url"):
            payload = self._call(payload["next_url"])
            rows.extend(payload.get("results") or [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # ``t`` is the bar's start timestamp in UTC ms; daily bars are at 00:00 ET
        # -> convert to New York wall-clock before dropping the time component
        ts = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
        df.index = ts.dt.tz_localize(None).dt.normalize()
        return df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[
            ["open", "high", "low", "close", "volume"]
        ]

    def _fetch_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        raw = self._aggs(ticker, start, end, adjusted=False)
        if raw.empty:
            raise NoDataError(f"Polygon has no bars for {ticker}")
        if self.fetch_adjusted:
            # counts as a second request against the 5/min budget
            self.stats.rate_limit_sleep += self._limiter.acquire()
            self.stats.requests += 1
            adj = self._aggs(ticker, start, end, adjusted=True)
            raw["adj_close"] = adj["close"].reindex(raw.index)
        return raw

    def _fetch_actions(self, ticker: str, start: str, end: str) -> tuple[pd.Series, pd.Series]:
        spl = self._call(f"{self.BASE}/v3/reference/splits", ticker=ticker, limit=1000).get("results") or []
        div = self._call(f"{self.BASE}/v3/reference/dividends", ticker=ticker, limit=1000).get("results") or []

        splits = pd.Series(dtype=float)
        if spl:
            s = pd.DataFrame(spl)
            splits = pd.Series(
                (s["split_to"].astype(float) / s["split_from"].astype(float)).values,
                index=pd.to_datetime(s["execution_date"]),
            ).sort_index()
        dividends = pd.Series(dtype=float)
        if div:
            d = pd.DataFrame(div)
            dividends = pd.Series(d["cash_amount"].astype(float).values,
                                  index=pd.to_datetime(d["ex_dividend_date"])).sort_index()
            dividends = dividends.groupby(level=0).sum()  # multiple payments same ex-date
        rng = lambda s: s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
        return rng(dividends), rng(splits)
