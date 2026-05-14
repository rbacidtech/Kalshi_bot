"""Per-strategy backtest harness for h2h_sum_to_1_arb scanner.

Replays a Becker-shaped trades parquet (one row per print, with
ticker/event_ticker/created_time/yes_price/no_price columns added by
wealth_transfer/scripts/build_trades.py) against
kalshi_bot.strategy.scan_h2h_sum_to_1_arb and aggregates the resulting
signal stream into the (annual_pnl, trade_count, mean_return_pct) tuple
the verdict doc reports per ticker prefix.

Methodology (matches EdgePulse_Backtest_Verdict_2026.md §2.2 + §3.1):
  * Filter trades to the requested ticker_prefix.
  * For each ticker (leg), keep only trades inside the verdict's
    snapshot window [close_time - window_h, close_time), and compute
    that leg's `market_yes` as the MEDIAN of yes_price/100.0 across
    those trades. (Median, not last-trade — that's what §2.2 specifies.)
  * Group legs by event_ticker. An event with exactly two priced legs
    in the window is a binary H2H event.
  * Sum the two legs' medians. If the sum is below the threshold
    (default 0.98), the event counts as ONE arb entry whose gross_edge
    = 1 - sum.
  * Aggregate: signal count, mean gross_edge × 100 = mean_return_pct,
    sum(gross_edge) ≈ realized P&L per contract pair.

The window length is configurable (default 6 hours, per verdict §3.1)
because the verdict doc also reports robustness checks at T-12h and
T-24h (verdict §2.5).

This module is the harness the `pytest.skip` at
tests/test_per_strategy_parity.py:144-151 calls out as the missing
piece.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# Make kalshi_bot importable when running outside of a pytest invocation
# from the EdgePulse root.
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ep_config  # noqa: E402  -- must come before kalshi_bot
from kalshi_bot import strategy as ks  # noqa: E402


_H2H_THRESHOLD = ks._H2H_DEFAULT_SUM_THRESHOLD  # 0.98


@dataclass
class BacktestResult:
    prefix: str
    n_trades_input: int        # raw trade rows for this prefix
    n_events: int              # events with exactly 2 legs
    signal_count: int          # arb entries emitted
    total_gross_edge: float    # sum of (1 - sum) across signals — $ per contract
    mean_gross_edge_pct: float # arithmetic mean of gross_edge × 100


def backtest_h2h_sum_to_1(
    trades: pd.DataFrame,
    ticker_prefix: str,
    threshold: float = _H2H_THRESHOLD,
    snapshot_horizon_hours: float = 6.0,
) -> BacktestResult:
    """Replay scan_h2h_sum_to_1_arb against the slice of trades whose
    ticker starts with ``ticker_prefix``.

    Args:
        trades:                  DataFrame with ticker, event_ticker,
                                 created_time, yes_price, close_time
                                 columns. Typically Becker's
                                 wealth_transfer/data/trades.parquet.
        ticker_prefix:           One of kalshi_bot.strategy._H2H_SUM_TO_1_PREFIXES
                                 (or any sport ticker prefix to dry-run on).
        threshold:               Sum-of-asks fire threshold; matches the
                                 scanner default 0.98 unless overridden.
        snapshot_horizon_hours:  Hours before close_time to snapshot at.
                                 Verdict §3.1 default is T-6h.
    """
    rel = trades[trades["ticker"].str.startswith(ticker_prefix)]
    if rel.empty:
        return BacktestResult(ticker_prefix, 0, 0, 0, 0.0, 0.0)

    rel = rel[["event_ticker", "ticker", "created_time", "yes_price", "close_time"]]

    # Verdict §2.2 window: trades in [close - window_h, close).
    horizon = pd.Timedelta(hours=snapshot_horizon_hours)
    window_start = rel["close_time"] - horizon
    in_window = (rel["created_time"] >= window_start) & (rel["created_time"] < rel["close_time"])
    rel = rel.loc[in_window]
    if rel.empty:
        return BacktestResult(ticker_prefix, 0, 0, 0, 0.0, 0.0)

    # Median yes_price per leg over the window (cents).
    per_leg = (
        rel.groupby(["event_ticker", "ticker"], observed=True)["yes_price"]
        .median()
        .reset_index()
    )

    # Keep only 2-leg events.
    leg_counts = per_leg.groupby("event_ticker", observed=True).size()
    binary_events = leg_counts[leg_counts == 2].index
    per_leg = per_leg[per_leg["event_ticker"].isin(binary_events)]

    # Sum the two legs' medians per event.
    sums = per_leg.groupby("event_ticker", observed=True)["yes_price"].sum()

    threshold_cents = threshold * 100
    arb_sums = sums[sums < threshold_cents]
    n_signals = int(len(arb_sums))
    total_gross_edge = float(((threshold_cents * 0 + (100 - arb_sums)).sum()) / 100.0)
    mean_gross_edge_pct = (
        (total_gross_edge / n_signals) * 100.0 if n_signals else 0.0
    )

    return BacktestResult(
        prefix=ticker_prefix,
        n_trades_input=int(len(rel)),
        n_events=int(len(binary_events)),
        signal_count=n_signals,
        total_gross_edge=total_gross_edge,
        mean_gross_edge_pct=mean_gross_edge_pct,
    )


def main() -> None:
    """CLI entry — run all H2H prefixes against the Becker parquet and
    print a per-prefix table for visual comparison against the verdict
    benchmarks in research/becker_benchmarks.json."""
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--trades",
        default=os.environ.get("WEALTH_TRANSFER_ROOT", "/root/wealth_transfer")
        + "/data/trades.parquet",
    )
    ap.add_argument(
        "--benchmarks",
        default=str(_HERE / "research" / "becker_benchmarks.json"),
    )
    args = ap.parse_args()

    print(f"loading trades from {args.trades}...", flush=True)
    trades = pd.read_parquet(
        args.trades,
        columns=["ticker", "event_ticker", "created_time", "yes_price", "close_time"],
    )
    print(f"  {len(trades):,} rows loaded", flush=True)

    with open(args.benchmarks) as f:
        bench = json.load(f)["per_strategy"]

    rows = []
    for key, spec in bench.items():
        if not key.startswith("h2h_sum_to_1_"):
            continue
        prefix = spec["ticker_prefix"]
        r = backtest_h2h_sum_to_1(trades, prefix)
        verdict_pnl = spec["annual_pnl_usd"]
        verdict_count = spec["trade_count"]
        verdict_mean = spec["mean_return_pct"]
        rows.append({
            "prefix": prefix,
            "verdict_count": verdict_count,
            "harness_count": r.signal_count,
            "count_drift_pct": (
                (r.signal_count - verdict_count) / verdict_count * 100
                if verdict_count else float("nan")
            ),
            "verdict_mean_pct": verdict_mean,
            "harness_mean_pct": r.mean_gross_edge_pct,
            "verdict_pnl_usd": verdict_pnl,
            "harness_gross_edge_usd": r.total_gross_edge,
            "n_input_trades": r.n_trades_input,
            "n_2leg_events": r.n_events,
        })

    df = pd.DataFrame(rows)
    print()
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
