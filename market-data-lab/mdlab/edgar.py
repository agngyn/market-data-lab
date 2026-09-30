"""
SEC EDGAR ticker -> CIK mapping, used to detect ticker changes and delistings.

Why EDGAR
---------
A ticker is a *label*; the CIK (Central Index Key) is the *entity*. Vendors key
history by ticker, so when Facebook became META (2022-06-09) the symbol ``FB``
stopped having a future and ``META`` acquired a past. A universe built from
tickers alone silently loses the pre-rename history — or, if you only look at
today's symbols, loses every company that vanished (survivorship bias).

SEC publishes the current map for free:

* https://www.sec.gov/files/company_tickers.json      -> {cik_str, ticker, title}
* https://data.sec.gov/submissions/CIK##########.json  -> formerNames, tickers, exchanges

Rules: a descriptive ``User-Agent`` with contact email is **mandatory** (403
otherwise) and the fair-use limit is 10 requests/second.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from .universe import KNOWN_TICKER_CHANGES

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


class EdgarClient:
    def __init__(self, user_agent: str, cache_dir: Path | str = "cache/edgar", ttl_days: int = 7, timeout: float = 30.0) -> None:
        if "@" not in user_agent:
            log.warning("EDGAR user agent should include a contact email: %r", user_agent)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_days * 86400
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._last_call = 0.0

    def _throttle(self) -> None:
        # 10 req/s fair-use -> at least 0.1s between calls
        wait = 0.11 - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _cached_json(self, key: str, url: str) -> Optional[dict]:
        p = self.cache_dir / f"{key}.json"
        if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
            return json.loads(p.read_text())
        self._throttle()
        try:
            r = self._s.get(url, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.warning("EDGAR fetch failed for %s: %s", url, exc)
            if p.exists():  # stale cache beats nothing
                return json.loads(p.read_text())
            return None
        p.write_text(json.dumps(data))
        return data

    # -- public ------------------------------------------------------------
    def ticker_map(self) -> Optional[dict[str, dict]]:
        """{TICKER: {"cik": int, "name": str}} for every currently listed SEC filer, or None offline."""
        data = self._cached_json("company_tickers", TICKERS_URL)
        if not data:
            return None
        out = {}
        for row in data.values():
            out[str(row["ticker"]).upper()] = {"cik": int(row["cik_str"]), "name": row["title"]}
        return out

    def submissions(self, cik: int) -> Optional[dict]:
        return self._cached_json(f"cik_{cik:010d}", SUBMISSIONS_URL.format(cik=cik))

    def resolve(self, ticker: str, tmap: Optional[dict] = None) -> dict:
        """Best-effort explanation for a ticker: current CIK, or why it is gone."""
        tmap = tmap if tmap is not None else self.ticker_map()
        key = ticker.upper().replace("-", ".")
        if tmap and (key in tmap or ticker.upper() in tmap):
            hit = tmap.get(key) or tmap[ticker.upper()]
            sub = self.submissions(hit["cik"]) or {}
            return {
                "ticker": ticker, "status": "listed", "cik": hit["cik"], "name": hit["name"],
                "all_tickers": sub.get("tickers", []), "former_names": [f.get("name") for f in sub.get("formerNames", [])][:5],
            }
        known = KNOWN_TICKER_CHANGES.get(ticker.upper())
        if known:
            return {"ticker": ticker, "status": "changed", **known}
        return {"ticker": ticker, "status": "not_found" if tmap else "unknown_offline"}


def ticker_change_report(tickers: list[str], client: Optional[EdgarClient]) -> pd.DataFrame:
    """One row per ticker with EDGAR resolution (falls back to the curated table offline)."""
    rows = []
    tmap = client.ticker_map() if client else None
    for t in tickers:
        if client is not None:
            rows.append(client.resolve(t, tmap))
        else:
            known = KNOWN_TICKER_CHANGES.get(t.upper())
            rows.append({"ticker": t, "status": "changed", **known} if known else {"ticker": t, "status": "unknown_offline"})
    df = pd.DataFrame(rows)
    df["edgar_live"] = tmap is not None
    return df
