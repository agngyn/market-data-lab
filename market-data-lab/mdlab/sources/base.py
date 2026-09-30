"""
Vendor-agnostic data source contract.

Every vendor is a subclass of :class:`DataSource` implementing two methods:

* ``_fetch_ohlcv(ticker, start, end)`` -> raw vendor DataFrame
* ``_fetch_actions(ticker, start, end)`` -> (dividends Series, splits Series)  [optional]

The base class owns everything that is the same for every vendor and that is
easy to get subtly wrong:

* **Rate limiting** — a token bucket so we never trip vendor quotas
  (Polygon free tier = 5 req/min, FMP free = 250 req/day).
* **Retry with exponential backoff + jitter** (tenacity) on transient network
  errors and HTTP 429/5xx. Client errors (4xx other than 429) are NOT retried —
  retrying a bad API key 5 times just burns quota.
* **Read-through Parquet cache** keyed on (source, ticker, start, end).
* **Schema normalisation + pandera validation** so callers can trust the shape.
* **Fetch telemetry** (latency, cache hits, errors) surfaced in the DQ report.
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import requests
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from ..cache import ParquetCache
from ..schema import empty_bars, standardize, validate_bars

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class DataSourceError(RuntimeError):
    """Base class for vendor errors."""


class TransientError(DataSourceError):
    """Retryable: timeouts, connection resets, HTTP 429 / 5xx."""


class PermanentError(DataSourceError):
    """Not retryable: bad key (401/403), unknown symbol (404), malformed request."""


class NoDataError(DataSourceError):
    """Vendor responded fine but has no bars for this ticker/window (delisted, wrong symbol)."""


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return False


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log.warning(
        "retry %d/%d after %.1fs — %s: %s",
        state.attempt_number,
        MAX_ATTEMPTS,
        state.next_action.sleep if state.next_action else 0.0,
        type(exc).__name__,
        exc,
    )


MAX_ATTEMPTS = 5


# --------------------------------------------------------------------------
# Rate limiter
# --------------------------------------------------------------------------
class RateLimiter:
    """Thread-safe token bucket: at most ``rate`` calls per ``per`` seconds.

    Blocking (sleeps) rather than raising — in a batch job we would rather wait
    than fail. ``rate=None`` disables limiting.
    """

    def __init__(self, rate: Optional[int], per: float = 60.0) -> None:
        self.rate, self.per = rate, per
        self._tokens = float(rate or 0)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a token is available; return seconds slept."""
        if not self.rate:
            return 0.0
        slept = 0.0
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.rate, self._tokens + (now - self._last) * self.rate / self.per)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return slept
                wait = (1 - self._tokens) * self.per / self.rate
                time.sleep(wait)
                slept += wait


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------
@dataclass
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    errors: int = 0
    no_data: int = 0
    seconds: float = 0.0
    rate_limit_sleep: float = 0.0
    error_log: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "errors": self.errors,
            "no_data": self.no_data,
            "seconds": round(self.seconds, 2),
            "rate_limit_sleep_s": round(self.rate_limit_sleep, 2),
        }


