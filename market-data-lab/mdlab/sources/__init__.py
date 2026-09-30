"""Vendor adapters. ``make_sources`` builds the live trio from ``Settings``."""
from __future__ import annotations

import logging
from typing import Optional

from ..cache import ParquetCache
from ..config import Settings
from .base import DataSource, DataSourceError, NoDataError, PermanentError, TransientError
from .fmp import FMPSource
from .polygon import PolygonSource
from .synthetic import DefectProfile, SyntheticSource, TrueMarket, make_synthetic_trio
from .yahoo import YahooSource

log = logging.getLogger(__name__)

__all__ = [
    "DataSource", "DataSourceError", "NoDataError", "PermanentError", "TransientError",
    "YahooSource", "FMPSource", "PolygonSource",
    "SyntheticSource", "DefectProfile", "TrueMarket", "make_synthetic_trio",
    "make_sources",
]


def make_sources(settings: Settings, cache: Optional[ParquetCache] = None,
                 vendors: Optional[list[str]] = None, fetch_adjusted: bool = True) -> list[DataSource]:
    """Instantiate live vendors. Vendors without credentials are skipped with a warning
    rather than raising, so a Yahoo-only run works out of the box."""
    cache = cache if cache is not None else ParquetCache(settings.cache_dir)
    wanted = vendors or ["yahoo", "fmp", "polygon"]
    out: list[DataSource] = []
    for v in wanted:
        if v == "yahoo":
            out.append(YahooSource(cache=cache, timeout=settings.request_timeout))
        elif v == "fmp":
            if settings.fmp_api_key:
                out.append(FMPSource(settings.fmp_api_key, fetch_adjusted=fetch_adjusted,
                                     cache=cache, timeout=settings.request_timeout))
            else:
                log.warning("FMP_API_KEY not set — skipping FMP")
        elif v == "polygon":
            if settings.polygon_api_key:
                out.append(PolygonSource(settings.polygon_api_key, fetch_adjusted=fetch_adjusted,
                                         cache=cache, timeout=settings.request_timeout))
            else:
                log.warning("POLYGON_API_KEY not set — skipping Polygon")
        else:
            raise ValueError(f"unknown vendor {v!r}")
    return out
