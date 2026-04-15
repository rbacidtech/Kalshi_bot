"""
models/backtester.py — Backtest FOMC strategy against historical data.

Two modes:

1. LIVE BACKTEST (run alongside the bot):
   Reads your paper-trade CSV after each FOMC meeting resolves and
   computes: hit rate, average edge captured, fee-adjusted P&L,
   and calibration (were your 70%-confidence trades right ~70% of the time?)

2. HISTORICAL SIMULATION (run once before going live):
   Given a CSV of historical Kalshi FOMC prices + FedWatch data,
   simulates what the bot would have done and reports results.
   Use this to validate the strategy before risking real money.

Usage (live):
    from kalshi_bot.models.backtester import LiveBacktester
    bt = LiveBacktester(trades_csv=Path("output/trades.csv"))
    bt.print_report()

Usage (historical simulation):
    from kalshi_bot.models.backtester import run_historical_sim
    run_historical_sim(history_csv=Path("data/fomc_history.csv"))

Output metrics:
    - Trade count
    - Hit rate (% of trades that resolved in predicted direction)
    - Average edge at entry
    - Fee-adjusted P&L per trade (cents)
    - Calibration curve (how accurate were different confidence levels?)
    - Sharpe-like ratio (edge / std deviation of outcomes)
    - Max drawdown on the simulated trade sequence
"""

import csv
import math
import logging
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Kalshi fee per contract in cents
DEFAULT_FEE_CENTS = 7


@dataclass
class TradeRecord:
    """A single trade from the CSV log, with optional resolution data."""
    timestamp:    datetime
    ticker:       str
    side:         str         # "yes" or "no"
    contracts:    int
    price_cents:  int         # entry price
    fair_value:   float       # model's fair value at entry
    edge:         float       # edge at entry
    mode:         str         # "paper" or "live"
    order_id:     str = ""
    resolved:     bool = False
    won:          bool = False
    pnl_cents:    int  = 0    # fee-adjusted P&L in cents


@dataclass
class BacktestReport:
    """Summary statistics from a backtest run."""
    trade_count:      int   = 0
    resolved_count:   int   = 0
    hit_rate:         float = 0.0   # % of trades that resolved correctly
    avg_edge:         float = 0.0   # average edge at entry
    avg_pnl_cents:    float = 0.0   # average fee-adjusted P&L per trade
    total_pnl_cents:  int   = 0     # cumulative P&L
    fee_total_cents:  int   = 0     # total fees paid
    max_drawdown:     float = 0.0   # max peak-to-trough in running P&L
    edge_hit_buckets: dict  = field(default_factory=dict)  # calibration
    sharpe:           float = 0.0

    def print(self):
        log.info("=" * 60)
        log.info("BACKTEST REPORT")
        log.info("  Trades recorded:    %d", self.trade_count)
        log.info("  Trades resolved:    %d", self.resolved_count)
        if self.resolved_count > 0:
            log.info("  Hit rate:           %.1f%%", self.hit_rate * 100)
            log.info("  Avg edge at entry:  %.3f (%.1f¢)", self.avg_edge, self.avg_edge * 100)
            log.info("  Avg P&L per trade:  %.1f¢ (fee-adjusted)", self.avg_pnl_cents)
            log.info("  Total P&L:          $%.2f", self.total_pnl_cents / 100)
            log.info("  Total fees paid:    $%.2f", self.fee_total_cents / 100)
            log.info("  Max drawdown:       $%.2f", self.max_drawdown / 100)
            log.info("  Sharpe-like ratio:  %.2f", self.sharpe)
            if self.edge_hit_buckets:
                log.info("  Calibration (edge bucket → hit rate):")
                for bucket, stats in sorted(self.edge_hit_buckets.items()):
                    n    = stats["n"]
                    hits = stats["hits"]
                    log.info("    edge %.2f-%.2f: %d trades, hit rate %.1f%%",
                             bucket, bucket + 0.05, n, hits / n * 100 if n else 0)
        log.info("=" * 60)


