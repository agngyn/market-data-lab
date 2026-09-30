"""
End-to-end orchestration: fetch -> cache -> panel -> rebase -> reconcile -> checks -> report.

Two modes
---------
* ``mode="live"``      Yahoo (+ FMP / Polygon when keys are present)
* ``mode="synthetic"`` three deterministic vendors with injected defects; no
                       network; also produces a detection scorecard

CLI::

    python -m mdlab.pipeline --mode synthetic --start 2023-01-01 --end 2024-12-31
    python -m mdlab.pipeline --mode live --tickers AAPL MSFT NVDA --vendors yahoo
"""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from .adjust import compare_bases
from .cache import ParquetCache
from .config import Settings, configure_logging
from .edgar import EdgarClient, ticker_change_report
from .panel import Panel, add_rebased_closes, fetch_panel
from .quality import QualityResult, run_checks, score_against_injected
from .reconcile import Reconciliation, methodology_gap, reconcile
from .report import Report
from .universe import PROBES, REFERENCE_SPLITS, default_universe
from . import viz

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    panel: Panel
    reconciliation: Reconciliation            # on our re-based split-adjusted close (fair)
    reconciliation_vendor_adj: Reconciliation  # on each vendor's own adj_close (exposes methodology)
    methodology: pd.DataFrame
    quality: QualityResult
    edgar: pd.DataFrame
    injected: pd.DataFrame
    detection: pd.DataFrame
    report_paths: dict[str, Path]
    stats: dict = field(default_factory=dict)

    def summary(self) -> pd.Series:
        s = self.quality.scorecard()
        s["vendors"] = ", ".join(self.panel.source_names)
        s["tickers"] = len(self.panel.tickers)
        s["fetch_seconds"] = round(self.stats.get("fetch_seconds", 0.0), 1)
        s["cache_hit_rate"] = self.stats.get("cache", {}).get("hit_rate")
        return s


