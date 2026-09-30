"""
Ticker universe and reference corporate actions.

Three groups:

* ``CORE``   — ~60 liquid US large caps across sectors. Boring on purpose: if
               three vendors disagree on AAPL we have a pipeline bug, not a
               market-structure curiosity.
* ``SPLITS`` — names with recent, large splits. These are the acid test for
               adjustment logic (a 10:1 unadjusted split is a -90% "return").
* ``PROBES`` — symbols that *should* fail: renamed (FB -> META), acquired
               (TWTR, ATVI), delisted. They demonstrate survivorship bias
               concretely: a universe built from today's constituents has none
               of these, and a backtest over it never held a loser that vanished.

``REFERENCE_SPLITS`` is used by the synthetic vendors and as an independent
sanity check against vendor-supplied split tables.
"""
from __future__ import annotations

import pandas as pd

CORE = [
    # tech / comms
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "CSCO", "INTC", "AMD", "QCOM", "TXN",
    "NFLX", "DIS", "CMCSA", "TMUS", "VZ",
    # financials
    "JPM", "BAC", "WFC", "GS", "MS", "BLK", "V", "MA", "AXP", "BRK-B",
    # health
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "TMO", "ABT", "AMGN",
    # consumer
    "PG", "KO", "PEP", "WMT", "COST", "HD", "MCD", "NKE", "SBUX", "TGT",
    # industrials / energy / materials / utilities / real estate
    "CAT", "HON", "UNP", "BA", "GE", "XOM", "CVX", "COP", "LIN", "NEE", "DUK", "AMT", "PLD",
    # small/mid + high-volume names
    "TSLA", "UBER", "PLTR", "F", "GM",
]

SPLITS = ["NVDA", "AVGO", "WMT", "CMG", "SMCI", "LRCX", "GOOGL", "AMZN", "TSLA"]

PROBES = ["FB", "TWTR", "ATVI"]

# ticker -> [(date, ratio)] ; ratio 10.0 == 10-for-1. Sources: company 8-Ks.
REFERENCE_SPLITS: dict[str, list[tuple[str, float]]] = {
    "NVDA": [("2021-07-20", 4.0), ("2024-06-10", 10.0)],
    "AVGO": [("2024-07-15", 10.0)],
    "WMT": [("2024-02-26", 3.0)],
    "CMG": [("2024-06-26", 50.0)],
    "SMCI": [("2024-10-01", 10.0)],
    "LRCX": [("2024-10-03", 10.0)],
    "GOOGL": [("2022-07-18", 20.0)],
    "AMZN": [("2022-06-06", 20.0)],
    "TSLA": [("2020-08-31", 5.0), ("2022-08-25", 3.0)],
    "AAPL": [("2020-08-31", 4.0)],
}

# ticker renames / delistings — the EDGAR CIK map catches these live; this table
# is the offline fallback and the expected answer in tests.
KNOWN_TICKER_CHANGES: dict[str, dict] = {
    "FB": {"new_ticker": "META", "effective": "2022-06-09", "reason": "rename (Meta Platforms)"},
    "TWTR": {"new_ticker": None, "effective": "2022-10-28", "reason": "taken private (X Corp)"},
    "ATVI": {"new_ticker": None, "effective": "2023-10-13", "reason": "acquired by Microsoft"},
}


def default_universe(n_core: int | None = None, include_probes: bool = True) -> list[str]:
    """Deduplicated ticker list, order preserved."""
    core = CORE if n_core is None else CORE[:n_core]
    seq = list(core) + SPLITS + (PROBES if include_probes else [])
    seen: set[str] = set()
    return [t for t in seq if not (t in seen or seen.add(t))]


def reference_splits(ticker: str) -> pd.Series:
    rows = REFERENCE_SPLITS.get(ticker, [])
    if not rows:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="date"), name="splits")
    s = pd.Series([r for _, r in rows], index=pd.to_datetime([d for d, _ in rows]), name="splits")
    return s.sort_index()
