import pandas as pd
import pytest

from mdlab.cache import ParquetCache
from mdlab.config import Settings
from mdlab.panel import add_rebased_closes, fetch_panel
from mdlab.sources import make_synthetic_trio

TICKERS = ["AAPL", "NVDA", "KO", "PEP", "PLD", "DUK", "F", "CAT", "WMT", "FB"]
START, END = "2023-01-01", "2024-12-31"


@pytest.fixture(scope="session")
def settings(tmp_path_factory) -> Settings:
    root = tmp_path_factory.mktemp("mdlab")
    return Settings(cache_dir=root / "cache", report_dir=root / "reports")


@pytest.fixture(scope="session")
def trio(settings):
    return make_synthetic_trio(cache=ParquetCache(settings.cache_dir))


@pytest.fixture(scope="session")
def panel(trio):
    return add_rebased_closes(fetch_panel(trio, TICKERS, START, END))


@pytest.fixture(scope="session")
def injected(trio) -> pd.DataFrame:
    return pd.concat([s.injected_frame() for s in trio], ignore_index=True)