def run_pipeline(
    tickers: Optional[list[str]] = None,
    start: str = "2022-01-01",
    end: Optional[str] = None,
    *,
    mode: str = "live",
    vendors: Optional[list[str]] = None,
    settings: Optional[Settings] = None,
    fetch_adjusted: bool = True,
    use_edgar: bool = True,
    make_report: bool = True,
    report_stem: str = "data_quality_report",
    overlay_ticker: Optional[str] = None,
    progress: bool = True,
    discrepancy_notes: Optional[str] = None,
) -> PipelineResult:
    """Run the whole lab and return every intermediate object."""
    t_all = time.perf_counter()
    settings = settings or Settings.from_env()
    end = end or (pd.Timestamp.today().normalize() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    tickers = tickers or default_universe()
    cache = ParquetCache(settings.cache_dir)

    # 1. vendors ---------------------------------------------------------
    if mode == "synthetic":
        from .sources import make_synthetic_trio

        sources = make_synthetic_trio(cache=cache)
    elif mode == "live":
        from .sources import make_sources

        sources = make_sources(settings, cache=cache, vendors=vendors, fetch_adjusted=fetch_adjusted)
    else:
        raise ValueError("mode must be 'live' or 'synthetic'")
    log.info("mode=%s vendors=%s tickers=%d window=%s..%s", mode, [s.name for s in sources], len(tickers), start, end)

    # 2. fetch + panel -----------------------------------------------------
    t0 = time.perf_counter()
    panel = fetch_panel(sources, tickers, start, end, progress=(log.info if progress else None))
    fetch_seconds = time.perf_counter() - t0
    panel = add_rebased_closes(panel)

    # 3. reconcile ---------------------------------------------------------
    rec = reconcile(panel, "close_split", settings.vendor_disagreement_threshold)
    rec_adj = reconcile(panel, "adj_close", settings.vendor_disagreement_threshold)
    method = methodology_gap(panel, settings.vendor_disagreement_threshold)

    # 4. EDGAR -------------------------------------------------------------
    edgar_client = None
    if use_edgar and mode == "live":
        edgar_client = EdgarClient(settings.edgar_user_agent, cache_dir=settings.cache_dir / "edgar")
    edgar_df = ticker_change_report(tickers, edgar_client)
    edgar_map = edgar_client.ticker_map() if edgar_client else None

    # 5. quality checks ----------------------------------------------------
    quality = run_checks(panel, settings, edgar_map=edgar_map)

    # 6. synthetic scorecard -------------------------------------------------
    injected = pd.DataFrame(columns=["source", "ticker", "date", "kind"])
    detection = pd.DataFrame()
    if mode == "synthetic":
        injected = pd.concat([s.injected_frame() for s in sources], ignore_index=True)
        detection = score_against_injected(quality, injected, expected_absent=tuple(PROBES))

    stats = {
        "fetch_seconds": fetch_seconds,
        "cache": cache.stats.as_dict(),
        "vendors": {s.name: s.stats.as_dict() for s in sources},
        "rows": int(len(panel.long)),
        "check_timings_s": {k: round(v, 3) for k, v in quality.timings.items()},
    }

    # 7. report --------------------------------------------------------------
    paths: dict[str, Path] = {}
    if make_report:
        paths = build_report(panel, rec, rec_adj, method, quality, edgar_df, detection, stats, settings,
                             mode=mode, stem=report_stem, overlay_ticker=overlay_ticker,
                             discrepancy_notes=discrepancy_notes)
    stats["total_seconds"] = round(time.perf_counter() - t_all, 1)
    log.info("pipeline done in %.1fs", stats["total_seconds"])
    return PipelineResult(panel, rec, rec_adj, method, quality, edgar_df, injected, detection, paths, stats)


# --------------------------------------------------------------------------
def build_report(panel, rec, rec_adj, method, quality, edgar_df, detection, stats, settings, *,
                 mode: str, stem: str, overlay_ticker: Optional[str], discrepancy_notes: Optional[str]) -> dict[str, Path]:
    figdir = settings.report_dir / "figures"
    pal = viz.VendorPalette(panel.source_names)
    sc = quality.scorecard()
    rep = Report(
        title="Market Data Quality Report",
        subtitle=f"{len(panel.tickers)} tickers · {', '.join(panel.source_names)} · {panel.start} → {panel.end} · mode: {mode}",
    )
    rep.add_tile("bars checked", int(sc["bars_checked"]))
    rep.add_tile("bar failure rate", f"{sc['bar_failure_rate_pct']:.2f}%", "bad" if sc["bar_failure_rate_pct"] > 1 else "ok")
    rep.add_tile("bars with ≥1 failure", int(sc["bars_with_any_failure"]))
    rep.add_tile("ticker-level issues", int(sc["ticker_level_failures"]), "bad" if sc["ticker_level_failures"] else "ok")
    rep.add_tile("clean series", int(sc["clean_series"]))
    rep.add_tile(f"checks run ({int(sc['bar_checks'])} bar + {int(sc['ticker_checks'])} ticker)", int(sc["bar_checks"] + sc["ticker_checks"]))
    rep.add_tile("cache hit rate", f"{100 * stats['cache']['hit_rate']:.0f}%")
    rep.add_tile("fetch time", f"{stats['fetch_seconds']:.0f}s")

    # coverage
    cov = panel.coverage()
    rep.add_figure("Coverage by vendor", viz.plot_coverage(cov, pal, "sessions missing vs the best vendor for the same ticker", figdir / "coverage.png"),
                   "Only tickers where at least one vendor is short are shown. Long bars = truncated history or a vendor that does not carry the name.")
    if len(panel.failures):
        rep.add_table("Fetch failures (vendor returned nothing or payload rejected)", panel.failures,
                      note="NoData = delisted / renamed / not carried. SchemaErrors = pandera rejected physically impossible bars at ingest.")

    # reconciliation (needs >= 2 vendors)
    multi = len(panel.source_names) >= 2
    if not multi:
        rep.add_text("Vendor reconciliation", "Only one vendor in this run — cross-vendor reconciliation is skipped. "
                     "Add FMP_API_KEY / POLYGON_API_KEY (or run `--mode synthetic`) to see pairwise comparisons.")
    if multi:
        rep.add_table("Vendor reconciliation — re-based split-adjusted close (fair comparison)", rec.pair_summary,
                      note=f"Every vendor's close converted to the same split basis using one reference action table. "
                           f"Tolerance {100 * rec.tolerance:.1f}%. Residual disagreement here is data error or restatement timing.")
        rep.add_figure("Vendor price-difference distribution",
                       viz.plot_vendor_diff_distribution(rec.pair_diffs, "close_split: pairwise vendor differences",
                                                         figdir / "vendor_diff_close_split.png", tolerance_bps=1e4 * rec.tolerance),
                       "Healthy vendors pile up at 0 bp. A tail beyond the dashed tolerance is a data problem on one side.")
        rep.add_table("Largest disagreements", rec.worst(25))
        rep.add_table("Vendor reconciliation — each vendor's OWN adj_close (methodology comparison)", rec_adj.pair_summary,
                      note="Deliberately unfair: vendors define 'adjusted' differently (split-only vs split+dividend). "
                           "A systematic mean offset with a fat distribution is the fingerprint of a methodology gap, not bad data.")
        rep.add_figure("Vendor adjusted-close difference distribution",
                       viz.plot_vendor_diff_distribution(rec_adj.pair_diffs, "adj_close: pairwise vendor differences",
                                                         figdir / "vendor_diff_adj_close.png", clip_bps=600, tolerance_bps=1e4 * rec.tolerance),
                       "Compare with the previous chart: the extra spread is dividends, not errors.")
        rep.add_table("Methodology gap per vendor (vendor adj_close vs our total-return rebase)",
                      method.groupby(["source", "vendor_adj_basis"], dropna=False)[["median_gap_bps", "gap_at_start_bps", "pct_days_beyond_tol"]]
                      .median().reset_index(),
                      note="Median across tickers. ~0 bp = vendor adjusts for dividends the same way we do; a gap that grows with lookback = split-only adjustment.")
        gap_ticker = _pick_gap_ticker(method, panel)
        if gap_ticker:
            rep.add_figure(f"Methodology drift — {gap_ticker}",
                           viz.plot_methodology_gap(panel.long, gap_ticker, pal, panel.sources, figdir / f"methodology_{gap_ticker}.png"),
                           "Both series normalised to their last bar. Flat at 0 = same method. A staircase drifting away from 0 = one vendor drops dividends.")

    # corporate action overlay
    ov = overlay_ticker or _pick_overlay_ticker(panel)
    if ov:
        src = _best_source_for(panel, ov)
        bars = panel.bars(src, ov)
        div, spl = panel.actions.get(ov, (pd.Series(dtype=float), pd.Series(dtype=float)))
        ref = [(pd.Timestamp(d), r) for d, r in REFERENCE_SPLITS.get(ov, []) if pd.Timestamp(panel.start) <= pd.Timestamp(d) <= pd.Timestamp(panel.end)]
        if len(spl) == 0 and ref:
            spl = pd.Series([r for _, r in ref], index=pd.DatetimeIndex([d for d, _ in ref]))
        if len(spl):
            sd, ratio = spl.index[-1], float(spl.iloc[-1])
            bases = compare_bases(bars, spl, div, input_basis=panel.sources[src]["close_basis"])
            rep.add_figure(f"Adjusted vs unadjusted — {ov} ({src}) around the {ratio:g}:1 split",
                           viz.plot_adjustment_overlay(bases, ov, sd, ratio, figdir / f"overlay_{ov}.png"),
                           f"Source {src} declares close_basis='{panel.sources[src]['close_basis']}'. Left: three bases on a log axis. "
                           f"Right: the raw series shows a −{100 * (1 - 1 / ratio):.0f}% 'return' that never happened.")

    # quality
    rep.add_figure("Failures by check and vendor", viz.plot_failures_by_check(quality.summary_by_check(), pal, "failing bars / series per check", figdir / "failures_by_check.png"),
                   "Bar-level checks count bars; ticker-level checks count (vendor, ticker) series.")
    rep.add_table("Failure summary", quality.summary_by_check())
    for src in panel.source_names:
        m = quality.matrix(src)
        rep.add_figure(f"Failure heatmap — {src}",
                       viz.plot_failure_heatmap(m, f"{src}: bars failing any check (tickers × dates)", figdir / f"heatmap_{src}.png",
                                                subtitle="rows sorted by failure count; right-edge number = total failing bars"),
                       "Vertical stripes = a bad day across many names (feed outage / holiday mis-handled). Horizontal streaks = one bad series.")
    rep.add_table("Ticker-level issues", quality.failures().query("date != date")[["source", "ticker", "check"]] if len(quality.failures()) else pd.DataFrame(),
                  note="stale_series / late_start / no_data / ingest_rejected / ticker_change / coverage_mismatch")
    rep.add_table("Worst tickers", quality.summary_by_ticker(20))

    # edgar
    rep.add_table("Ticker → CIK resolution (SEC EDGAR)", edgar_df,
                  note="status=listed: symbol found in SEC's current map. changed/not_found: renamed, acquired or delisted — "
                       "a universe built from today's symbols would never contain these (survivorship bias).")

    # synthetic scorecard
    if len(detection):
        rep.add_table("Detection scorecard vs injected defects (synthetic mode)", detection,
                      note="Every defect was injected on purpose and recorded; recall = share detected, precision = share of flags that were injected defects.")

    # telemetry
    tele = pd.DataFrame(stats["vendors"]).T.reset_index().rename(columns={"index": "vendor"})
    rep.add_table("Fetch telemetry", tele, note=f"Cache: {stats['cache']}")
    rep.add_table("Check timings (seconds)", pd.Series(stats["check_timings_s"], name="seconds").rename_axis("check").reset_index())

    if discrepancy_notes:
        rep.add_text("Documented discrepancies", discrepancy_notes)

    return rep.save(settings.report_dir, stem=stem)


def _pick_overlay_ticker(panel: Panel) -> Optional[str]:
    start, end = pd.Timestamp(panel.start), pd.Timestamp(panel.end)
    for t in ("NVDA", "WMT", "AVGO", "CMG", "GOOGL", "AMZN", "TSLA", "AAPL", "SMCI", "LRCX"):
        if t in panel.tickers and any(start <= pd.Timestamp(d) <= end for d, _ in REFERENCE_SPLITS.get(t, [])):
            return t
    return None


def _pick_gap_ticker(method: pd.DataFrame, panel: Panel) -> Optional[str]:
    if method.empty:
        return None
    split_only = method[method["vendor_adj_basis"] == "split"]
    pool = split_only if len(split_only) else method
    pool = pool[pool["n"] > 50].sort_values("median_gap_bps", ascending=False)
    return str(pool["ticker"].iloc[0]) if len(pool) else None


def _best_source_for(panel: Panel, ticker: str) -> str:
    cov = panel.coverage().query("ticker == @ticker").sort_values("rows", ascending=False)
    # prefer a raw-basis vendor for the overlay so all three bases are visibly different
    for _, r in cov.iterrows():
        if panel.sources.get(r["source"], {}).get("close_basis") == "raw":
            return r["source"]
    return str(cov["source"].iloc[0])


# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="mdlab", description="multi-source market data pipeline & DQ lab")
    p.add_argument("--mode", choices=["live", "synthetic"], default="synthetic")
    p.add_argument("--tickers", nargs="*", default=None)
    p.add_argument("--n-core", type=int, default=None, help="use only the first N core tickers")
    p.add_argument("--vendors", nargs="*", default=None, help="live only: subset of yahoo fmp polygon")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--cache-dir", default="cache")
    p.add_argument("--report-dir", default="reports")
    p.add_argument("--no-edgar", action="store_true")
    p.add_argument("--no-adjusted", action="store_true", help="skip vendors' adjusted series (halves FMP/Polygon calls)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    configure_logging(logging.DEBUG if a.verbose else logging.INFO)
    settings = Settings.from_env(cache_dir=Path(a.cache_dir), report_dir=Path(a.report_dir))
    tickers = a.tickers or default_universe(a.n_core)
    res = run_pipeline(tickers, a.start, a.end, mode=a.mode, vendors=a.vendors, settings=settings,
                       fetch_adjusted=not a.no_adjusted, use_edgar=not a.no_edgar)
    print(res.summary().to_string())
    print("report:", res.report_paths.get("html"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
