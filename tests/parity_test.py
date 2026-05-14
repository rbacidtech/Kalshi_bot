"""tests/parity_test.py — Parity against Becker's market-microstructure benchmarks.

Phase 1.1 S.4 Step 3 of EdgePulse_Migration_Plan_2026.md. Reproduces the
benchmarks documented in EdgePulse_Backtest_DataPipeline.md §1.4 against
Becker's transformed wealth_transfer parquet data. Gates Step 4 (deploy.sh
integration) — all unskipped assertions must pass before deploy is unblocked.

Three assertions per the user's Step 3 spec; two are verifiable from docs
on this server and shipping now; the third is skipped pending primary-source
verification of the 40-48% benchmark range (see research/becker_benchmarks.json
`unverified_assertions[0]`).

Methodology (DataPipeline.md §4):
    tw = ((side == 'YES') & (outcome == 1)) | ((side == 'NO') & (outcome == 0))
    tr = tw.astype(float) - price
    mr = -tr  # maker excess return = negation of taker excess

Run:
    python -m pytest tests/parity_test.py -v
    # or directly (returns exit code suitable for deploy.sh integration):
    python tests/parity_test.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest


_BENCHMARKS_PATH = Path(__file__).resolve().parent.parent / "research" / "becker_benchmarks.json"


def _load_benchmarks() -> dict:
    if not _BENCHMARKS_PATH.exists():
        raise FileNotFoundError(
            f"Benchmark spec missing: {_BENCHMARKS_PATH}. This file is the "
            f"source of truth for parity assertions; restore from git."
        )
    with open(_BENCHMARKS_PATH) as f:
        return json.load(f)


def _resolve_trades_parquet() -> Path:
    root = os.environ.get("WEALTH_TRANSFER_ROOT", "/root/research/wealth_transfer")
    p = Path(root) / "data" / "trades.parquet"
    if not p.exists():
        raise FileNotFoundError(
            f"Becker transformed-trades parquet not found at {p}. See "
            f"EdgePulse_Backtest_DataPipeline.md §2 for the transform.py script "
            f"that produces data/trades.parquet from Jon-Becker/prediction-market-"
            f"analysis raw parquets. Set WEALTH_TRANSFER_ROOT to override."
        )
    return p


@pytest.fixture(scope="module")
def benchmarks() -> dict:
    return _load_benchmarks()


@pytest.fixture(scope="module")
def trades_df() -> pd.DataFrame:
    p = _resolve_trades_parquet()
    df = pd.read_parquet(p)
    required_cols = {"price", "side", "outcome", "volume"}
    missing = required_cols - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Becker parquet at {p} missing columns {missing}. "
            f"Expected schema per DataPipeline.md §2: "
            f"price, side, is_taker_buy, outcome, category, volume."
        )
    return df


def _maker_excess(df: pd.DataFrame) -> pd.Series:
    """Per-row maker excess return; mean across rows is the headline statistic."""
    taker_won = ((df["side"] == "YES") & (df["outcome"] == 1)) | (
        (df["side"] == "NO") & (df["outcome"] == 0)
    )
    taker_excess = taker_won.astype(float) - df["price"]
    return -taker_excess


def test_assertion_1_maker_excess_return(trades_df, benchmarks):
    """Maker excess return matches Becker's 1.12% within ±0.10 pp."""
    target_pct = benchmarks["benchmarks"]["maker_excess_return_pct"]["becker_2026"]
    tol_pp = benchmarks["tolerance_pp"]
    actual_pct = _maker_excess(trades_df).mean() * 100
    drift_pp = actual_pct - target_pct
    assert abs(drift_pp) <= tol_pp, (
        f"Maker excess return {actual_pct:+.4f}% diverges from Becker "
        f"{target_pct:+.2f}% by {drift_pp:+.4f} pp (tolerance ±{tol_pp} pp). "
        f"See EdgePulse_Backtest_DataPipeline.md §5 (Errata) for known pipeline "
        f"pitfalls; §5.1 inverted-sign bug is the most common cause."
    )


def test_assertion_2_no_gt_yes_maker_excess(trades_df, benchmarks):
    """Optimism Tax: maker NO excess > maker YES excess by min documented gap."""
    mr = _maker_excess(trades_df)
    # When taker is on YES, maker is on NO — so filter by taker side.
    maker_no_pct = mr[trades_df["side"] == "YES"].mean() * 100
    maker_yes_pct = mr[trades_df["side"] == "NO"].mean() * 100
    gap_pp = maker_no_pct - maker_yes_pct
    min_gap_pp = benchmarks["directional_invariant_no_gt_yes"]["expected_gap_pp_min"]

    assert maker_no_pct > maker_yes_pct, (
        f"Optimism Tax inverted: maker NO {maker_no_pct:+.4f}% NOT > maker YES "
        f"{maker_yes_pct:+.4f}%. Pipeline likely has the §5.1 price-column "
        f"inversion bug; check transform.py for `t.yes_price / 100.0 AS price` "
        f"(should be side-specific CASE)."
    )
    assert gap_pp >= min_gap_pp, (
        f"Optimism Tax gap {gap_pp:+.4f} pp below min expected {min_gap_pp} pp "
        f"(Becker reference 0.48 pp). Either pipeline drift or genuine signal "
        f"degradation — investigate before proceeding."
    )


@pytest.mark.skip(
    reason=(
        "40-48% YES-taker-share at 1-10¢ benchmark not cited in research docs "
        "on this server. See research/becker_benchmarks.json unverified_assertions[0]. "
        "Provide a primary source + add min_pct/max_pct to the JSON to enable."
    )
)
def test_assertion_3_yes_taker_share_1_to_10c_bin(trades_df, benchmarks):
    """YES taker volume share in 1-10¢ price bin (Optimism Tax magnitude proxy).

    Currently SKIPPED — the 40-48% range is not sourced from research docs.
    The computation below is the intended methodology; when the benchmark
    range is verified and added to becker_benchmarks.json, remove the @skip
    and read min_pct/max_pct from the JSON.
    """
    in_bin = (trades_df["price"] >= 0.01) & (trades_df["price"] <= 0.10)
    bin_total_volume = trades_df.loc[in_bin, "volume"].sum()
    if bin_total_volume == 0:
        pytest.fail("No trades in 1-10¢ bin — pipeline filter may be wrong.")
    yes_volume = trades_df.loc[in_bin & (trades_df["side"] == "YES"), "volume"].sum()
    share_pct = (yes_volume / bin_total_volume) * 100

    spec = benchmarks["unverified_assertions"][0]
    min_pct, max_pct = spec.get("min_pct"), spec.get("max_pct")
    assert min_pct is not None and max_pct is not None, (
        "becker_benchmarks.json unverified_assertions[0] missing min_pct/max_pct."
    )
    assert min_pct <= share_pct <= max_pct, (
        f"YES taker share in 1-10¢ bin {share_pct:.2f}% outside expected "
        f"[{min_pct}, {max_pct}]%."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
