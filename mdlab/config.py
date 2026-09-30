"""
Runtime configuration.

Design goals
------------
* Never hard-code secrets. Keys are read from environment variables, a local
  ``.env`` file, or (when running in Colab) ``google.colab.userdata``.
* One immutable ``Settings`` object is threaded through the pipeline so every
  component sees the same paths, thresholds and API keys.
* Everything has a sane default so ``Settings()`` works out of the box in
  offline (synthetic-vendor) mode with zero setup.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (avoids a python-dotenv dependency).

    Only sets variables that are not already present in ``os.environ`` so that
    explicit shell exports always win over the file.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _colab_secret(name: str) -> Optional[str]:
    """Read a secret from Colab's Secrets panel if we are running in Colab."""
    try:  # pragma: no cover - only exercised inside Colab
        from google.colab import userdata  # type: ignore

        return userdata.get(name)
    except Exception:
        return None


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a secret by precedence: environment > Colab userdata > default."""
    return os.environ.get(name) or _colab_secret(name) or default


@dataclass(frozen=True)
class Settings:
    """All tunables for a pipeline run.

    Attributes
    ----------
    cache_dir : Path
        Root of the Parquet response cache.
    report_dir : Path
        Where HTML / markdown reports and figures are written.
    fmp_api_key, polygon_api_key : str | None
        Vendor credentials. ``None`` disables that vendor gracefully.
    edgar_user_agent : str
        SEC requires a descriptive User-Agent with contact info; requests
        without one are rejected with HTTP 403.
    price_jump_threshold : float
        Absolute one-day return above which a bar is flagged (default 20%).
    vendor_disagreement_threshold : float
        Relative close-price difference between vendors above which the bar is
        flagged as a cross-vendor disagreement (default 1%).
    stale_run_length : int
        Number of consecutive identical closes needed to flag a stale print.
    stale_series_bdays : int
        A series whose last bar is older than this many business days before
        the requested end date is flagged as stale/dead.
    request_timeout : float
        Per-request HTTP timeout in seconds.
    """

    cache_dir: Path = field(default_factory=lambda: Path("cache"))
    report_dir: Path = field(default_factory=lambda: Path("reports"))
    fmp_api_key: Optional[str] = field(default_factory=lambda: get_secret("FMP_API_KEY"))
    polygon_api_key: Optional[str] = field(default_factory=lambda: get_secret("POLYGON_API_KEY"))
    edgar_user_agent: str = field(
        default_factory=lambda: get_secret("EDGAR_USER_AGENT", "market-data-lab contact@example.com")
    )
    price_jump_threshold: float = 0.20
    vendor_disagreement_threshold: float = 0.01
    stale_run_length: int = 3
    stale_series_bdays: int = 5
    request_timeout: float = 30.0

    def __post_init__(self) -> None:
        # dataclass is frozen -> use object.__setattr__ for path coercion
        object.__setattr__(self, "cache_dir", Path(self.cache_dir))
        object.__setattr__(self, "report_dir", Path(self.report_dir))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, dotenv: Optional[Path] = Path(".env"), **overrides) -> "Settings":
        """Build settings after loading an optional ``.env`` file."""
        if dotenv is not None:
            _load_dotenv(Path(dotenv))
        return cls(**overrides)

    def available_vendors(self) -> list[str]:
        """Live vendors we actually hold credentials for (Yahoo needs none)."""
        vendors = ["yahoo"]
        if self.fmp_api_key:
            vendors.append("fmp")
        if self.polygon_api_key:
            vendors.append("polygon")
        return vendors


def configure_logging(level: int = logging.INFO) -> None:
    """Consistent, timestamped logging for scripts and notebooks."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # third-party chatter we do not need at INFO
    for noisy in ("urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
