"""
Data quality report — self-contained HTML (figures embedded as base64 PNG) plus
a Markdown twin for GitHub READMEs / PR comments.

Nothing here does analysis: the report only *renders* objects produced by
``panel``, ``reconcile``, ``quality`` and ``viz``. That separation keeps the
report deterministic and the analysis testable without a browser.
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import pandas as pd

log = logging.getLogger(__name__)

_CSS = """
:root{--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--surface:#fcfcfb;--page:#f9f9f7;
--blue:#2a78d6;--good:#0ca30c;--warn:#fab219;--serious:#ec835a;--critical:#d03b3b}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:40px 0 10px;padding-top:12px;border-top:1px solid var(--grid)}
h3{font-size:15px;margin:22px 0 6px;color:var(--ink2)}p.sub{color:var(--ink2);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
.tile{background:var(--surface);border:1px solid var(--grid);border-radius:8px;padding:12px 14px}
.tile .v{font-size:24px;font-weight:600;letter-spacing:-.01em}.tile .k{font-size:12px;color:var(--muted)}
.tile.bad .v{color:var(--critical)}.tile.ok .v{color:#006300}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--surface);margin:8px 0 16px}
th,td{padding:6px 9px;border-bottom:1px solid var(--grid);font-variant-numeric:tabular-nums}
.num{text-align:right}.txt{text-align:left}
th{color:var(--ink2);font-weight:600;background:var(--page);position:sticky;top:0}
tr:hover td{background:#f3f3f0}
.scroll{max-height:420px;overflow:auto;border:1px solid var(--grid);border-radius:6px}
figure{margin:14px 0 22px;background:var(--surface);border:1px solid var(--grid);border-radius:8px;padding:8px}
figure img{width:100%;height:auto;display:block}figcaption{font-size:12.5px;color:var(--ink2);padding:6px 6px 2px}
.note{background:var(--surface);border-left:3px solid var(--blue);padding:10px 14px;margin:12px 0;border-radius:0 6px 6px 0}
.pill{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:999px;border:1px solid var(--grid);color:var(--ink2);margin-right:6px}
code{font-size:12.5px;background:#f0efec;padding:1px 5px;border-radius:4px}
footer{margin-top:48px;color:var(--muted);font-size:12px}
@media (prefers-color-scheme:dark){:root{--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--surface:#1a1a19;--page:#0d0d0d}
tr:hover td{background:#222}code{background:#2c2c2a}.tile.ok .v{color:var(--good)}}
"""


def fig_to_b64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def df_html(df: pd.DataFrame, max_rows: int = 200, floatfmt: str = "{:,.2f}") -> str:
    if df is None or len(df) == 0:
        return "<p class='sub'>— none —</p>"
    import html as _h

    raw = df.head(max_rows)
    numeric = [c for c in raw.columns if pd.api.types.is_numeric_dtype(raw[c]) and not pd.api.types.is_bool_dtype(raw[c])]
    d = _format(raw, floatfmt)
    head = "".join(f"<th class='{'num' if c in numeric else 'txt'}'>{_h.escape(str(c))}</th>" for c in d.columns)
    rows = []
    for _, r in d.iterrows():
        cells = "".join(
            f"<td class='{'num' if c in numeric else 'txt'}'>{'' if pd.isna(v) else _h.escape(str(v))}</td>"
            for c, v in zip(d.columns, r.values)
        )
        rows.append(f"<tr>{cells}</tr>")
    html = f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    more = f"<p class='sub'>showing {max_rows} of {len(df):,} rows</p>" if len(df) > max_rows else ""
    return f"<div class='scroll'>{html}</div>{more}"


def _format(df: pd.DataFrame, floatfmt: str = "{:,.2f}") -> pd.DataFrame:
    """Human formatting: integral floats without decimals, dates as ISO, NaN blank."""
    d = df.copy()
    for c in d.columns:
        col = d[c]
        if pd.api.types.is_float_dtype(col):
            nonnull = col.dropna()
            if len(nonnull) and (nonnull == nonnull.round()).all() and nonnull.abs().max() >= 1:
                d[c] = col.map(lambda x: "" if pd.isna(x) else f"{int(x):,}")
            else:
                d[c] = col.map(lambda x: "" if pd.isna(x) else floatfmt.format(x))
        elif pd.api.types.is_datetime64_any_dtype(col):
            d[c] = col.dt.strftime("%Y-%m-%d")
    return d


def df_md(df: pd.DataFrame, max_rows: int = 40) -> str:
    """Pipe-table markdown without the ``tabulate`` dependency."""
    if df is None or len(df) == 0:
        return "_none_\n"
    d = _format(df.head(max_rows))
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r.values) + " |")
    if len(df) > max_rows:
        lines.append(f"\n_showing {max_rows} of {len(df):,} rows_")
    return "\n".join(lines) + "\n"


@dataclass
class ReportSection:
    title: str
    html: str
    markdown: str = ""


@dataclass
class Report:
    title: str
    subtitle: str
    tiles: list[tuple[str, str, str]] = field(default_factory=list)   # (label, value, css_class)
    sections: list[ReportSection] = field(default_factory=list)

    # -- builders ----------------------------------------------------------
    def add_tile(self, label: str, value, css: str = "") -> None:
        self.tiles.append((label, f"{value:,}" if isinstance(value, int) else str(value), css))

    def add_table(self, title: str, df: pd.DataFrame, note: str = "", max_rows: int = 200) -> None:
        html = (f"<div class='note'>{note}</div>" if note else "") + df_html(df, max_rows=max_rows)
        md = (f"> {note}\n\n" if note else "") + df_md(df)
        self.sections.append(ReportSection(title, html, md))

    def add_figure(self, title: str, fig: plt.Figure, caption: str = "", png_path: Optional[Path] = None) -> None:
        if png_path is not None:
            png_path = Path(png_path)
            png_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(png_path, dpi=150, bbox_inches="tight")
        b64 = fig_to_b64(fig)
        html = f"<figure><img alt='{title}' src='data:image/png;base64,{b64}'/><figcaption>{caption}</figcaption></figure>"
        rel = f"figures/{png_path.name}" if png_path is not None else ""
        md = (f"![{title}]({rel})\n\n_{caption}_\n" if rel else f"_{caption}_\n")
        self.sections.append(ReportSection(title, html, md))

    def add_text(self, title: str, markdown_text: str) -> None:
        html = "".join(f"<p>{p}</p>" for p in _md_paragraphs(markdown_text))
        self.sections.append(ReportSection(title, f"<div class='note'>{html}</div>", markdown_text))

    # -- render ------------------------------------------------------------
    def to_html(self) -> str:
        tiles = "".join(f"<div class='tile {c}'><div class='v'>{v}</div><div class='k'>{k}</div></div>" for k, v, c in self.tiles)
        body = "".join(f"<h2>{s.title}</h2>{s.html}" for s in self.sections)
        return (
            f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{self.title}</title><style>{_CSS}</style></head><body><main>"
            f"<h1>{self.title}</h1><p class='sub'>{self.subtitle}</p><div class='tiles'>{tiles}</div>{body}"
            f"<footer>generated {pd.Timestamp.now('UTC'):%Y-%m-%d %H:%M} UTC · market-data-lab</footer></main></body></html>"
        )

    def to_markdown(self) -> str:
        head = f"# {self.title}\n\n{self.subtitle}\n\n"
        tiles = "| metric | value |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v, _ in self.tiles) + "\n"
        body = "".join(f"## {s.title}\n\n{s.markdown}\n" for s in self.sections)
        return head + tiles + body

    def save(self, directory: Path | str, stem: str = "data_quality_report") -> dict[str, Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        html_p, md_p = directory / f"{stem}.html", directory / f"{stem}.md"
        html_p.write_text(self.to_html(), encoding="utf-8")
        md_p.write_text(self.to_markdown(), encoding="utf-8")
        log.info("report written: %s (%.1f KB), %s", html_p, html_p.stat().st_size / 1024, md_p)
        return {"html": html_p, "markdown": md_p}


def _md_paragraphs(text: str) -> list[str]:
    import html as _h
    import re

    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        p = _h.escape(para.strip())
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
        p = re.sub(r"`(.+?)`", r"<code>\1</code>", p)
        out.append(p.replace("\n", "<br/>"))
    return out
