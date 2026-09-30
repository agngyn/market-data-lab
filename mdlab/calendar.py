"""
NYSE trading calendar.

Why we need our own
-------------------
"Missing bar" is only meaningful relative to an *expected* set of sessions. A
naive business-day calendar over-counts (it expects bars on Good Friday,
Thanksgiving, etc.), which would flood the report with false positives.
``pandas_market_calendars`` solves this but is a heavy dependency; the NYSE rule
set is small enough to encode directly with ``pandas.tseries.holiday``.

Coverage: regular NYSE holidays (with weekend observance rules), Juneteenth from
2022 onwards, and the handful of unscheduled full-day closures since 2000.
Half-days are still trading sessions and are *not* excluded.
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)
from pandas.tseries.offsets import CustomBusinessDay


class NYSEHolidayCalendar(AbstractHolidayCalendar):
    """Regular full-day NYSE closures."""

    rules = [
        # New Year's Day: if it falls on Saturday the NYSE does NOT observe on
        # Friday (would be in the prior year); Sunday -> Monday.
        Holiday("New Year's Day", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


# Unscheduled closures (national days of mourning, disasters). Extend as needed.
AD_HOC_CLOSURES = pd.to_datetime(
    [
        "2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14",  # 9/11
        "2004-06-11",  # Reagan funeral
        "2007-01-02",  # Ford funeral
        "2012-10-29", "2012-10-30",  # Hurricane Sandy
        "2018-12-05",  # G.H.W. Bush funeral
        "2025-01-09",  # Carter funeral
    ]
)


@lru_cache(maxsize=8)
def _holidays(start: str, end: str) -> pd.DatetimeIndex:
    regular = NYSEHolidayCalendar().holidays(pd.Timestamp(start), pd.Timestamp(end))
    return regular.union(AD_HOC_CLOSURES).sort_values()


def nyse_sessions(start, end) -> pd.DatetimeIndex:
    """Return every expected NYSE trading session in ``[start, end]`` (inclusive).

    Parameters are anything ``pd.Timestamp`` accepts. The result is tz-naive
    and normalized to midnight so it joins cleanly against vendor bars.

    Example
    -------
    >>> nyse_sessions("2024-06-28", "2024-07-08").strftime("%Y-%m-%d").tolist()
    ['2024-06-28', '2024-07-01', '2024-07-02', '2024-07-03', '2024-07-05', '2024-07-08']
    """
    start_ts, end_ts = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    # widen the holiday window by a year each side so observance shifts are caught
    hol = _holidays(str(start_ts.year - 1), str(end_ts.year + 1))
    bday = CustomBusinessDay(holidays=hol)
    return pd.date_range(start_ts, end_ts, freq=bday)


def is_session(ts) -> bool:
    """True if the given date is an NYSE trading session."""
    ts = pd.Timestamp(ts).normalize()
    return len(nyse_sessions(ts, ts)) == 1
