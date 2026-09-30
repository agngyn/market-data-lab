"""
Report figures (matplotlib, headless-safe).

Conventions — the same in every chart so the report reads as one system:

* one categorical hue per **vendor**, assigned in fixed order and never
  re-cycled (a vendor keeps its colour in every figure);
* magnitude (failure counts, missing-ness) uses one sequential hue, light -> dark;
* status colours are reserved for pass/fail cues and never reused for a series;
* text and axes wear neutral ink; series colours are carried by marks only;
* recessive hairline grid, no top/right spines, 2px lines, direct labels where
  they help, a legend whenever >= 2 series are drawn.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import sys

import matplotlib

if "ipykernel" not in sys.modules:      # headless in scripts / CI; leave the inline backend alone in notebooks
    matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import BoundaryNorm, ListedColormap  # noqa: E402

log = logging.getLogger(__name__)

# -- palette (validated categorical order; see docs/design.md) --------------
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#1c5cab", "#104281", "#0d366b"]
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
SURFACE, PAGE = "#fcfcfb", "#f9f9f7"
INK, INK2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
        "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False, "axes.titleweight": "semibold",
        "axes.titlesize": 12, "axes.titlelocation": "left", "font.size": 10, "legend.frameon": False,
        "lines.linewidth": 2.0, "font.family": "sans-serif",
    }
)


class VendorPalette:
    """Fixed vendor -> colour assignment shared by every figure in a report."""

    def __init__(self, vendors: list[str]) -> None:
        self.map = {v: SERIES[i % len(SERIES)] for i, v in enumerate(sorted(vendors))}

    def __getitem__(self, vendor: str) -> str:
        return self.map.get(vendor, MUTED)


def _finish(fig: plt.Figure, path: Optional[Path]) -> plt.Figure:
    fig.tight_layout()
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        log.debug("saved %s", path)
    return fig


# ==========================================================================
# 1. failure / missing-data heatmap  (tickers × dates)
# ==========================================================================
def plot_failure_heatmap(matrix: pd.DataFrame, title: str, path: Optional[Path] = None,
                         max_tickers: int = 80, subtitle: str = "") -> plt.Figure:
    """``matrix``: ticker × date (0/1 or small counts). Rows sorted by total, worst on top."""
    if matrix.empty:
        fig, ax = plt.subplots(figsize=(11, 2.2))
        ax.text(0.5, 0.5, "no failures", ha="center", va="center", color=MUTED, fontsize=13)
        ax.set_axis_off(); ax.set_title(title)
        return _finish(fig, path)

    m = matrix.copy()
    m = m.loc[m.sum(axis=1).sort_values(ascending=False).index].head(max_tickers)
    vmax = max(int(m.values.max()), 1)
    levels = min(vmax, len(SEQUENTIAL))
    cmap = ListedColormap([GRID] + SEQUENTIAL[-levels:] if levels > 1 else [GRID, SEQUENTIAL[4]])
    norm = BoundaryNorm(np.arange(-0.5, levels + 1.5, 1), cmap.N)

    h = max(2.5, 0.22 * len(m) + 1.6)
    fig, ax = plt.subplots(figsize=(12, h))
    ax.grid(False)
    ax.imshow(np.clip(m.values, 0, levels), aspect="auto", cmap=cmap, norm=norm, interpolation="nearest")
    ax.set_yticks(range(len(m))); ax.set_yticklabels(m.index, fontsize=8)
    dates = pd.DatetimeIndex(m.columns)
    step = max(1, len(dates) // 10)
    ax.set_xticks(range(0, len(dates), step))
    ax.set_xticklabels([d.strftime("%Y-%m") for d in dates[::step]], fontsize=8, rotation=0)
    ax.set_title(title, pad=16 if subtitle else 6)
    if subtitle:
        ax.text(0, 1.0, subtitle, transform=ax.transAxes, color=INK2, fontsize=8.5, va="bottom")
    for spine in ax.spines.values():
        spine.set_visible(False)
    # row totals as direct labels on the right
    tot = m.sum(axis=1).values
    for i, t in enumerate(tot):
        ax.text(len(dates) - 0.5 + len(dates) * 0.005, i, f" {int(t)}", va="center", fontsize=7.5, color=INK2)
    return _finish(fig, path)


# ==========================================================================
# 2. vendor price-difference distribution
# ==========================================================================
def plot_vendor_diff_distribution(pair_diffs: pd.DataFrame, title: str, path: Optional[Path] = None,
                                  clip_bps: float = 100.0, tolerance_bps: float = 100.0) -> plt.Figure:
    """Histogram of ``pct_diff`` (in bps) per vendor pair, symmetric log-ish view.

    Mass at exactly 0 is the healthy case; a second mode is a methodology gap;
    a long tail is bad data. Values beyond ``clip_bps`` are folded into the edge bins
    and their count is annotated so the tail is not silently hidden.
    """
    d = pair_diffs.dropna(subset=["pct_diff"]).copy()
    if d.empty:
        fig, ax = plt.subplots(figsize=(10, 3))
        ax.text(0.5, 0.5, "no overlapping bars to compare", ha="center", va="center", color=MUTED)
        ax.set_axis_off(); ax.set_title(title)
        return _finish(fig, path)

    d["bps"] = 1e4 * d["pct_diff"]
    pairs = sorted(d["pair"].unique())
    fig, axes = plt.subplots(len(pairs), 1, figsize=(11, 2.6 * len(pairs)), sharex=True, squeeze=False)
    bins = np.linspace(-clip_bps, clip_bps, 81)
    for ax, pair, color in zip(axes[:, 0], pairs, SERIES):
        x = d.loc[d["pair"] == pair, "bps"]
        n_tail = int((x.abs() > clip_bps).sum())
        ax.hist(x.clip(-clip_bps, clip_bps), bins=bins, color=color, edgecolor=SURFACE, linewidth=0.4)
        ax.set_yscale("log")
        ax.axvline(0, color=AXIS, lw=1)
        for t in (-tolerance_bps, tolerance_bps):
            if abs(t) < clip_bps:
                ax.axvline(t, color=STATUS["critical"], lw=1, ls="--", alpha=0.8)
        med, p99 = x.abs().median(), x.abs().quantile(0.99)
        share0 = (x.abs() < 0.5).mean()
        ax.text(0.99, 0.92, f"{pair}\n{share0:.1%} within ½ bp · median |Δ| {med:.1f} bp · p99 {p99:.0f} bp · "
                            f"{n_tail} beyond ±{clip_bps:.0f} bp (folded into edges)",
                transform=ax.transAxes, ha="right", va="top", fontsize=8.5, color=INK2)
        ax.set_ylabel("bars (log)")
    axes[-1, 0].set_xlabel("close-price difference, basis points  (a / b − 1)")
    axes[0, 0].set_title(title)
    return _finish(fig, path)


# ==========================================================================
# 3. adjusted vs unadjusted overlay on a split date
# ==========================================================================
def plot_adjustment_overlay(bases: pd.DataFrame, ticker: str, split_date, ratio: float,
                            path: Optional[Path] = None, window: int = 40, dividends: Optional[pd.Series] = None) -> plt.Figure:
    """``bases``: date-indexed frame with columns raw / split / total_return (from ``adjust.compare_bases``)."""
    split_date = pd.Timestamp(split_date)
    idx = bases.index
    pos = idx.searchsorted(split_date)
    lo, hi = max(0, pos - window), min(len(idx), pos + window)
    view = bases.iloc[lo:hi]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.2), gridspec_kw={"width_ratios": [1.15, 1]})
    labels = {"raw": "raw (as traded)", "split": "split-adjusted", "total_return": "split + dividend adjusted"}
    for i, col in enumerate(["raw", "split", "total_return"]):
        ax1.plot(view.index, view[col], color=SERIES[i], label=labels[col])
    ax1.axvline(split_date, color=AXIS, lw=1, ls="--")
    ax1.text(split_date, ax1.get_ylim()[1], f" {ratio:g}:1 split", va="top", fontsize=8.5, color=INK2)
    ax1.set_title(f"{ticker}: price on three bases around the split")
    ax1.set_yscale("log"); ax1.set_ylabel("price (log)")
    ax1.legend(loc="center right", fontsize=8.5)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    # right panel: the *return* view — raw shows a fake crash, adjusted does not
    rets = view.pct_change()
    ax2.plot(rets.index, 100 * rets["raw"], color=SERIES[0], label="raw", lw=1.6)
    ax2.plot(rets.index, 100 * rets["split"], color=SERIES[1], label="split-adjusted", lw=1.6)
    ax2.axvline(split_date, color=AXIS, lw=1, ls="--")
    fake = 100 * rets.loc[rets.index >= split_date, "raw"].iloc[0] if (rets.index >= split_date).any() else np.nan
    ax2.annotate(f"{fake:+.0f}% \"return\" if you forget to adjust", xy=(split_date, fake),
                 xytext=(0.45, 0.15), textcoords="axes fraction", fontsize=8.5, color=INK2,
                 arrowprops={"arrowstyle": "-", "color": AXIS, "lw": 0.8})
    ax2.set_title("daily return, %")
    ax2.legend(loc="lower right", fontsize=8.5)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    return _finish(fig, path)


# ==========================================================================
# 4. failures by check × vendor (stacked horizontal bars)
# ==========================================================================
def plot_failures_by_check(summary: pd.DataFrame, palette: VendorPalette, title: str,
                           path: Optional[Path] = None) -> plt.Figure:
    if summary.empty:
        fig, ax = plt.subplots(figsize=(10, 2.2))
        ax.text(0.5, 0.5, "no failures", ha="center", va="center", color=MUTED); ax.set_axis_off(); ax.set_title(title)
        return _finish(fig, path)
    piv = summary.pivot_table(index="check", columns="source", values="failures", aggfunc="sum").fillna(0)
    piv = piv.loc[piv.sum(axis=1).sort_values().index]
    fig, ax = plt.subplots(figsize=(11, max(3, 0.42 * len(piv) + 1.2)))
    left = np.zeros(len(piv))
    for src in sorted(piv.columns):
        vals = piv[src].values
        ax.barh(piv.index, vals, left=left, color=palette[src], label=src, height=0.62,
                edgecolor=SURFACE, linewidth=1.2)
        left += vals
    for i, total in enumerate(left):
        ax.text(total, i, f" {int(total):,}", va="center", fontsize=8.5, color=INK2)
    ax.set_xscale("symlog", linthresh=10)
    ax.set_xlabel("failing bars / series (symlog)")
    ax.set_title(title)
    ax.legend(title="vendor", fontsize=8.5, title_fontsize=8.5, loc="lower right")
    ax.grid(axis="y", visible=False)
    return _finish(fig, path)


# ==========================================================================
# 5. methodology gap: vendor adj_close vs our total-return rebase
# ==========================================================================
def plot_methodology_gap(panel_long: pd.DataFrame, ticker: str, palette: VendorPalette,
                         source_meta: dict, path: Optional[Path] = None) -> plt.Figure:
    """For one ticker, plot (vendor adj_close / our close_tr), both normalised to
    the final bar. A flat line at 0 = same methodology; a drift = the vendor
    ignores dividends (or uses a different convention)."""
    d = panel_long[(panel_long["ticker"] == ticker)].dropna(subset=["adj_close", "close_tr"])
    fig, ax = plt.subplots(figsize=(11, 3.8))
    if d.empty:
        ax.text(0.5, 0.5, f"no adjusted data for {ticker}", ha="center", va="center", color=MUTED); ax.set_axis_off()
        return _finish(fig, path)
    for src, g in d.groupby("source"):
        g = g.sort_values("date")
        ratio = (g["adj_close"] / g["adj_close"].iloc[-1]) / (g["close_tr"] / g["close_tr"].iloc[-1]) - 1
        basis = source_meta.get(src, {}).get("adj_close_basis", "?")
        ax.plot(g["date"], 1e4 * ratio, color=palette[src], label=f"{src}  (vendor adj basis: {basis})")
        ax.text(g["date"].iloc[0], 1e4 * ratio.iloc[0], f" {1e4 * ratio.iloc[0]:+.0f} bp", fontsize=8, color=INK2, va="bottom")
    ax.axhline(0, color=AXIS, lw=1)
    ax.set_ylabel("vendor adj_close vs our total-return rebase, bp")
    ax.set_title(f"{ticker}: does the vendor's 'adjusted' close include dividends?")
    ax.legend(fontsize=8.5, loc="best")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    return _finish(fig, path)


# ==========================================================================
# 6. coverage: bars per ticker per vendor
# ==========================================================================
def plot_coverage(coverage: pd.DataFrame, palette: VendorPalette, title: str, path: Optional[Path] = None,
                  max_tickers: int = 25) -> plt.Figure:
    """Sessions each vendor is missing relative to the best vendor for the same
    ticker. Only tickers with a shortfall are drawn, so a clean panel is one line."""
    if coverage.empty:
        fig, ax = plt.subplots(figsize=(10, 2)); ax.set_axis_off(); ax.set_title(title)
        return _finish(fig, path)
    piv = coverage.pivot_table(index="ticker", columns="source", values="rows", aggfunc="sum")
    missing = (piv.max(axis=1).values[:, None] - piv.fillna(0)).astype(int)
    missing = missing[(missing > 0).any(axis=1)]
    if missing.empty:
        fig, ax = plt.subplots(figsize=(10, 1.8))
        ax.text(0.5, 0.5, "every vendor has the same number of sessions for every ticker", ha="center", va="center", color=MUTED)
        ax.set_axis_off(); ax.set_title(title)
        return _finish(fig, path)
    n_short = len(missing)
    missing = missing.loc[missing.sum(axis=1).sort_values().index].tail(max_tickers)
    if n_short > max_tickers:
        title = f"{title}  (worst {max_tickers} of {n_short} tickers with a shortfall)"
    srcs = sorted(missing.columns)
    fig, ax = plt.subplots(figsize=(11, max(2.8, 0.34 * len(missing) + 1.4)))
    y = np.arange(len(missing))
    h = 0.8 / len(srcs)
    for i, src in enumerate(srcs):
        vals = missing[src].values
        ax.barh(y + i * h - 0.4 + h / 2, vals, height=h * 0.9, color=palette[src], label=src)
        for yy, v in zip(y + i * h - 0.4 + h / 2, vals):
            if v > 0:
                ax.text(v, yy, f" {v}", va="center", fontsize=7.5, color=INK2)
    ax.set_yticks(y); ax.set_yticklabels(missing.index, fontsize=8)
    ax.set_xlabel("sessions missing vs best vendor")
    ax.set_xscale("symlog", linthresh=10)
    ax.set_title(title); ax.legend(title="vendor", fontsize=8.5, title_fontsize=8.5, loc="lower right")
    ax.grid(axis="y", visible=False)
    return _finish(fig, path)
