"""tests/test_per_strategy_parity.py — Engineering S.4 per-strategy parity.

Goes beyond the Becker headline benchmarks (tests/parity_test.py) to validate
**each verdict strategy individually** against its expected annual P&L,
trade count, mean return, and win rate per the verdict doc §3-§4.

The test iterates `research/becker_benchmarks.json` `per_strategy` entries
and, for each, attempts to:
  1. Locate the production scanner in kalshi_bot.strategy
  2. Run it against Becker's historical parquet (filtered to the strategy's
     ticker prefix)
  3. Compute trade_count + sum_pnl + mean_return + win_rate
  4. Assert each within tolerance (10% P&L / 1% count per Engineering S.4)

If any of the inputs is missing (scanner not implemented, Becker data
absent, ticker prefix empty in the dataset), the corresponding test
skips with a clear reason. Skip ≠ pass — the test surface tracks what's
been verified vs what's pending.

Run with: python -m pytest tests/test_per_strategy_parity.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


_BENCHMARKS = Path(__file__).resolve().parent.parent / "research" / "becker_benchmarks.json"


def _load_per_strategy() -> dict[str, dict]:
    if not _BENCHMARKS.exists():
        return {}
    with open(_BENCHMARKS) as f:
        data = json.load(f)
    return data.get("per_strategy", {}) or {}


def _tolerances() -> tuple[float, float]:
    if not _BENCHMARKS.exists():
        return 10.0, 1.0
    with open(_BENCHMARKS) as f:
        data = json.load(f)
    return (
        float(data.get("per_strategy_pnl_tolerance_pct", 10.0)),
        float(data.get("per_strategy_count_tolerance_pct", 1.0)),
    )


def _resolve_trades_parquet() -> Path | None:
    root = os.environ.get("WEALTH_TRANSFER_ROOT", "/root/wealth_transfer")
    p = Path(root) / "data" / "trades.parquet"
    return p if p.exists() else None


def _is_synthetic_data() -> bool:
    root = os.environ.get("WEALTH_TRANSFER_ROOT", "/root/wealth_transfer")
    return (Path(root) / "data" / ".synthetic").exists()


def _resolve_scanner(strategy_key: str) -> Any:
    """Map verdict_benchmarks per_strategy key → production scanner callable.

    Returns the callable or None when the scanner isn't implemented yet.
    The mapping uses the key prefix; e.g. all h2h_sum_to_1_KX* entries
    dispatch to scan_h2h_sum_to_1_arb.
    """
    try:
        from kalshi_bot import strategy as ks
    except Exception:
        return None
    if strategy_key.startswith("h2h_sum_to_1_"):
        return getattr(ks, "scan_h2h_sum_to_1_arb", None)
    # Other strategies are not yet implemented (Phase 2 work).
    return None


@pytest.fixture(scope="module")
def trades_df_per_strategy() -> pd.DataFrame | None:
    p = _resolve_trades_parquet()
    if p is None:
        return None
    return pd.read_parquet(p)


@pytest.mark.parametrize("strategy_key", list(_load_per_strategy().keys()))
def test_per_strategy_parity(strategy_key: str, trades_df_per_strategy: pd.DataFrame | None):
    """Parity check for one verdict-strategy entry.

    Skips with a clear reason when:
      - Becker data absent (no parquet at WEALTH_TRANSFER_ROOT/data/trades.parquet)
      - Synthetic-mode data (per-strategy magnitudes are not the goal of
        the synthetic generator; only signs/ordering matter)
      - Scanner not yet implemented in kalshi_bot.strategy
      - Ticker prefix has no matching rows in the dataset
    """
    spec = _load_per_strategy().get(strategy_key)
    if spec is None:
        pytest.skip(f"No spec entry for {strategy_key}")

    if trades_df_per_strategy is None:
        pytest.skip(
            f"Becker parquet not found at WEALTH_TRANSFER_ROOT — "
            f"per-strategy parity cannot run. Resolve by populating "
            f"WEALTH_TRANSFER_ROOT/data/trades.parquet (see DataPipeline.md §2)."
        )

    if _is_synthetic_data():
        pytest.skip(
            f"Synthetic data marker present — per-strategy magnitudes are "
            f"not reproducible from synthesize_trades. Drop .synthetic marker "
            f"+ real Becker parquet to enable {strategy_key}."
        )

    scanner = _resolve_scanner(strategy_key)
    if scanner is None:
        pytest.skip(
            f"Scanner for {strategy_key} not yet implemented in kalshi_bot.strategy. "
            f"Phase 2 work."
        )

    prefix = spec["ticker_prefix"]
    rel_rows = trades_df_per_strategy[
        trades_df_per_strategy["ticker"].astype(str).str.startswith(prefix)
    ]
    if rel_rows.empty:
        pytest.skip(
            f"No rows with ticker prefix {prefix} in Becker dataset — "
            f"cannot validate {strategy_key}."
        )

    # The full scanner-execution path requires a Kalshi market dict
    # representation (yes_ask_dollars, event_ticker, close_time) that
    # Becker's trades parquet doesn't directly provide. The parity-test
    # framework is in place; the per-strategy backtest harness (converting
    # trades.parquet → market snapshots → scanner input) is a follow-on
    # implementation. Skip with a clear actionable reason.
    pytest.skip(
        f"Per-strategy backtest harness for {strategy_key} is not yet "
        f"implemented. The framework checks (spec entry, scanner, data) "
        f"all pass; what's missing is the trades→market_snapshot translation "
        f"that feeds the scanner. Engineering S.4 ships in stages: "
        f"(1) THIS test infrastructure ✓, (2) translation harness, "
        f"(3) per-strategy backtest replay."
    )


def test_per_strategy_table_loaded():
    """Sanity: the per_strategy table loaded and has entries."""
    table = _load_per_strategy()
    assert len(table) > 0, "research/becker_benchmarks.json per_strategy is empty"
    assert all("ticker_prefix" in v for v in table.values()), "every entry needs ticker_prefix"
    assert all("annual_pnl_usd" in v for v in table.values()), "every entry needs annual_pnl_usd"


def test_per_strategy_tolerances_sane():
    """Sanity: tolerances are sensible (not zero, not absurd)."""
    pnl_tol, count_tol = _tolerances()
    assert 1.0 <= pnl_tol <= 25.0, f"per_strategy_pnl_tolerance_pct {pnl_tol} out of [1, 25]"
    assert 0.1 <= count_tol <= 10.0, f"per_strategy_count_tolerance_pct {count_tol} out of [0.1, 10]"
