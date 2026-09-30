"""The quality checks must catch every injected defect — and nothing that wasn't injected."""
import pandas as pd
import pytest

from mdlab.quality import BAR_CHECKS, TICKER_CHECKS, run_checks, score_against_injected
from mdlab.reconcile import methodology_gap, reconcile


@pytest.fixture(scope="module")
def result(panel, settings):
    return run_checks(panel, settings)


def test_registry_has_the_documented_checks():
    assert {"missing_bar", "zero_volume", "price_jump", "stale_print", "vendor_disagreement",
            "split_adjustment_mismatch", "ohlc_integrity", "volume_outlier"} <= set(BAR_CHECKS)
    assert {"stale_series", "late_start", "no_data", "ticker_change", "ingest_rejected", "coverage_mismatch"} <= set(TICKER_CHECKS)


def test_every_check_returns_bool_series(result):
    assert result.bar_flags.dtypes.eq(bool).all()
    assert result.ticker_flags.dtypes.eq(bool).all()
    assert result.bar_flags.index.names == ["source", "ticker", "date"]


def test_all_injected_defects_are_detected(result, injected):
    score = score_against_injected(result, injected, expected_absent=("FB",))
    assert len(score) == 8, score
    assert (score["recall"] == 1.0).all(), score
    assert (score["precision_vs_injected"] == 1.0).all(), score


def test_split_dates_do_not_trigger_price_jump(result):
    # NVDA 10:1 on 2024-06-10 and WMT 3:1 on 2024-02-26 are inside the window
    pj = result.bar_flags["price_jump"]
    for tkr in ("NVDA", "WMT"):
        assert not pj.xs(tkr, level="ticker").any(), f"{tkr} split leaked into price_jump"


def test_mislabeled_split_basis_is_caught_on_the_split_date(result):
    sam = result.bar_flags["split_adjustment_mismatch"]
    hits = sam[sam]
    assert list(hits.index.get_level_values("source").unique()) == ["synth_a"]
    assert list(hits.index.get_level_values("ticker").unique()) == ["WMT"]
    assert hits.index.get_level_values("date")[0] == pd.Timestamp("2024-02-26")


def test_schema_rejects_impossible_bars_and_it_is_reported(panel, result):
    assert (panel.failures["error"].str.startswith("SchemaErrors")).sum() == 1
    assert result.ticker_flags.loc[("synth_b", "PEP"), "ingest_rejected"]


def test_delisted_probe_has_no_data_everywhere(result):
    nd = result.ticker_flags["no_data"]
    assert nd.xs("FB", level="ticker").all()


def test_reconciliation_fair_comparison_is_tight_between_honest_vendors(panel):
    rec = reconcile(panel, "close_split", 0.01)
    bc = rec.pair_summary.set_index("pair").loc["synth_b|synth_c"]
    assert bc["median_abs_bps"] == 0.0
    assert bc["pct_beyond_tol"] < 0.1


def test_methodology_gap_fingerprints_split_only_vendor(panel):
    mg = methodology_gap(panel)
    split_only = mg[mg["vendor_adj_basis"] == "split"]
    tr = mg[(mg["vendor_adj_basis"] == "total_return") & (mg["source"] == "synth_b")]
    # a split-only vendor drifts by the dividend yield; a total-return vendor does not
    assert split_only["median_gap_bps"].median() > 50
    assert tr["median_gap_bps"].median() < 1
