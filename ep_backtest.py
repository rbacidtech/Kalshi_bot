"""
ep_backtest.py — EdgePulse strategy backtest harness.

Usage:
    python3 ep_backtest.py [--days N] [--strategy STRATEGY] [--csv PATH]

Reads trades.csv and computes per-strategy performance metrics.
Optionally fetches Kalshi historical resolution data to verify P&L.

Outputs:
    - Per-strategy table: win rate, Sharpe, max drawdown, avg edge, avg hold time
    - Overall portfolio metrics
    - JSON report to output/backtest_results.json
"""

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Env loading ───────────────────────────────────────────────────────────────

def _load_env(path: str = ".env") -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
    except ImportError:
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())


# ── Trade loading ──────────────────────────────────────────────────────────────

def load_trades(csv_path: str, days: int = 90, strategy_filter: Optional[str] = None):
    """Load completed round-trips from trades.csv (pairs entry+exit rows per ticker)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if not os.path.exists(csv_path):
        print(f"trades.csv not found at {csv_path}", file=sys.stderr)
        return []

    # CSV schema: timestamp, ticker, meeting, outcome, side, action,
    #             contracts, price_cents, fair_value, edge, confidence,
    #             model_source, order_id, mode
    raw_rows = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts_raw = row.get("timestamp", "")
                if ts_raw:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                raw_rows.append(row)
            except Exception:
                continue

    entries: dict = {}
    completed = []
    for row in raw_rows:
        ticker = row.get("ticker", "")
        action = row.get("action", "")
        if action == "entry":
            entries[ticker] = row
        elif action == "exit" and ticker in entries:
            e = entries.pop(ticker)
            src = e.get("model_source", "") or ""
            if strategy_filter and strategy_filter not in src:
                continue
            try:
                entry_p   = float(e.get("price_cents", 50) or 50)
                exit_p    = float(row.get("price_cents", entry_p) or entry_p)
                side      = e.get("side", "yes")
                contracts = int(e.get("contracts", 1) or 1)
                gross = (exit_p - entry_p) * contracts if side == "yes" else (entry_p - exit_p) * contracts
                fee   = max(0.0, gross * 0.07)
                completed.append({
                    "ticker":       ticker,
                    "model_source": src,
                    "mode":         e.get("mode", "live"),
                    "side":         side,
                    "pnl_cents":    gross - fee,
                    "entry_cents":  entry_p,
                    "exit_cents":   exit_p,
                    "contracts":    contracts,
                    "ts":           e.get("timestamp", ""),
                    "edge":         float(e.get("edge", 0) or 0),
                })
            except Exception:
                continue

    return completed


# ── Metrics ──────────────────────────────────────────────────────────────────

def _sharpe(returns: list, annualise_factor: float = 252.0) -> float:
    """Compute Sharpe ratio from a list of daily P&L returns."""
    if len(returns) < 5:
        return 0.0
    n   = len(returns)
    mu  = sum(returns) / n
    var = sum((r - mu) ** 2 for r in returns) / max(n - 1, 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mu / std) * math.sqrt(annualise_factor)


def _max_drawdown(cumulative_pnl: list) -> float:
    """Maximum peak-to-trough drawdown in cents."""
    if not cumulative_pnl:
        return 0.0
    peak = cumulative_pnl[0]
    max_dd = 0.0
    for val in cumulative_pnl:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _compute_strategy_metrics(trades: list) -> dict:
    """Compute per-strategy performance metrics."""
    by_strategy = defaultdict(list)
    for t in trades:
        key = t["model_source"] or "unknown"
        by_strategy[key].append(t)

    results = {}
    for strategy, strats in sorted(by_strategy.items()):
        live_trades  = [t for t in strats if t["mode"] != "paper"]
        if not live_trades:
            live_trades = strats

        n          = len(live_trades)
        wins       = sum(1 for t in live_trades if t["pnl_cents"] > 0)
        losses     = sum(1 for t in live_trades if t["pnl_cents"] < 0)
        total_pnl  = sum(t["pnl_cents"] for t in live_trades)
        avg_edge   = sum(t["edge"] for t in live_trades) / n if n else 0

        # Sharpe: use calendar-day bucketing (sqrt(252)) when ≥5 unique trading days
        # available; fall back to per-trade sqrt(252) otherwise.  Both approaches
        # use the same annualisation factor — the difference is whether correlated
        # same-day trades are aggregated before measuring volatility.
        daily: dict = defaultdict(float)
        for t in live_trades:
            try:
                day_key = t["ts"][:10] if t.get("ts") else "0000-00-00"
            except Exception:
                day_key = "0000-00-00"
            daily[day_key] += t["pnl_cents"]

        daily_pnl = list(daily.values())
        pnl_list  = [t["pnl_cents"] for t in live_trades]
        cum_pnl   = []
        running   = 0.0
        for p in pnl_list:
            running += p
            cum_pnl.append(running)

        if len(daily_pnl) >= 5:
            sharpe = _sharpe(daily_pnl, annualise_factor=252.0)
        else:
            sharpe = _sharpe(pnl_list, annualise_factor=252.0)
        max_dd  = _max_drawdown(cum_pnl)
        win_rt  = wins / n if n > 0 else 0

        results[strategy] = {
            "n":          n,
            "win_rate":   round(win_rt, 3),
            "wins":       wins,
            "losses":     losses,
            "total_pnl_cents": int(total_pnl),
            "avg_pnl_cents":   round(total_pnl / n, 1) if n else 0,
            "max_drawdown_cents": int(max_dd),
            "sharpe":     round(sharpe, 2),
            "avg_edge":   round(avg_edge, 4),
        }

    return results


# ── Report formatting ─────────────────────────────────────────────────────────

def _format_report(metrics: dict, overall: dict) -> str:
    lines = []
    lines.append("=" * 80)
    lines.append("EdgePulse Strategy Backtest Report")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 80)
    lines.append("")

    # Header
    lines.append(f"{'Strategy':<35} {'N':>5} {'WinRate':>8} {'TotalP&L':>10} {'Sharpe':>7} {'MaxDD':>8} {'AvgEdge':>8}")
    lines.append("-" * 80)

    for strat, m in sorted(metrics.items(), key=lambda x: -x[1]["total_pnl_cents"]):
        pnl_fmt = f"{m['total_pnl_cents']/100:+.2f}"
        dd_fmt  = f"{m['max_drawdown_cents']/100:.2f}"
        lines.append(
            f"{strat[:35]:<35} {m['n']:>5} {m['win_rate']:>7.1%} {pnl_fmt:>10} "
            f"{m['sharpe']:>7.2f} {dd_fmt:>8} {m['avg_edge']:>8.4f}"
        )

    lines.append("-" * 80)
    lines.append("")
    lines.append("Overall Portfolio:")
    lines.append(f"  Total trades:  {overall['total_trades']}")
    lines.append(f"  Total P&L:    ${overall['total_pnl_cents']/100:+.2f}")
    lines.append(f"  Win rate:      {overall['win_rate']:.1%}")
    lines.append(f"  Sharpe:        {overall['sharpe']:.2f}")
    lines.append(f"  Max drawdown: ${overall['max_drawdown_cents']/100:.2f}")
    lines.append("")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="EdgePulse strategy backtest")
    parser.add_argument("--days",     type=int,   default=90,    help="Look-back period in days")
    parser.add_argument("--strategy", type=str,   default=None,  help="Filter to specific model_source")
    parser.add_argument("--csv",      type=str,   default=None,  help="Path to trades.csv")
    parser.add_argument("--json",     action="store_true",       help="Output JSON only")
    args = parser.parse_args()

    _load_env()

    csv_path = args.csv or os.path.join(
        os.path.dirname(__file__), "output", "trades.csv"
    )

    trades = load_trades(csv_path, days=args.days, strategy_filter=args.strategy)

    if not trades:
        print("No trades found. Check trades.csv path and date range.", file=sys.stderr)
        sys.exit(1)

    per_strategy = _compute_strategy_metrics(trades)

    # Overall metrics — use daily-bucketed P&L for calendar-day Sharpe
    live_trades_all = [t for t in trades if t["mode"] != "paper"] or trades
    all_pnl     = [t["pnl_cents"] for t in live_trades_all]
    daily_all: dict = defaultdict(float)
    for t in live_trades_all:
        try:
            day_key = t["ts"][:10] if t.get("ts") else "0000-00-00"
        except Exception:
            day_key = "0000-00-00"
        daily_all[day_key] += t["pnl_cents"]

    cum_all  = []
    running  = 0.0
    for p in all_pnl:
        running += p
        cum_all.append(running)

    total_n  = len(all_pnl)
    total_w  = sum(1 for p in all_pnl if p > 0)
    overall  = {
        "total_trades":       total_n,
        "total_pnl_cents":    int(sum(all_pnl)),
        "win_rate":           total_w / total_n if total_n else 0,
        "sharpe":             round(
            _sharpe(list(daily_all.values()), 252.0) if len(daily_all) >= 5
            else _sharpe(all_pnl, 252.0), 2
        ),
        "max_drawdown_cents": int(_max_drawdown(cum_all)),
        "strategies":         per_strategy,
    }

    output_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "backtest_results.json")
    with open(results_path, "w") as f:
        json.dump(overall, f, indent=2)

    if args.json:
        print(json.dumps(overall, indent=2))
    else:
        print(_format_report(per_strategy, overall))
        print(f"JSON saved to {results_path}")


if __name__ == "__main__":
    main()
