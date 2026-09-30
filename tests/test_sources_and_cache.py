"""Vendor adapters (parsed against documented payload shapes) + cache + calendar + schema."""
import json
import time

import pandas as pd
import pandera.pandas as pa
import pytest

from mdlab.cache import ParquetCache
from mdlab.calendar import is_session, nyse_sessions
from mdlab.schema import BAR_COLS, standardize, validate_bars
from mdlab.sources.base import DataSource, NoDataError, PermanentError, RateLimiter, TransientError
from mdlab.sources.fmp import FMPSource
from mdlab.sources.polygon import PolygonSource


# ---------------------------------------------------------------- calendar
def test_nyse_calendar_skips_good_friday_and_july_4():
    s = nyse_sessions("2024-03-27", "2024-04-02").strftime("%Y-%m-%d").tolist()
    assert "2024-03-29" not in s and "2024-03-28" in s        # Good Friday closed
    assert not is_session("2024-07-04") and is_session("2024-07-05")
    assert not is_session("2025-01-09")                        # Carter national day of mourning


def test_juneteenth_only_from_2022():
    assert is_session("2021-06-18")                            # Friday before, still open in 2021
    assert not is_session("2023-06-19")


# ---------------------------------------------------------------- schema
def test_standardize_normalises_index_and_columns():
    raw = pd.DataFrame({"Open": [1.0], "Close": [1.5], "High": [2.0], "Low": [0.5], "Volume": [10]},
                       index=pd.to_datetime(["2024-01-02 09:30"]).tz_localize("America/New_York"))
    out = standardize(raw)
    assert list(out.columns) == BAR_COLS
    assert out.index.tz is None and out.index[0] == pd.Timestamp("2024-01-02")


def test_validate_bars_rejects_high_below_low():
    df = standardize(pd.DataFrame({"open": [1.0], "high": [0.5], "low": [1.0], "close": [0.8], "volume": [1]},
                                  index=pd.to_datetime(["2024-01-02"])))
    with pytest.raises(pa.errors.SchemaErrors):
        validate_bars(df)


# ---------------------------------------------------------------- cache
def test_cache_roundtrip_and_stats(tmp_path):
    c = ParquetCache(tmp_path)
    df = standardize(pd.DataFrame({"close": [1.0, 2.0], "volume": [1, 2]}, index=pd.to_datetime(["2024-01-02", "2024-01-03"])))
    assert c.get("x", "AAPL", "2024-01-01", "2024-01-31") is None
    c.put("x", "AAPL", "2024-01-01", "2024-01-31", df, close_basis="raw")
    back = c.get("x", "AAPL", "2024-01-01", "2024-01-31")
    pd.testing.assert_frame_equal(back, df)
    assert c.stats.hits == 1 and c.stats.misses == 1
    assert c.meta("x", "AAPL", "2024-01-01", "2024-01-31")["close_basis"] == "raw"
    assert len(c.inventory()) == 1
    assert c.invalidate("x") == 1


