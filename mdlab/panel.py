"""
Assemble per-(source, ticker) bar frames into one analysable panel.

Two layouts, one truth
----------------------
* **Long**: one row per (source, ticker, date) — the storage format. Parquet
  handles it natively (MultiIndex *columns* are not supported by pyarrow), it
  appends cleanly, and groupby/pivot gets you anything else.
* **Wide**: ``date × MultiIndex(source, ticker)`` for a single field — the
  analysis format (reconciliation, heatmaps, vectorised checks).

``Panel`` wraps the long frame and offers ``wide(field)`` on demand, plus
save/load round-trips and a ``fetch_panel`` builder that iterates sources ×
tickers with per-item error isolation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import pandas as pd

from .schema import BAR_COLS
from .sources.base import DataSource

log = logging.getLogger(__name__)

KEY_COLS = ["source", "ticker", "date"]


@dataclass
class Panel:
    """Long-format bar panel + metadata about how it was built."""

    long: pd.DataFrame
    sources: dict[str, dict] = field(default_factory=dict)      # name -> {close_basis, adj_close_basis}
    actions: dict[str, tuple[pd.Series, pd.Series]] = field(default_factory=dict)  # ticker -> (div, splits)
    failures: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["source", "ticker", "error"]))
    start: Optional[str] = None
    end: Optional[str] = None

    # -- accessors ---------------------------------------------------------
    @property
    def tickers(self) -> list[str]:
        return sorted(self.long["ticker"].unique().tolist())

    @property
    def source_names(self) -> list[str]:
        return sorted(self.long["source"].unique().tolist())

    def wide(self, field_name: str = "close") -> pd.DataFrame:
        """``date × (source, ticker)`` matrix for one field."""
        w = self.long.pivot_table(index="date", columns=["source", "ticker"], values=field_name, aggfunc="first")
        return w.sort_index()

    def bars(self, source: str, ticker: str) -> pd.DataFrame:
        sub = self.long[(self.long["source"] == source) & (self.long["ticker"] == ticker)]
        return sub.set_index("date")[BAR_COLS].sort_index()

    def coverage(self) -> pd.DataFrame:
        """rows / first / last per (source, ticker)."""
        g = self.long.groupby(["source", "ticker"])["date"]
        return pd.DataFrame({"rows": g.size(), "first": g.min(), "last": g.max()}).reset_index()

    # -- persistence -------------------------------------------------------
    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.long.to_parquet(path, engine="pyarrow", compression="zstd", index=False)
        meta = {
            "sources": self.sources, "start": self.start, "end": self.end,
            "actions": {t: {"dividends": d.to_dict(), "splits": s.to_dict()} for t, (d, s) in self.actions.items()},
        }
        pd.Series(meta).to_json(path.with_suffix(".meta.json"), default_handler=str)
        return path

    @classmethod
    def load(cls, path: Path | str) -> "Panel":
        path = Path(path)
        long = pd.read_parquet(path)
        meta_p = path.with_suffix(".meta.json")
        sources, start, end, actions = {}, None, None, {}
        if meta_p.exists():
            import json

            meta = json.loads(meta_p.read_text())
            sources = meta.get("sources", {}) or {}
            start, end = meta.get("start"), meta.get("end")
            for t, a in (meta.get("actions") or {}).items():
                d = pd.Series(a.get("dividends", {}), dtype="float64", name="dividends")
                s = pd.Series(a.get("splits", {}), dtype="float64", name="splits")
                d.index, s.index = pd.to_datetime(d.index), pd.to_datetime(s.index)
                actions[t] = (d, s)
        return cls(long=long, sources=sources, actions=actions, start=start, end=end)


def fetch_panel(
    sources: Iterable[DataSource],
    tickers: Iterable[str],
    start: str,
    end: str,
    *,
    actions_from: Optional[DataSource] = None,
    progress: Optional[Callable[[str], None]] = None,
    stop_on_permanent_error: bool = False,
) -> Panel:
    """Fetch every (source, ticker) pair and stack into a :class:`Panel`.

    Error isolation: one bad symbol or one vendor hiccup is recorded in
    ``panel.failures`` and the loop continues. Set ``stop_on_permanent_error``
    when a bad API key should abort early instead of failing 100 times.

    ``actions_from`` selects the vendor whose dividends/splits become the
    reference corporate-action table (defaults to the first source that
    provides actions). All vendors are re-based against the *same* table so
    that adjustment-methodology differences are isolated from action-table
    differences.
    """
    sources, tickers = list(sources), list(tickers)
    frames, fails, src_meta = [], [], {}
    bars_by_key: dict[tuple[str, str], pd.DataFrame] = {}
    t0 = time.perf_counter()
    total = len(sources) * len(tickers)
    done = 0

    for src in sources:
        src_meta[src.name] = {"close_basis": src.close_basis, "adj_close_basis": src.adj_close_basis}
        for tkr in tickers:
            done += 1
            try:
                df = src.get_ohlcv(tkr, start, end)
            except Exception as exc:  # noqa: BLE001 — isolate per item, record, continue
                fails.append({"source": src.name, "ticker": tkr, "error": _short_error(exc)})
                from .sources.base import PermanentError

                if stop_on_permanent_error and isinstance(exc, PermanentError):
                    raise
                continue
            if df.empty:
                fails.append({"source": src.name, "ticker": tkr, "error": "NoData: empty response"})
                continue
            bars_by_key[(src.name, tkr)] = df
            long = df.reset_index()
            long.insert(0, "ticker", tkr)
            long.insert(0, "source", src.name)
            frames.append(long)
            if progress and (done % 10 == 0 or done == total):
                progress(f"{done}/{total} fetched  ({src.name}:{tkr})  {time.perf_counter() - t0:5.1f}s")

    long = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=KEY_COLS + BAR_COLS)
    long["date"] = pd.to_datetime(long["date"])
    long = long.sort_values(KEY_COLS).reset_index(drop=True)

    # reference corporate actions: preferred vendor first, then fall back to any
    # other vendor that carries the name (a reference vendor missing a ticker
    # must not silently strip that ticker's dividends from every rebase)
    #
    # The action table is stored in RAW share terms (dividend per then-outstanding
    # share). Vendors that restate history in post-split shares (Yahoo) are
    # converted here so ``adjust_bars`` sees one convention.
    actions: dict[str, tuple[pd.Series, pd.Series]] = {}
    providers = [s for s in sources if s.provides_actions]
    if actions_from is not None:
        providers = [actions_from] + [s for s in providers if s is not actions_from]
    for tkr in tickers:
        div, spl = pd.Series(dtype=float), pd.Series(dtype=float)
        for prov in providers:
            # cheap path: the bars we already hold carry the events (Yahoo, synthetic)
            div, spl = _actions_from_bars(bars_by_key.get((prov.name, tkr)))
            if not (len(div) or len(spl)):
                div, spl = prov.get_actions(tkr, start, end)
            if len(div) or len(spl):
                if prov.actions_basis == "split_adjusted" and len(div) and len(spl):
                    from .adjust import split_factors

                    div = div / split_factors(pd.DatetimeIndex(div.index), spl)
                break
        actions[tkr] = (div, spl)

    panel = Panel(
        long=long, sources=src_meta, actions=actions,
        failures=pd.DataFrame(fails, columns=["source", "ticker", "error"]),
        start=start, end=end,
    )
    log.info("panel: %d rows, %d sources, %d tickers, %d failures, %.1fs",
             len(long), len(src_meta), long["ticker"].nunique() if len(long) else 0, len(fails),
             time.perf_counter() - t0)
    return panel


def _actions_from_bars(df: Optional[pd.DataFrame]) -> tuple[pd.Series, pd.Series]:
    """Non-zero dividend / split rows carried inside a bar frame (empty if the vendor doesn't populate them)."""
    if df is None or df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    div = df["dividends"][df["dividends"].fillna(0) > 0].astype(float).rename("dividends")
    spl = df["splits"][df["splits"].fillna(0) > 0].astype(float).rename("splits")
    return div, spl


def _short_error(exc: BaseException, limit: int = 160) -> str:
    """One-line error for the failures table; the full traceback is already in the log."""
    msg = " ".join(str(exc).split())
    name = type(exc).__name__
    if name == "SchemaErrors":
        import re

        checks = sorted(set(re.findall(r'"check": "([^"]+)"', str(exc))))
        msg = "pandera rejected payload: " + ", ".join(checks)
    return f"{name}: {msg[:limit]}{'…' if len(msg) > limit else ''}"


def add_rebased_closes(panel: Panel) -> Panel:
    """Attach ``close_split``, ``close_tr`` (total-return) and ``volume_split`` columns computed by
    *our* adjustment engine from each vendor's close + the reference action table.

    This is the step that makes vendors comparable: after it, ``close_split`` from
    a raw vendor (Polygon/FMP) and from a split-adjusted vendor (Yahoo) should
    agree to within a few bps.
    """
    from .adjust import adjust_bars

    parts = []
    for (src, tkr), grp in panel.long.groupby(["source", "ticker"], sort=False):
        bars = grp.set_index("date")[BAR_COLS]
        div, spl = panel.actions.get(tkr, (pd.Series(dtype=float), pd.Series(dtype=float)))
        basis = panel.sources.get(src, {}).get("close_basis", "raw")
        try:
            split_frame = adjust_bars(bars, spl, div, input_basis=basis, target="split")
            split_adj, vol_split = split_frame["close"], split_frame["volume"]
            tr_adj = adjust_bars(bars, spl, div, input_basis=basis, target="total_return")["close"]
        except Exception as exc:  # pragma: no cover
            log.warning("rebase failed for %s:%s (%s)", src, tkr, exc)
            split_adj = tr_adj = vol_split = pd.Series(index=bars.index, dtype=float)
        g = grp.copy()
        g["close_split"] = split_adj.values
        g["close_tr"] = tr_adj.values
        g["volume_split"] = vol_split.values
        parts.append(g)
    panel.long = pd.concat(parts, ignore_index=True) if parts else panel.long
    return panel
