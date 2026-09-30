"""
The one canonical bar schema every vendor is normalised into.

Every ``DataSource.get_ohlcv`` returns a DataFrame with:

    index   : ``date``  — tz-naive DatetimeIndex, normalised to midnight, sorted, unique
    columns : open, high, low, close, adj_close, volume, dividends, splits

Semantics that vendors do NOT agree on, and that we therefore record explicitly
on the source object (see ``DataSource.close_basis``):

* ``close``      — the vendor's "unadjusted" close. Yahoo's is actually
                   *split-adjusted* (Yahoo back-adjusts history on every split);
                   Polygon ``adjusted=false`` and FMP ``close`` are truly raw.
* ``adj_close``  — the vendor's own adjusted close. Yahoo & FMP adjust for splits
                   AND dividends (total-return basis); Polygon ``adjusted=true``
                   adjusts for splits only.
* ``dividends``  — cash dividend per share with ex-date on that bar (0 if none)
* ``splits``     — split ratio effective that bar (e.g. 10.0 for a 10:1; 0 if none)

``validate_bars`` is an assertion-based gate using pandera: structural problems
(negative prices, high < low, duplicate dates) raise immediately at ingestion
instead of silently corrupting a backtest three modules downstream.
"""
from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd
import pandera.pandas as pa

log = logging.getLogger(__name__)

PRICE_COLS = ["open", "high", "low", "close", "adj_close"]
BAR_COLS = PRICE_COLS + ["volume", "dividends", "splits"]


def empty_bars() -> pd.DataFrame:
    """A correctly-typed empty frame (used when a vendor has no data)."""
    df = pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLS})
    df.index = pd.DatetimeIndex([], name="date")
    return df


# --- pandera schema -------------------------------------------------------
# ``nullable=True`` on prices: a vendor may legitimately omit a field (e.g. FMP
# without an adjusted series). The *quality checks* decide whether a null is a
# problem; the schema only enforces that what IS present is physically valid.
BARS_SCHEMA = pa.DataFrameSchema(
    columns={
        "open": pa.Column(float, pa.Check.gt(0), nullable=True),
        "high": pa.Column(float, pa.Check.gt(0), nullable=True),
        "low": pa.Column(float, pa.Check.gt(0), nullable=True),
        "close": pa.Column(float, pa.Check.gt(0), nullable=True),
        "adj_close": pa.Column(float, pa.Check.gt(0), nullable=True),
        "volume": pa.Column(float, pa.Check.ge(0), nullable=True),
        "dividends": pa.Column(float, pa.Check.ge(0), nullable=True),
        "splits": pa.Column(float, pa.Check.ge(0), nullable=True),
    },
    index=pa.Index(pa.DateTime, unique=True, name="date"),
    checks=[
        # high must be the max of the bar and low the min — wide-form checks
        pa.Check(lambda df: (df["high"] >= df["low"]) | df[["high", "low"]].isna().any(axis=1),
                 name="high_ge_low", error="high < low"),
        pa.Check(lambda df: ((df["close"] <= df["high"] * 1.0001) & (df["close"] >= df["low"] * 0.9999))
                 | df[["close", "high", "low"]].isna().any(axis=1),
                 name="close_within_range", error="close outside [low, high]"),
    ],
    strict=False,
    coerce=True,
)


def standardize(df: pd.DataFrame, rename: dict[str, str] | None = None) -> pd.DataFrame:
    """Coerce an arbitrary vendor frame into the canonical bar layout.

    * renames columns via ``rename``
    * guarantees all ``BAR_COLS`` exist (missing -> NaN, actions -> 0)
    * converts the index to a tz-naive, normalised, sorted, de-duplicated ``date`` index
    """
    out = df.rename(columns=rename or {}).copy()
    # lower-case whatever the vendor sent so downstream selection is uniform
    out.columns = [str(c).lower().replace(" ", "_") for c in out.columns]

    for col in BAR_COLS:
        if col not in out.columns:
            out[col] = 0.0 if col in ("dividends", "splits") else np.nan
    out = out[BAR_COLS].astype("float64")

    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    out.index = idx.normalize().rename("date")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def validate_bars(df: pd.DataFrame, *, context: str = "", lazy: bool = True) -> pd.DataFrame:
    """Run the pandera schema. Raises ``pandera.errors.SchemaErrors`` on failure.

    ``lazy=True`` collects *all* violations before raising so a single call
    tells you everything wrong with a vendor payload.
    """
    if df.empty:
        return df
    try:
        return BARS_SCHEMA.validate(df, lazy=lazy)
    except pa.errors.SchemaErrors as exc:  # pragma: no cover - exercised in tests
        n = len(exc.failure_cases)
        log.error("schema validation failed for %s: %d failure cases", context or "<bars>", n)
        raise


def coverage_summary(frames: Iterable[tuple[str, str, pd.DataFrame]]) -> pd.DataFrame:
    """Quick (source, ticker) -> rows / first / last table for sanity checks."""
    rows = []
    for source, ticker, df in frames:
        rows.append(
            {
                "source": source,
                "ticker": ticker,
                "rows": len(df),
                "first": df.index.min() if len(df) else pd.NaT,
                "last": df.index.max() if len(df) else pd.NaT,
            }
        )
    return pd.DataFrame(rows)
