"""Corporate-action math: the part of the pipeline where a silent bug costs the most."""
import numpy as np
import pandas as pd
import pytest

from mdlab.adjust import adjust_bars, compare_bases, dividend_factors, split_factors, total_return_index
from mdlab.schema import BAR_COLS


def _bars(closes, dates=None):
    dates = dates or pd.bdate_range("2024-06-03", periods=len(closes))
    df = pd.DataFrame({c: np.nan for c in BAR_COLS}, index=pd.DatetimeIndex(dates, name="date"))
    df["close"] = closes
    df["open"] = df["high"] = df["low"] = df["close"]
    df["volume"] = 1000.0
    df[["dividends", "splits"]] = 0.0
    return df


def test_split_factor_is_one_over_ratio_before_and_one_after():
    idx = pd.to_datetime(["2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11"])
    f = split_factors(idx, pd.Series([10.0], index=pd.to_datetime(["2024-06-10"]), name="splits"))
    assert f.tolist() == pytest.approx([0.1, 0.1, 1.0, 1.0])


def test_split_adjust_removes_the_fake_crash():
    raw = _bars([1200.0, 1210.0, 121.0, 122.0])           # 10:1 on the third bar
    splits = pd.Series([10.0], index=[raw.index[2]])
    adj = adjust_bars(raw, splits, pd.Series(dtype=float), input_basis="raw", target="split")
    rets = adj["close"].pct_change().dropna()
    assert rets.abs().max() < 0.05                        # no -90% "return"
    assert adj["volume"].iloc[0] == pytest.approx(10000)  # pre-split volume scaled up


def test_undo_vendor_split_adjustment_recovers_raw():
    raw = _bars([1200.0, 1210.0, 121.0, 122.0])
    splits = pd.Series([10.0], index=[raw.index[2]])
    vendor_adj = adjust_bars(raw, splits, pd.Series(dtype=float), input_basis="raw", target="split")
    back = adjust_bars(vendor_adj, splits, pd.Series(dtype=float), input_basis="split_adjusted", target="raw")
    pd.testing.assert_series_equal(back["close"], raw["close"], check_names=False)


def test_dividend_factor_matches_crsp_convention():
    close = pd.Series([100.0, 100.0, 98.0, 98.0], index=pd.bdate_range("2024-01-01", periods=4))
    div = pd.Series([2.0], index=[close.index[2]])        # $2 ex on bar 3, prior close 100
    f = dividend_factors(close, div)
    assert f.tolist() == pytest.approx([0.98, 0.98, 1.0, 1.0])


def test_total_return_index_has_no_ex_div_drop():
    close = pd.Series([100.0, 100.0, 98.0, 98.0], index=pd.bdate_range("2024-01-01", periods=4))
    div = pd.Series([2.0], index=[close.index[2]])
    tri = total_return_index(close, div)
    assert tri.pct_change().dropna().abs().max() < 1e-9


def test_dividend_larger_than_price_is_skipped_not_negative():
    close = pd.Series([1.0, 1.0, 1.0], index=pd.bdate_range("2024-01-01", periods=3))
    div = pd.Series([5.0], index=[close.index[1]])
    f = dividend_factors(close, div)
    assert (f == 1.0).all()


def test_compare_bases_has_three_columns_and_agrees_at_the_end():
    raw = _bars([1200.0, 1210.0, 121.0, 122.0])
    splits = pd.Series([10.0], index=[raw.index[2]])
    cb = compare_bases(raw, splits, pd.Series(dtype=float), input_basis="raw")
    assert list(cb.columns) == ["raw", "split", "total_return"]
    assert cb.iloc[-1].nunique() == 1                     # last bar identical on all bases