def test_cache_ttl_expiry(tmp_path):
    c = ParquetCache(tmp_path, ttl_seconds=0.01)
    df = standardize(pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2024-01-02"])))
    c.put("x", "T", "a", "b", df)
    time.sleep(0.05)
    assert c.get("x", "T", "a", "b") is None


# ---------------------------------------------------------------- base class behaviour
class _Flaky(DataSource):
    name = "flaky"

    def __init__(self, fail_times):
        super().__init__()
        self.fail_times, self.calls = fail_times, 0

    def _fetch_ohlcv(self, ticker, start, end):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TransientError("boom")
        return pd.DataFrame({"close": [1.0, 1.1], "open": [1, 1], "high": [2, 2], "low": [0.5, 0.5], "volume": [1, 1]},
                            index=pd.to_datetime(["2024-01-02", "2024-01-03"]))


def test_transient_errors_are_retried(monkeypatch):
    # make tenacity's sleep instant so the test is fast
    import mdlab.sources.base as base
    monkeypatch.setattr(base.DataSource._fetch_with_retry.retry, "wait", lambda *a, **k: 0)
    src = _Flaky(fail_times=2)
    df = src.get_ohlcv("T", "2024-01-01", "2024-01-31")
    assert src.calls == 3 and len(df) == 2 and src.stats.requests == 3


def test_permanent_errors_are_not_retried():
    class Bad(DataSource):
        name = "bad"

        def _fetch_ohlcv(self, *a):
            raise PermanentError("401")

    with pytest.raises(PermanentError):
        Bad().get_ohlcv("T", "2024-01-01", "2024-01-31")


def test_no_data_returns_empty_frame_not_exception():
    class Empty(DataSource):
        name = "empty"

        def _fetch_ohlcv(self, *a):
            raise NoDataError("gone")

    df = Empty().get_ohlcv("T", "2024-01-01", "2024-01-31")
    assert df.empty and list(df.columns) == BAR_COLS


def test_rate_limiter_blocks():
    rl = RateLimiter(rate=2, per=0.2)
    t0 = time.perf_counter()
    for _ in range(4):
        rl.acquire()
    assert time.perf_counter() - t0 >= 0.15


# ---------------------------------------------------------------- vendor payload parsing (offline)
POLYGON_AGGS = {
    "status": "OK", "resultsCount": 2,
    "results": [
        {"t": 1717992000000, "o": 1200.5, "h": 1215.0, "l": 1190.0, "c": 1208.88, "v": 41000000, "vw": 1201.1, "n": 1},
        {"t": 1718078400000, "o": 120.0, "h": 123.0, "l": 118.5, "c": 121.79, "v": 313000000, "vw": 121.0, "n": 1},
    ],
}
FMP_STABLE = [
    {"symbol": "NVDA", "date": "2024-06-07", "open": 1200.5, "high": 1215.0, "low": 1190.0, "close": 1208.88, "volume": 41000000},
    {"symbol": "NVDA", "date": "2024-06-10", "open": 120.0, "high": 123.0, "low": 118.5, "close": 121.79, "volume": 313000000},
]
FMP_LEGACY = {"symbol": "NVDA", "historical": [dict(r, adjClose=r["close"] * 0.99) for r in FMP_STABLE]}


def test_polygon_parses_daily_aggs_and_adjusted_column(monkeypatch):
    src = PolygonSource("k", fetch_adjusted=True)
    monkeypatch.setattr(src, "_call", lambda url, **p: POLYGON_AGGS)
    df = src.get_ohlcv("NVDA", "2024-06-07", "2024-06-12")
    # t is 04:00 UTC == midnight New York; must land on the ET calendar date, not the UTC one
    assert df.index.tolist() == [pd.Timestamp("2024-06-10"), pd.Timestamp("2024-06-11")]
    assert set(["open", "high", "low", "close", "adj_close", "volume"]) <= set(df.columns)
    assert src.close_basis == "raw" and src.adj_close_basis == "split"


def test_fmp_parses_stable_and_legacy_shapes(monkeypatch):
    src = FMPSource("k", fetch_adjusted=False)
    monkeypatch.setattr(src, "_call", lambda path, **p: FMP_STABLE)
    df = src.get_ohlcv("NVDA", "2024-06-01", "2024-06-30")
    assert df["close"].tolist() == pytest.approx([1208.88, 121.79])
    monkeypatch.setattr(src, "_call", lambda path, **p: FMP_LEGACY)
    df2 = src.get_ohlcv("NVDA", "2024-06-01", "2024-06-30", use_cache=False)
    assert df2["adj_close"].notna().all()


def test_fmp_split_ratio_parsing():
    from mdlab.sources.fmp import _split_ratio
    s = _split_ratio([{"date": "2024-06-10", "numerator": 10, "denominator": 1}, {"date": "2021-07-20", "numerator": 4, "denominator": 1}])
    assert s.loc["2024-06-10"] == 10.0 and s.index.is_monotonic_increasing


def test_vendor_error_bodies_map_to_exceptions(monkeypatch):
    src = FMPSource("k")

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"Error Message": "Limit Reach . Please upgrade your plan"}

    monkeypatch.setattr(src._session, "get", lambda *a, **k: R())
    with pytest.raises(TransientError):
        src._call("/x")
