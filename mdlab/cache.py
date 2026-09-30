"""
Local Parquet response cache.

Why Parquet
-----------
* Columnar + compressed: a 10-year daily OHLCV series is ~40 KB.
* Preserves dtypes and the DatetimeIndex exactly (CSV loses both).
* Readable by pandas, polars, DuckDB, Spark — the cache doubles as a mini data lake.

Key design
----------
Cache entries are keyed on ``(source, ticker, start, end)``. The date range is
part of the key on purpose: vendors *restate* history (late corrections,
split back-adjustments), so a request for a different window must never be
served from a stale superset. A ``.meta.json`` sidecar records when the entry
was written and by which vendor so the DQ report can flag stale caches.

Layout::

    cache/
      yahoo/
        AAPL/
          2020-01-01_2024-12-31.parquet
          2020-01-01_2024-12-31.meta.json
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(s: str) -> str:
    """Filesystem-safe token (tickers like ``BRK.B`` or ``^GSPC`` need this)."""
    return _SAFE.sub("_", str(s))


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    bytes_written: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": round(self.hit_rate, 4),
            "mb_written": round(self.bytes_written / 1e6, 3),
        }


class ParquetCache:
    """Read-through cache for vendor bar data.

    Parameters
    ----------
    root : Path
        Cache directory root.
    ttl_seconds : float | None
        Entries older than this are treated as misses (``None`` = never expire).
        Useful for a nightly refresh of the most recent window.
    """

    def __init__(self, root: Path | str = "cache", ttl_seconds: Optional[float] = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self.stats = CacheStats()

    # -- key handling ------------------------------------------------------
    def path_for(self, source: str, ticker: str, start: str, end: str) -> Path:
        return self.root / _safe(source) / _safe(ticker) / f"{_safe(start)}_{_safe(end)}.parquet"

    @staticmethod
    def _meta_path(p: Path) -> Path:
        return p.with_suffix(".meta.json")

    # -- read / write ------------------------------------------------------
    def get(self, source: str, ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
        """Return the cached frame, or ``None`` on a miss / expired entry."""
        p = self.path_for(source, ticker, start, end)
        if not p.exists():
            self.stats.misses += 1
            return None
        if self.ttl_seconds is not None:
            age = time.time() - p.stat().st_mtime
            if age > self.ttl_seconds:
                log.debug("cache expired (%.0fs old): %s", age, p)
                self.stats.misses += 1
                return None
        try:
            df = pd.read_parquet(p)
        except Exception as exc:  # corrupted file -> treat as miss, remove
            log.warning("unreadable cache entry %s (%s); deleting", p, exc)
            p.unlink(missing_ok=True)
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return df

    def put(self, source: str, ticker: str, start: str, end: str, df: pd.DataFrame, **meta) -> Path:
        """Write ``df`` atomically (tmp file + rename) with a JSON metadata sidecar."""
        p = self.path_for(source, ticker, start, end)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=True)
        tmp.replace(p)  # atomic on POSIX; a crash mid-write never leaves a half file
        self._meta_path(p).write_text(
            json.dumps(
                {
                    "source": source,
                    "ticker": ticker,
                    "start": start,
                    "end": end,
                    "rows": int(len(df)),
                    "written_at": pd.Timestamp.now('UTC').isoformat(),
                    **meta,
                },
                indent=2,
                default=str,
            )
        )
        self.stats.writes += 1
        self.stats.bytes_written += p.stat().st_size
        return p

    def meta(self, source: str, ticker: str, start: str, end: str) -> Optional[dict]:
        mp = self._meta_path(self.path_for(source, ticker, start, end))
        return json.loads(mp.read_text()) if mp.exists() else None

    # -- housekeeping ------------------------------------------------------
    def invalidate(self, source: Optional[str] = None, ticker: Optional[str] = None) -> int:
        """Delete entries; scoped to a source and/or ticker. Returns files removed."""
        target = self.root
        if source:
            target = target / _safe(source)
        if ticker:
            target = target / _safe(ticker)
        if not target.exists():
            return 0
        n = sum(1 for _ in target.rglob("*.parquet"))
        shutil.rmtree(target)
        return n

    def inventory(self) -> pd.DataFrame:
        """Table of every cached entry — handy for a 'what do I have' cell."""
        rows = []
        for mp in self.root.rglob("*.meta.json"):
            try:
                rows.append(json.loads(mp.read_text()))
            except Exception:
                continue
        cols = ["source", "ticker", "start", "end", "rows", "written_at"]
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
