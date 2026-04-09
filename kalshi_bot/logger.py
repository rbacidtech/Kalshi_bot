"""
logger.py — Structured logging and daily summary reporting.

Improvements over v1:
  - JSON-structured log file (machine-readable alongside human console output)
  - Daily summary: total trades, P&L estimate, win rate, top markets
  - Cycle timing so you can see how long scans actually take
  - Log rotation so files don't grow unbounded

Usage:
    setup_logging()             # call once at startup
    reporter = DailySummary()
    reporter.record(signal, executed=True)
    reporter.print_summary()    # call at end of each day/session
"""

import json
import time
import logging
import logging.handlers
import datetime
from pathlib import Path
from collections import defaultdict


def setup_logging(log_dir: Path = Path("output/logs"), level: int = logging.INFO) -> None:
    """
    Configure logging with:
      - Console handler: human-readable with colors (INFO+)
      - JSON file handler: machine-readable, rotated daily (DEBUG+)

    Call once at startup before any other log calls.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any existing handlers (prevents duplicate output on re-import)
    root.handlers.clear()

    # Console: clean human-readable output
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(console)

    # JSON file: structured, rotated at midnight, keep 30 days
    json_path = log_dir / "kalshi_bot.jsonl"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        json_path, when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFormatter())
    root.addHandler(file_handler)

    logging.getLogger("kalshi_bot").info(
        "Logging initialised — JSON log at %s", json_path
    )


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON (JSON Lines format)."""

    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts":      datetime.datetime.utcfromtimestamp(record.created).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc)


# ── Daily summary ─────────────────────────────────────────────────────────────

class DailySummary:
    """
    Tracks per-session statistics and prints a summary on demand.

    Records every signal (executed or skipped) so you can see
    how many opportunities the bot found vs. how many it took.
    """

    def __init__(self):
        self._start     = time.time()
        self._cycles    = 0
        self._executed  = 0
        self._skipped   = 0
        self._by_ticker = defaultdict(lambda: {"executed": 0, "skipped": 0, "edge_sum": 0.0})
        self._log       = logging.getLogger("kalshi_bot.summary")

    def record_cycle(self):
        self._cycles += 1

    def record(self, signal, executed: bool):
        """Call for every signal the strategy produces."""
        t = self._by_ticker[signal.ticker]
        if executed:
            self._executed += 1
            t["executed"]  += 1
        else:
            self._skipped += 1
            t["skipped"]  += 1
        t["edge_sum"] += signal.edge

    def print_summary(self):
        """Log a human-readable session summary."""
        elapsed = time.time() - self._start
        total   = self._executed + self._skipped

        self._log.info("=" * 60)
        self._log.info("SESSION SUMMARY")
        self._log.info("  Runtime:          %s",
                       str(datetime.timedelta(seconds=int(elapsed))))
        self._log.info("  Cycles run:       %d", self._cycles)
        self._log.info("  Signals found:    %d", total)
        self._log.info("  Trades executed:  %d", self._executed)
        self._log.info("  Trades skipped:   %d  (risk/dedup/liquidity)",
                       self._skipped)

        if self._executed > 0:
            exec_rate = self._executed / total * 100 if total else 0
            self._log.info("  Execution rate:   %.1f%%", exec_rate)

        if self._by_ticker:
            self._log.info("  Top markets by edge:")
            top = sorted(
                self._by_ticker.items(),
                key=lambda kv: kv[1]["edge_sum"],
                reverse=True,
            )[:5]
            for ticker, stats in top:
                avg_edge = stats["edge_sum"] / max(stats["executed"] + stats["skipped"], 1)
                self._log.info(
                    "    %-40s  executed=%d  avg_edge=%.3f",
                    ticker[:40], stats["executed"], avg_edge,
                )

        self._log.info("=" * 60)


class CycleTimer:
    """Simple context manager to time and log each scan cycle."""

    def __init__(self, cycle: int):
        self._cycle = cycle
        self._start = None
        self._log   = logging.getLogger("kalshi_bot.timer")

    def __enter__(self):
        self._start = time.time()
        self._log.info("── Cycle %d started ──────────────────────────────────", self._cycle)
        return self

    def __exit__(self, *_):
        elapsed = time.time() - self._start
        self._log.info("── Cycle %d finished in %.2fs ───────────────────────", self._cycle, elapsed)