# --------------------------------------------------------------------------
# Base class
# --------------------------------------------------------------------------
class DataSource(ABC):
    """Abstract vendor adapter. Subclasses set the class attributes and
    implement ``_fetch_ohlcv`` (+ optionally ``_fetch_actions``).

    Class attributes
    ----------------
    name : str
        Short vendor id used in cache paths, panel keys and reports.
    close_basis : {"raw", "split_adjusted"}
        What the vendor's *unadjusted* close really is (see ``schema.py``).
    adj_close_basis : {"split", "total_return", None}
        What the vendor's ``adj_close`` adjusts for.
    actions_basis : {"raw", "split_adjusted"}
        Whether dividend amounts are quoted per then-outstanding share (raw, the
        way they were declared) or restated in today's post-split shares (Yahoo).
    rate_limit : (calls, seconds) | None
    """

    name: str = "abstract"
    close_basis: str = "raw"
    adj_close_basis: Optional[str] = None
    actions_basis: str = "raw"
    rate_limit: Optional[tuple[int, float]] = None
    provides_actions: bool = False

    def __init__(self, cache: Optional[ParquetCache] = None, timeout: float = 30.0) -> None:
        self.cache = cache
        self.timeout = timeout
        self.stats = FetchStats()
        self._limiter = RateLimiter(*(self.rate_limit or (None, 60.0)))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "market-data-lab/0.1 (+https://github.com/agngyn/market-data-lab)"})

    # -- public API --------------------------------------------------------
    def get_ohlcv(self, ticker: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
        """Return canonical bars for ``ticker`` in ``[start, end]``.

        Never raises for "no data": returns an empty canonical frame instead so
        a single delisted symbol cannot kill a 100-ticker batch. Vendor/auth
        errors *do* raise ``PermanentError`` after logging — those need a human.
        """
        start, end = _iso(start), _iso(end)
        if use_cache and self.cache is not None:
            cached = self.cache.get(self.name, ticker, start, end)
            if cached is not None:
                self.stats.cache_hits += 1
                return cached

        t0 = time.perf_counter()
        try:
            raw = self._fetch_with_retry(ticker, start, end)
            df = standardize(raw) if raw is not None and len(raw) else empty_bars()
            # clip to the requested window: some vendors return one bar either side
            df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
            if df.empty:
                self.stats.no_data += 1
                log.info("%s: no data for %s in %s..%s", self.name, ticker, start, end)
            else:
                validate_bars(df, context=f"{self.name}:{ticker}")
        except NoDataError as exc:
            self.stats.no_data += 1
            log.info("%s: %s", self.name, exc)
            df = empty_bars()
        except Exception as exc:
            self.stats.errors += 1
            self.stats.error_log.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
            log.error("%s: failed %s (%s: %s)", self.name, ticker, type(exc).__name__, " ".join(str(exc).split())[:160])
            raise
        finally:
            self.stats.seconds += time.perf_counter() - t0

        if self.cache is not None:
            self.cache.put(self.name, ticker, start, end, df, close_basis=self.close_basis)
        return df

    def get_actions(self, ticker: str, start: str, end: str) -> tuple[pd.Series, pd.Series]:
        """(dividends, splits) Series indexed by ex-date. Empty if unsupported."""
        if not self.provides_actions:
            return _empty_series("dividends"), _empty_series("splits")
        try:
            div, spl = self._fetch_actions(ticker, _iso(start), _iso(end))
        except Exception as exc:
            log.warning("%s: actions fetch failed for %s: %s", self.name, ticker, exc)
            return _empty_series("dividends"), _empty_series("splits")
        return _clean_series(div, "dividends"), _clean_series(spl, "splits")

    # -- hooks for subclasses ---------------------------------------------
    @abstractmethod
    def _fetch_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:  # pragma: no cover
        """Return a raw vendor frame indexed by date with (some of) the canonical columns."""

    def _fetch_actions(self, ticker: str, start: str, end: str) -> tuple[pd.Series, pd.Series]:
        raise NotImplementedError

    # -- plumbing ----------------------------------------------------------
    @retry(
        retry=retry_if_exception(_is_transient),
        wait=wait_random_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        before_sleep=_log_retry,
        reraise=True,
    )
    def _fetch_with_retry(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        self.stats.rate_limit_sleep += self._limiter.acquire()
        self.stats.requests += 1
        return self._fetch_ohlcv(ticker, start, end)

    def _get_json(self, url: str, params: Optional[dict] = None) -> object:
        """HTTP GET with status-code -> exception mapping shared by REST vendors."""
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TransientError(f"network error: {exc}") from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"HTTP {resp.status_code} from {self.name}")
        if resp.status_code in (401, 403):
            raise PermanentError(f"HTTP {resp.status_code} from {self.name}: check API key / plan")
        if resp.status_code == 404:
            raise NoDataError(f"{self.name} returned 404 for {url}")
        if resp.status_code != 200:
            raise PermanentError(f"HTTP {resp.status_code} from {self.name}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise PermanentError(f"{self.name} returned non-JSON body") from exc

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} close_basis={self.close_basis!r}>"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _iso(d) -> str:
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def _empty_series(name: str) -> pd.Series:
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="date"), name=name)


def _clean_series(s: pd.Series, name: str) -> pd.Series:
    if s is None or len(s) == 0:
        return _empty_series(name)
    s = s.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(s.index))
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    s.index = idx.normalize().rename("date")
    s = s[s != 0].astype("float64")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.name = name
    return s
