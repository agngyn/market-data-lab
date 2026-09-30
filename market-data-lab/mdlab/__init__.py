"""
mdlab — Multi-source market data pipeline & data quality lab.

Pulls the same universe of tickers from several vendors (Yahoo Finance, FMP,
Polygon — or deterministic synthetic vendors for offline work), caches every
response as Parquet, reconciles vendors against each other, rebuilds
split/dividend-adjusted series from raw prices + corporate actions, and runs a
battery of assertion-style data quality checks that roll up into an HTML report.

Public entry point: :func:`mdlab.pipeline.run_pipeline`.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .config import Settings  # noqa: F401
from .pipeline import run_pipeline, PipelineResult  # noqa: F401
