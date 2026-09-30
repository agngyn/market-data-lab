"""
Financial Modeling Prep (FMP) adapter.

Endpoints (``stable`` API, 2025+; the legacy ``/api/v3`` shape is also parsed
for older keys):

* ``/stable/historical-price-eod/full?symbol=AAPL&from=&to=``
      -> list of {symbol, date, open, high, low, close, volume, change, changePercent, vwap}
* ``/stable/historical-price-eod/dividend-adjusted?symbol=...``
      -> list of {date, adjOpen, adjHigh, adjLow, adjClose, volume}
* ``/stable/dividends?symbol=`` and ``/stable/splits?symbol=``

Semantics: ``close`` is truly raw (unadjusted); ``adjClose`` is adjusted for
splits and dividends (total-return basis, like Yahoo's Adj Close).

Free tier: 250 requests/day, so fetching adjusted + raw costs 2 calls/ticker.
Set ``fetch_adjusted=False`` to halve usage and let ``mdlab.adjust`` rebuild the
adjusted series from raw + corporate actions instead.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from .base import DataSource, NoDataError, PermanentError

log = logging.getLogger(__name__)


class FMPSource(DataSource):
    name = "fmp"
    close_basis = "raw"
    adj_close_basis = "total_return"
    rate_limit = (250, 60.0)          # paid tiers allow ~300/min; free is 250/day (!)
    provides_actions = True

    BASE = "https://financialmodelingprep.com"

    def __init__(self, api_key: str, fetch_adjusted: bool = True, **kw) -> None:
        if not api_key:
            raise PermanentError("FMP_API_KEY is required for FMPSource")
        super().__init__(**kw)
        self.api_key = api_key
        self.fetch_adjusted = fetch_adjusted

    # -- helpers -----------------------------------------------------------
    def _call(self, path: str, **params) -> object:
        params["apikey"] = self.api_key
        payload = self._get_json(f"{self.BASE}{path}", params=params)
        # FMP returns 200 with an error dict for bad keys / plan limits
        if isinstance(payload, dict) and ("Error Message" in payload or "error" in payload):
            msg = payload.get("Error Message") or payload.get("error")
            if "limit" in str(msg).lower():
                from .base import TransientError
                raise TransientError(f"FMP quota: {msg}")
            raise PermanentError(f"FMP error: {msg}")
        return payload

    @staticmethod
    def _rows_to_frame(payload: object) -> pd.DataFrame:
        """Accept both the stable (list) and legacy ({'historical': [...]}) shapes."""
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("historical", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "date" not in df.columns:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()

    # -- contract ----------------------------------------------------------
    def _fetch_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        raw = self._rows_to_frame(
            self._call("/stable/historical-price-eod/full", symbol=ticker, **{"from": start, "to": end})
        )
        if raw.empty:
            raise NoDataError(f"FMP has no bars for {ticker}")
        out = raw[["open", "high", "low", "close", "volume"]].copy()

        if "adjClose" in raw.columns:           # legacy v3 payload already has it
            out["adj_close"] = raw["adjClose"]
        elif self.fetch_adjusted:
            adj = self._rows_to_frame(
                self._call("/stable/historical-price-eod/dividend-adjusted",
                           symbol=ticker, **{"from": start, "to": end})
            )
            if not adj.empty and "adjClose" in adj.columns:
                out["adj_close"] = adj["adjClose"].reindex(out.index)
        return out

    def _fetch_actions(self, ticker: str, start: str, end: str) -> tuple[pd.Series, pd.Series]:
        div_rows = self._call("/stable/dividends", symbol=ticker)
        spl_rows = self._call("/stable/splits", symbol=ticker)

        div = _series_from(div_rows, value_key="dividend", fallback_key="adjDividend")
        spl = _split_ratio(spl_rows)
        rng = lambda s: s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
        return rng(div), rng(spl)


def _series_from(rows: object, value_key: str, fallback_key: Optional[str] = None) -> pd.Series:
    if isinstance(rows, dict):
        rows = rows.get("historical", [])
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    key = value_key if value_key in df.columns else fallback_key
    if key is None or key not in df.columns or "date" not in df.columns:
        return pd.Series(dtype=float)
    s = pd.Series(df[key].astype(float).values, index=pd.to_datetime(df["date"]))
    return s.sort_index()


def _split_ratio(rows: object) -> pd.Series:
    """FMP gives numerator/denominator (e.g. 10/1). Convert to a single ratio."""
    if isinstance(rows, dict):
        rows = rows.get("historical", [])
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    if not {"numerator", "denominator", "date"}.issubset(df.columns):
        return pd.Series(dtype=float)
    ratio = df["numerator"].astype(float) / df["denominator"].astype(float).replace(0, float("nan"))
    return pd.Series(ratio.values, index=pd.to_datetime(df["date"])).dropna().sort_index()