class LiveBacktester:
    """
    Reads the live trade CSV and computes performance metrics
    on trades that have resolved.

    Call print_report() after each FOMC meeting resolves to see how
    the bot performed on that meeting's contracts.
    """

    def __init__(
        self,
        trades_csv: Path,
        fee_cents: int = DEFAULT_FEE_CENTS,
    ):
        self.trades_csv = trades_csv
        self.fee_cents  = fee_cents

    def load_trades(self) -> list[TradeRecord]:
        """Load all trades from the CSV file."""
        if not self.trades_csv.exists():
            log.warning("Trades CSV not found: %s", self.trades_csv)
            return []

        trades = []
        with open(self.trades_csv, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    trades.append(TradeRecord(
                        timestamp   = datetime.fromisoformat(row["timestamp"]),
                        ticker      = row["ticker"],
                        side        = row["side"],
                        contracts   = int(row["contracts"]),
                        price_cents = int(row.get("price_cents", 50)),
                        fair_value  = float(row.get("fair_value", 0.5)),
                        edge        = float(row.get("edge", 0)),
                        mode        = row.get("mode", "paper"),
                        order_id    = row.get("order_id", ""),
                    ))
                except (KeyError, ValueError) as exc:
                    log.debug("Skipping malformed row: %s", exc)

        log.info("Loaded %d trades from %s", len(trades), self.trades_csv)
        return trades

    def mark_resolutions(
        self,
        trades: list[TradeRecord],
        resolutions: dict[str, bool],
    ) -> list[TradeRecord]:
        """
        Mark trades as won or lost based on resolution data.

        Args:
            trades:      List of TradeRecord objects
            resolutions: Dict of ticker → True (YES won) / False (NO won)
                         Populate this manually after each FOMC meeting:
                         e.g. {"FOMC-25JUN18-HOLD": True, "FOMC-25JUN18-CUT25": False}

        Returns:
            Updated list with resolved=True and pnl_cents set.
        """
        for trade in trades:
            if trade.ticker not in resolutions:
                continue

            yes_won      = resolutions[trade.ticker]
            trade.won    = (trade.side == "yes" and yes_won) or \
                           (trade.side == "no"  and not yes_won)
            trade.resolved = True

            if trade.won:
                # Profit: net winnings after 7% Kalshi fee on net winnings only
                gross_win       = (100 - trade.price_cents) * trade.contracts
                fee             = int(gross_win * self.fee_cents / 100)
                trade.pnl_cents = gross_win - fee
            else:
                # Loss: only the cost of contracts — no fee charged on losing side
                trade.pnl_cents = -(trade.price_cents * trade.contracts)

        return trades

    def compute_report(self, trades: list[TradeRecord]) -> BacktestReport:
        """Compute summary statistics from a list of (partially) resolved trades."""
        report   = BacktestReport()
        resolved = [t for t in trades if t.resolved]

        report.trade_count    = len(trades)
        report.resolved_count = len(resolved)

        if not resolved:
            return report

        wins         = [t for t in resolved if t.won]
        report.hit_rate     = len(wins) / max(len(resolved), 1)
        report.avg_edge     = sum(t.edge for t in resolved) / max(len(resolved), 1)
        report.total_pnl_cents = sum(t.pnl_cents for t in resolved)
        report.avg_pnl_cents   = report.total_pnl_cents / max(len(resolved), 1)
        # Fee is only on winning trades: 7% of gross win
        report.fee_total_cents = sum(
            int((100 - t.price_cents) * t.contracts * self.fee_cents / 100)
            for t in resolved if t.won
        )

        # Max drawdown
        running = 0
        peak    = 0
        max_dd  = 0
        for t in sorted(resolved, key=lambda x: x.timestamp):
            running += t.pnl_cents
            peak     = max(peak, running)
            max_dd   = max(max_dd, peak - running)
        report.max_drawdown = max_dd

        # Sharpe-like ratio
        pnls = [t.pnl_cents for t in resolved]
        if len(pnls) > 1:
            mean_pnl = sum(pnls) / max(len(pnls), 1)
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / max(len(pnls), 1)
            std_pnl  = math.sqrt(variance) if variance > 0 else 1
            report.sharpe = mean_pnl / std_pnl

        # Calibration: group by edge bucket, measure hit rate per bucket
        buckets: dict[float, dict] = defaultdict(lambda: {"n": 0, "hits": 0})
        for t in resolved:
            bucket = round(math.floor(t.edge / 0.05) * 0.05, 2)
            buckets[bucket]["n"]    += 1
            buckets[bucket]["hits"] += int(t.won)
        report.edge_hit_buckets = dict(buckets)

        return report

    def print_report(self, resolutions: dict[str, bool] = None):
        """Load trades, optionally mark resolutions, and print the report."""
        trades = self.load_trades()
        if resolutions:
            trades = self.mark_resolutions(trades, resolutions)
        report = self.compute_report(trades)
        report.print()
        return report


class HistoricalSimulator:
    """
    Simulates the bot against historical FOMC + Kalshi price data.

    Input CSV format (data/fomc_history.csv):
        date, ticker, fedwatch_prob, kalshi_price, resolution
        2024-09-18, FOMC-24SEP18-HOLD, 0.82, 0.75, 1
        2024-09-18, FOMC-24SEP18-CUT25, 0.15, 0.22, 0
        ...

    resolution: 1 = YES won, 0 = NO won

    Usage:
        sim = HistoricalSimulator(
            history_csv  = Path("data/fomc_history.csv"),
            edge_threshold = 0.10,
            fee_cents      = 7,
        )
        report = sim.run()
        report.print()
    """

    def __init__(
        self,
        history_csv: Path,
        edge_threshold: float = 0.10,
        fee_cents: int = DEFAULT_FEE_CENTS,
        max_contracts: int = 10,
    ):
        self.history_csv    = history_csv
        self.edge_threshold = edge_threshold
        self.fee_cents      = fee_cents
        self.max_contracts  = max_contracts

    def run(self) -> BacktestReport:
        """Load historical data and simulate the strategy."""
        if not self.history_csv.exists():
            log.error("History CSV not found: %s", self.history_csv)
            log.info(
                "Create data/fomc_history.csv with columns:\n"
                "  date, ticker, fedwatch_prob, kalshi_price, resolution\n"
                "  (resolution: 1=YES won, 0=NO won)"
            )
            return BacktestReport()

        trades = []
        with open(self.history_csv, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    fw_prob    = float(row["fedwatch_prob"])
                    kalshi_p   = float(row["kalshi_price"])
                    resolution = int(row["resolution"])
                    ticker     = row["ticker"]
                    date_str   = row["date"]
                    ts         = datetime.fromisoformat(date_str).replace(
                                     tzinfo=timezone.utc)

                    diff = fw_prob - kalshi_p
                    if abs(diff) < self.edge_threshold:
                        continue

                    side       = "yes" if diff > 0 else "no"
                    edge       = abs(diff)
                    price_cents = int(kalshi_p * 100)
                    contracts   = min(self.max_contracts, max(1, int(edge * 100)))

                    yes_won = bool(resolution)
                    won     = (side == "yes" and yes_won) or (side == "no" and not yes_won)

                    if won:
                        pnl = (100 - price_cents) * contracts - self.fee_cents * contracts
                    else:
                        pnl = -(price_cents * contracts + self.fee_cents * contracts)

                    trades.append(TradeRecord(
                        timestamp   = ts,
                        ticker      = ticker,
                        side        = side,
                        contracts   = contracts,
                        price_cents = price_cents,
                        fair_value  = fw_prob,
                        edge        = edge,
                        mode        = "sim",
                        resolved    = True,
                        won         = won,
                        pnl_cents   = pnl,
                    ))

                except (KeyError, ValueError) as exc:
                    log.debug("Skipping row: %s", exc)

        log.info("Historical sim: %d trades generated from %s",
                 len(trades), self.history_csv)

        bter = LiveBacktester(self.history_csv, self.fee_cents)
        return bter.compute_report(trades)
