"""
ep_resolution_db.py — Performance analytics derived from the trades CSV.

The "resolution DB" is the trades.csv file written by kalshi_bot.executor.
Each completed trade consists of a paired entry + exit row for the same ticker.

CSV columns (from executor.py CSV_HEADERS):
    timestamp, ticker, meeting, outcome, side, action,
    contracts, price_cents, fair_value, edge,
    confidence, model_source, order_id, mode

P&L per trade:
    YES side:  (exit_price_cents - entry_price_cents) * contracts
    NO  side:  (entry_price_cents - exit_price_cents) * contracts

Usage:
    # From code (async context):
    summary = await get_performance_summary(days=30)

    # CLI one-liner:
    python3 -c "import asyncio; from ep_resolution_db import print_performance_report; asyncio.run(print_performance_report())"
"""

import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ep_config must be imported first (sets sys.path for kalshi_bot)
from ep_config import cfg, REDIS_URL, log


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into an aware UTC datetime."""
    if not ts_str:
        return None
    try:
        # Python ≥3.7 handles most ISO forms; replace Z for compatibility
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_completed_trades(csv_path: Path, since: datetime) -> List[dict]:
    """
    Read the trades CSV and return a list of completed-trade dicts, each
    representing a matched entry+exit pair for the same ticker.

    Only trades whose *exit* timestamp falls within [since, now] are included.

    Returned dict keys:
        ticker, strategy, side, contracts,
        entry_price_cents, exit_price_cents,
        entry_ts, exit_ts,
        hold_seconds, pnl_cents
    """
    if not csv_path.exists():
        log.debug("Trades CSV not found at %s — returning empty list.", csv_path)
        return []

    # -- Read all rows, bucket by ticker into entry / exit lists ----------------
    entries: Dict[str, List[dict]] = defaultdict(list)
    exits:   Dict[str, List[dict]] = defaultdict(list)

    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                action = row.get("action", "").strip().lower()
                ticker = row.get("ticker", "").strip()
                if not ticker:
                    continue
                if action == "entry":
                    entries[ticker].append(row)
                elif action == "exit":
                    exits[ticker].append(row)
    except Exception as exc:
        log.warning("Could not read trades CSV: %s", exc)
        return []

    # -- Match entry → exit chronologically ------------------------------------
    completed: List[dict] = []

    for ticker, exit_rows in exits.items():
        entry_rows = entries.get(ticker, [])
        if not entry_rows:
            continue   # orphan exit — no paired entry

        # Sort both lists by timestamp so earliest entry pairs with earliest exit
        entry_rows_sorted = sorted(entry_rows, key=lambda r: r.get("timestamp", ""))
        exit_rows_sorted  = sorted(exit_rows,  key=lambda r: r.get("timestamp", ""))

        for i, exit_row in enumerate(exit_rows_sorted):
            exit_ts = _parse_ts(exit_row.get("timestamp", ""))
            if exit_ts is None or exit_ts < since:
                continue

            # Use the matching entry row (same index if available, else first)
            entry_row = entry_rows_sorted[i] if i < len(entry_rows_sorted) else entry_rows_sorted[0]
            entry_ts  = _parse_ts(entry_row.get("timestamp", ""))

            try:
                entry_price_cents = int(entry_row.get("price_cents", 0) or 0)
                exit_price_cents  = int(exit_row.get("price_cents", 0) or 0)
                contracts         = int(entry_row.get("contracts", 1) or 1)
                side              = entry_row.get("side", "yes").strip().lower()
                model_source      = (
                    entry_row.get("model_source", "").strip()
                    or exit_row.get("model_source", "").strip()
                    or "unknown"
                )
            except (ValueError, TypeError):
                continue

            # P&L: profit when price moves in our favour
            if side == "yes":
                pnl_cents = (exit_price_cents - entry_price_cents) * contracts
            else:
                pnl_cents = (entry_price_cents - exit_price_cents) * contracts

            hold_seconds = (
                (exit_ts - entry_ts).total_seconds()
                if entry_ts is not None
                else 0.0
            )

            completed.append({
                "ticker":             ticker,
                "strategy":           model_source,
                "side":               side,
                "contracts":          contracts,
                "entry_price_cents":  entry_price_cents,
                "exit_price_cents":   exit_price_cents,
                "entry_ts":           entry_ts,
                "exit_ts":            exit_ts,
                "hold_seconds":       hold_seconds,
                "pnl_cents":          pnl_cents,
            })

    return completed


def _compute_sharpe(trades: List[dict]) -> Optional[float]:
    """
    Annualised Sharpe ratio from daily P&L buckets.

    - Groups completed trades by UTC calendar day (exit date).
    - Requires ≥ 5 distinct trading days; returns None otherwise.
    - Annualises by sqrt(252) (trading-day convention).
    """
    if not trades:
        return None

    daily: Dict[str, int] = defaultdict(int)
    for t in trades:
        day_key = t["exit_ts"].strftime("%Y-%m-%d")
        daily[day_key] += t["pnl_cents"]

    if len(daily) < 5:
        return None

    values = list(daily.values())
    n      = len(values)
    mean   = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    std    = math.sqrt(variance)

    if std == 0:
        return None

    return round((mean / std) * math.sqrt(252), 4)


# ── Public API ────────────────────────────────────────────────────────────────

async def get_performance_summary(days: int = 30) -> dict:
    """
    Return a performance summary dict for the last `days` calendar days.

    All monetary values are in **cents** (100 cents = $1.00).

    The function is async for consistency with the rest of the async codebase;
    the CSV I/O is synchronous and fast enough not to need executor offloading
    for the file sizes expected in practice.
    """
    now   = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    csv_path = Path(cfg.TRADES_CSV)
    trades   = _load_completed_trades(csv_path, since)

    # -- Aggregate totals ------------------------------------------------------
    total_trades = len(trades)
    wins         = sum(1 for t in trades if t["pnl_cents"] > 0)
    losses       = sum(1 for t in trades if t["pnl_cents"] <= 0)
    total_pnl    = sum(t["pnl_cents"] for t in trades)
    avg_pnl      = round(total_pnl / total_trades, 2) if total_trades else 0.0
    win_rate     = round(wins / total_trades, 4) if total_trades else 0.0

    # -- By strategy -----------------------------------------------------------
    by_strategy: Dict[str, dict] = {}
    for t in trades:
        s = t["strategy"]
        if s not in by_strategy:
            by_strategy[s] = {"trades": 0, "wins": 0, "pnl_cents": 0}
        by_strategy[s]["trades"]    += 1
        by_strategy[s]["pnl_cents"] += t["pnl_cents"]
        if t["pnl_cents"] > 0:
            by_strategy[s]["wins"] += 1

    # -- Best / worst trades ---------------------------------------------------
    best_trade  = None
    worst_trade = None
    if trades:
        best  = max(trades, key=lambda t: t["pnl_cents"])
        worst = min(trades, key=lambda t: t["pnl_cents"])
        best_trade  = {"ticker": best["ticker"],  "pnl_cents": best["pnl_cents"],  "strategy": best["strategy"]}
        worst_trade = {"ticker": worst["ticker"], "pnl_cents": worst["pnl_cents"], "strategy": worst["strategy"]}

    # -- Hold time -------------------------------------------------------------
    hold_times = [t["hold_seconds"] for t in trades if t["hold_seconds"] > 0]
    avg_hold_hours = round(sum(hold_times) / len(hold_times) / 3600, 2) if hold_times else 0.0

    # -- Sharpe ----------------------------------------------------------------
    sharpe = _compute_sharpe(trades)

    return {
        "period_days":        days,
        "total_trades":       total_trades,
        "wins":               wins,
        "losses":             losses,
        "win_rate":           win_rate,
        "total_pnl_cents":    total_pnl,
        "avg_pnl_per_trade":  avg_pnl,
        "by_strategy":        by_strategy,
        "best_trade":         best_trade,
        "worst_trade":        worst_trade,
        "avg_hold_time_hours": avg_hold_hours,
        "sharpe_daily":       sharpe,
    }


# ── CLI report ────────────────────────────────────────────────────────────────

async def print_performance_report(days: int = 30) -> None:
    """
    Print a human-readable performance report to stdout.

    Usage:
        python3 -c "import asyncio; from ep_resolution_db import print_performance_report; asyncio.run(print_performance_report())"

    Override the lookback window:
        python3 -c "import asyncio; from ep_resolution_db import print_performance_report; asyncio.run(print_performance_report(days=7))"
    """
    s = await get_performance_summary(days=days)

    win_pct    = s["win_rate"] * 100
    pnl_dollar = s["total_pnl_cents"] / 100
    avg_dollar = s["avg_pnl_per_trade"] / 100
    sharpe_str = f"{s['sharpe_daily']:.3f}" if s["sharpe_daily"] is not None else "N/A (<5 days)"

    print()
    print("=" * 60)
    print(f"  EdgePulse Performance Report  —  last {s['period_days']} days")
    print("=" * 60)
    print(f"  Trades      : {s['total_trades']}")
    print(f"  Win / Loss  : {s['wins']} / {s['losses']}  ({win_pct:.1f}%)")
    print(f"  Total P&L   : {pnl_dollar:+.2f}$  ({s['total_pnl_cents']:+d}¢)")
    print(f"  Avg P&L     : {avg_dollar:+.2f}$  per trade")
    print(f"  Avg hold    : {s['avg_hold_time_hours']:.1f} hours")
    print(f"  Sharpe      : {sharpe_str}")
    print()

    if s["by_strategy"]:
        print("  By strategy:")
        for strat, st in sorted(s["by_strategy"].items(),
                                key=lambda kv: kv[1]["pnl_cents"], reverse=True):
            st_pnl  = st["pnl_cents"] / 100
            st_wr   = (st["wins"] / st["trades"] * 100) if st["trades"] else 0.0
            print(f"    {strat:<35s}  {st['trades']:3d} trades  "
                  f"{st_wr:5.1f}% win  {st_pnl:+7.2f}$")
        print()

    if s["best_trade"]:
        bt = s["best_trade"]
        wt = s["worst_trade"]
        print(f"  Best trade  : {bt['ticker']}  {bt['pnl_cents']:+d}¢  ({bt['strategy']})")
        print(f"  Worst trade : {wt['ticker']}  {wt['pnl_cents']:+d}¢  ({wt['strategy']})")
        print()

    print("=" * 60)
    print()


# ── ResolutionDB — SQLite store for market outcomes ───────────────────────────

import sqlite3
import asyncio as _asyncio

class ResolutionDB:
    """
    SQLite-backed store for Kalshi market resolution outcomes and trade records.
    Used by ep_exec.py to detect resolved markets and record completed trades.
    """

    def __init__(self, db_path: str = "output/resolutions.db"):
        self._path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS resolutions (
                ticker      TEXT PRIMARY KEY,
                outcome     TEXT,
                resolved_at TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_outcomes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT,
                series      TEXT,
                side        TEXT,
                contracts   INTEGER,
                entry_cents INTEGER,
                exit_cents  INTEGER,
                pnl_cents   INTEGER,
                correct     INTEGER,
                recorded_at TEXT
            )
        """)
        self._conn.commit()
        # Migrations: add columns that were added after initial deployment
        for _col, _typedef in [
            ("series",      "TEXT"),
            ("entry_cents", "INTEGER"),
            ("exit_cents",  "INTEGER"),
            ("correct",     "INTEGER"),
        ]:
            try:
                self._conn.execute(
                    f"ALTER TABLE trade_outcomes ADD COLUMN {_col} {_typedef}"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        log.info("ResolutionDB initialised at %s", self._path)

    def get_outcome(self, ticker: str) -> Optional[str]:
        """Return 'yes', 'no', or None for a resolved market."""
        if self._conn is None:
            return None
        try:
            cur = self._conn.execute(
                "SELECT outcome FROM resolutions WHERE ticker = ?", (ticker,)
            )
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def record_resolution(self, ticker: str, outcome: str) -> None:
        if self._conn is None:
            return
        from datetime import datetime, timezone as _tz
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO resolutions (ticker, outcome, resolved_at) VALUES (?, ?, ?)",
                (ticker, outcome, datetime.now(_tz.utc).isoformat()),
            )
            self._conn.commit()
        except Exception as exc:
            log.warning("ResolutionDB.record_resolution error: %s", exc)

    def record_trade_outcome(self, *, ticker: str, series: str, side: str,
                              contracts: int, entry_cents: int, exit_cents: int,
                              pnl_cents: int, correct: bool) -> None:
        if self._conn is None:
            return
        from datetime import datetime, timezone as _tz
        try:
            self._conn.execute(
                """INSERT INTO trade_outcomes
                   (ticker, series, side, contracts, entry_cents, exit_cents,
                    pnl_cents, correct, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticker, series, side, contracts, entry_cents, exit_cents,
                 pnl_cents, 1 if correct else 0, datetime.now(_tz.utc).isoformat()),
            )
            self._conn.commit()
        except Exception as exc:
            log.warning("ResolutionDB.record_trade_outcome error: %s", exc)


async def poll_resolutions_loop(
    client,
    bus,
    db: ResolutionDB,
    interval: int = 3600,
) -> None:
    """
    Periodically poll open positions against the Kalshi API to detect resolved
    markets and store outcomes in ResolutionDB.
    """
    log.info("Resolution poller started (interval=%ds)", interval)
    while True:
        try:
            positions = await bus.get_all_positions()
            for ticker in list(positions.keys()):
                try:
                    loop = _asyncio.get_event_loop()
                    resp = await loop.run_in_executor(
                        None,
                        lambda t=ticker: client.get(f"/markets/{t}"),
                    )
                    market_data = (resp or {}).get("market", {}) if isinstance(resp, dict) else {}
                    status = market_data.get("status", "")
                    result = market_data.get("result", "")
                    if status == "resolved" and result in ("yes", "no"):
                        existing = db.get_outcome(ticker)
                        if existing != result:
                            db.record_resolution(ticker, result)
                            log.info("Resolution: %s → %s", ticker, result)
                except Exception as exc:
                    log.debug("poll_resolutions: %s — %s", ticker, exc)
        except Exception as exc:
            log.warning("poll_resolutions_loop error: %s", exc)
        await _asyncio.sleep(interval)
