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


def _load_completed_trades(
    csv_path: Path,
    since: datetime,
    mode: Optional[str] = None,
) -> List[dict]:
    """
    Read the trades CSV and return a list of completed-trade dicts, each
    representing a matched entry+exit pair for the same ticker.

    Only trades whose *exit* timestamp falls within [since, now] are included.
    Pass mode="live" or mode="paper" to filter by trade mode.

    Orphan exits (no matching entry, or more exits than entries) are silently
    skipped — they represent repeated exit-checker firings on a stale position
    and should not count as real trades.

    Returned dict keys:
        ticker, strategy, side, contracts,
        entry_price_cents, exit_price_cents,
        entry_ts, exit_ts,
        hold_seconds, pnl_cents, mode
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
            continue   # orphan exit — no paired entry at all

        # Sort both lists by timestamp so earliest entry pairs with earliest exit
        entry_rows_sorted = sorted(entry_rows, key=lambda r: r.get("timestamp", ""))
        exit_rows_sorted  = sorted(exit_rows,  key=lambda r: r.get("timestamp", ""))

        for i, exit_row in enumerate(exit_rows_sorted):
            # Strict 1:1 pairing — skip exits that have no matching entry.
            # Excess exits are orphans from repeated exit-checker firings on a
            # stale Redis position (e.g. positions.close() failed transiently).
            if i >= len(entry_rows_sorted):
                break

            exit_ts = _parse_ts(exit_row.get("timestamp", ""))
            if exit_ts is None or exit_ts < since:
                continue

            entry_row = entry_rows_sorted[i]
            entry_ts  = _parse_ts(entry_row.get("timestamp", ""))

            row_mode = (entry_row.get("mode") or exit_row.get("mode") or "paper").strip().lower()
            if mode is not None and row_mode != mode.lower():
                continue

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
                "mode":               row_mode,
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
    Includes separate paper/live breakdowns so the dashboard can display
    real P&L independently of paper-simulation history.
    """
    now   = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    csv_path = Path(cfg.TRADES_CSV)
    all_trades   = _load_completed_trades(csv_path, since)

    # Top-level metrics use LIVE trades only — paper history contaminates the
    # win_rate and P&L stats that drive Kelly calibration and the dashboard.
    live_trades  = [t for t in all_trades if t.get("mode") == "live"]
    paper_trades = [t for t in all_trades if t.get("mode") != "live"]
    trades = live_trades   # all downstream aggregation uses live only

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

    # -- Streaks ---------------------------------------------------------------
    streak_current = 0
    streak_best    = 0
    _cur_streak    = 0
    trades_by_time = sorted(trades, key=lambda t: t["exit_ts"])
    for t in trades_by_time:
        if t["pnl_cents"] > 0:
            _cur_streak = _cur_streak + 1 if _cur_streak >= 0 else 1
        else:
            _cur_streak = _cur_streak - 1 if _cur_streak <= 0 else -1
        if _cur_streak > streak_best:
            streak_best = _cur_streak
    streak_current = _cur_streak

    # -- Avg win / loss --------------------------------------------------------
    win_pnls  = [t["pnl_cents"] for t in trades if t["pnl_cents"] > 0]
    loss_pnls = [t["pnl_cents"] for t in trades if t["pnl_cents"] <= 0]
    avg_win_cents  = round(sum(win_pnls)  / len(win_pnls),  2) if win_pnls  else 0.0
    avg_loss_cents = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0

    # -- Expectancy (EV per trade in cents) ------------------------------------
    # expectancy = win_rate * avg_win + loss_rate * avg_loss
    loss_rate   = 1.0 - win_rate
    expectancy_cents = round(win_rate * avg_win_cents + loss_rate * avg_loss_cents, 2)

    # -- P&L distribution histogram -------------------------------------------
    buckets = [
        ("< -50\u00a2",   lambda c: c < -50),
        ("-50\u2013-20\u00a2",  lambda c: -50 <= c < -20),
        ("-20\u20130\u00a2",    lambda c: -20 <= c < 0),
        ("0\u201320\u00a2",     lambda c: 0 <= c < 20),
        ("20\u201350\u00a2",    lambda c: 20 <= c < 50),
        ("> 50\u00a2",     lambda c: c >= 50),
    ]
    pnl_distribution = []
    for label, fn in buckets:
        bucket_trades = [t for t in trades if fn(t["pnl_cents"])]
        pnl_distribution.append({
            "bucket":    label,
            "count":     len(bucket_trades),
            "pnl_cents": sum(t["pnl_cents"] for t in bucket_trades),
        })

    # Mode breakdown — live and paper totals separately
    live_pnl   = sum(t["pnl_cents"] for t in live_trades)
    live_wins  = sum(1 for t in live_trades if t["pnl_cents"] > 0)
    paper_pnl  = sum(t["pnl_cents"] for t in paper_trades)
    paper_wins = sum(1 for t in paper_trades if t["pnl_cents"] > 0)

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
        "streak_current":     streak_current,
        "streak_best":        streak_best,
        "avg_win_cents":      avg_win_cents,
        "avg_loss_cents":     avg_loss_cents,
        "expectancy_cents":   expectancy_cents,
        "pnl_distribution":   pnl_distribution,
        # Mode-separated P&L — use these for real performance assessment
        "live_trades":        len(live_trades),
        "live_wins":          live_wins,
        "live_pnl_cents":     live_pnl,
        "paper_trades":       len(paper_trades),
        "paper_wins":         paper_wins,
        "paper_pnl_cents":    paper_pnl,
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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT,
                series       TEXT,
                side         TEXT,
                contracts    INTEGER,
                entry_cents  INTEGER,
                exit_cents   INTEGER,
                pnl_cents    INTEGER,
                correct      INTEGER,
                recorded_at  TEXT,
                model_source TEXT
            )
        """)
        self._conn.commit()
        # Migrations: add columns that were added after initial deployment
        for _col, _typedef in [
            ("series",       "TEXT"),
            ("entry_cents",  "INTEGER"),
            ("exit_cents",   "INTEGER"),
            ("correct",      "INTEGER"),
            ("recorded_at",  "TEXT"),
            ("model_source", "TEXT"),
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
                              pnl_cents: int, correct: bool,
                              model_source: str = "") -> None:
        if self._conn is None:
            return
        from datetime import datetime, timezone as _tz
        try:
            self._conn.execute(
                """INSERT INTO trade_outcomes
                   (ticker, series_ticker, series, side, contracts, entry_cents,
                    exit_cents, pnl_cents, correct, recorded_at, model_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticker, series, series, side, contracts, entry_cents,
                 exit_cents, pnl_cents, 1 if correct else 0,
                 datetime.now(_tz.utc).isoformat(), model_source),
            )
            self._conn.commit()
        except Exception as exc:
            log.warning("ResolutionDB.record_trade_outcome error: %s", exc)


async def poll_resolutions_loop(
    client,
    bus,
    db: ResolutionDB,
    interval: int = 300,
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

                            # Write to ep:resolutions Redis hash so recency_bias_adj()
                            # in ep_behavioral.py has data to read. price_before is
                            # captured from ep:prices at resolution time (best available).
                            _series = ticker.split("-")[0] if "-" in ticker else ticker
                            _price_before = 50
                            try:
                                _pr = await bus._r.hget("ep:prices", ticker)
                                if _pr:
                                    _price_before = int(json.loads(_pr).get("yes_price", 50))
                            except Exception:
                                pass
                            try:
                                _cur = await bus._r.hget("ep:resolutions", _series)
                                _blob = json.loads(_cur) if _cur else {"outcomes": []}
                                _blob["outcomes"].append({
                                    "price_before": _price_before,
                                    "resolved_yes": result == "yes",
                                    "ts": int(datetime.now(timezone.utc).timestamp()),
                                })
                                _blob["outcomes"] = _blob["outcomes"][-10:]
                                await bus._r.hset("ep:resolutions", _series, json.dumps(_blob))
                                log.debug(
                                    "ep:resolutions updated: %s price_before=%d resolved_yes=%s",
                                    _series, _price_before, result == "yes",
                                )
                            except Exception as exc:
                                log.warning("ep:resolutions Redis write error for %s: %s", _series, exc)
                except Exception as exc:
                    log.debug("poll_resolutions: %s — %s", ticker, exc)
        except Exception as exc:
            log.warning("poll_resolutions_loop error: %s", exc)
        await _asyncio.sleep(interval)


# ── Advisor helper functions ───────────────────────────────────────────────────

async def get_rolling_strategy_health(
    recent_n:   int = 20,
    baseline_n: int = 50,
) -> dict:
    """
    Compare per-strategy win rate: last recent_n trades vs last baseline_n trades.

    Returns a dict keyed by strategy name:
        status:             "degrading" | "improving" | "stable" | "insufficient_data"
        recent_n:           actual trades in recent window
        baseline_n:         actual trades in baseline window
        recent_win_rate:    float or None
        baseline_win_rate:  float or None
        recent_pnl_cents:   int or None
        baseline_pnl_cents: int or None
        delta_win_rate:     float or None (positive = improving)

    Threshold: ±10pp win-rate change triggers degrading/improving.
    Requires ≥ 3 trades in both windows; otherwise "insufficient_data".
    """
    now   = datetime.now(timezone.utc)
    since = now - timedelta(days=365)
    trades = _load_completed_trades(Path(cfg.TRADES_CSV), since)

    by_strat: Dict[str, list] = defaultdict(list)
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        by_strat[t["strategy"]].append(t)

    def _stats(ts: list) -> Optional[dict]:
        if not ts:
            return None
        n    = len(ts)
        wins = sum(1 for t in ts if t["pnl_cents"] > 0)
        pnl  = sum(t["pnl_cents"] for t in ts)
        return {"n": n, "win_rate": round(wins / n, 3), "pnl_cents": pnl}

    def _span_days(ts: list) -> Optional[float]:
        """Wall-clock span of a trade slice in days, using exit_ts. Returns
        None if the slice is empty or too small to span a range."""
        if not ts or len(ts) < 2:
            return None
        first = ts[0].get("exit_ts")
        last  = ts[-1].get("exit_ts")
        if first is None or last is None:
            return None
        try:
            return round((last - first).total_seconds() / 86400, 2)
        except (TypeError, ValueError, AttributeError):
            return None

    result: dict = {}
    for strat, strat_trades in by_strat.items():
        baseline = strat_trades[-baseline_n:]
        recent   = strat_trades[-recent_n:]
        b        = _stats(baseline)
        r        = _stats(recent)

        if not b or b["n"] < 3 or not r or r["n"] < 3:
            status = "insufficient_data"
        else:
            delta = r["win_rate"] - b["win_rate"]
            if delta <= -0.10:
                status = "degrading"
            elif delta >= 0.10:
                status = "improving"
            else:
                status = "stable"

        # Days since the strategy's most recent ENTRY (i.e. signal that fired
        # and got executed). Used by the advisor's _check_escalation to skip
        # dormant strategies — historical trade performance shouldn't drive
        # current escalation if the scanner stopped firing (e.g., disabled in
        # code but old trades' exits are still streaming as positions close).
        # Must use entry_ts not exit_ts: a disabled scanner can still produce
        # recent exit_ts values for hours/days as old positions resolve.
        days_since_last_trade: Optional[float] = None
        if recent:
            entry_timestamps = [t.get("entry_ts") for t in recent if t.get("entry_ts")]
            if entry_timestamps:
                last_entry = max(entry_timestamps)
                try:
                    days_since_last_trade = round(
                        (now - last_entry).total_seconds() / 86400, 2
                    )
                except (TypeError, AttributeError):
                    pass

        result[strat] = {
            "status":             status,
            "recent_n":           len(recent),
            "baseline_n":         len(baseline),
            # Wall-clock span of the sample windows so the advisor can tell
            # whether "recent 20 trades" is 2 days (active) or 20 days (dead).
            # Added 2026-04-24 to address audit finding Adv #6.
            "recent_span_days":   _span_days(recent),
            "baseline_span_days": _span_days(baseline),
            "days_since_last_trade": days_since_last_trade,
            "recent_win_rate":    r["win_rate"]   if r else None,
            "baseline_win_rate":  b["win_rate"]   if b else None,
            "recent_pnl_cents":   r["pnl_cents"]  if r else None,
            "baseline_pnl_cents": b["pnl_cents"]  if b else None,
            "delta_win_rate":     round(r["win_rate"] - b["win_rate"], 3)
                                  if r and b else None,
        }

    return result


def get_concentration_metrics(positions: dict) -> dict:
    """
    Compute portfolio concentration from the ep:positions dict.

    Returns:
        total_exposure_cents: int
        total_exposure_usd:   float
        by_category:          {cat: {exposure_cents, count, pct}}
        by_meeting:           {meeting: {exposure_cents, count, pct}}
        largest_position:     {ticker, exposure_cents, side}
        max_category_pct:     float
        max_category_name:    str or None
    """
    def _category(ticker: str, model_source: str) -> str:
        src = (model_source or "").lower()
        if ticker == "BTC-USD":
            return "btc_spot"
        if "fomc" in src or "fed" in src or "kxfed" in src:
            return "fomc"
        if "btc" in src:
            return "btc"
        if "arb" in src:
            return "arb"
        if "gdp" in src:
            return "gdp"
        # Weather sources: NOAA, GFS, Open-Meteo (used by gfs+noaa_hourly,
        # noaa_nws+open_meteo, open_meteo). Without this branch, KXHIGHCHI/
        # KXHIGHNY/KXTEMP* positions fall through to "other" and dominate
        # the bogus-name category in concentration warnings.
        if "noaa" in src or "gfs" in src or "open_meteo" in src:
            return "weather"
        series = (ticker.split("-")[0] if "-" in ticker else ticker).upper()
        if series.startswith("KXFED"):
            return "fomc"
        if series.startswith("KXBTC"):
            return "btc"
        if series.startswith("KXGDP"):
            return "gdp"
        if series.startswith("KXHIGH") or series.startswith("KXTEMP"):
            return "weather"
        return "other"

    total_exp: int = 0
    by_cat:  Dict[str, dict] = defaultdict(lambda: {"exposure_cents": 0, "count": 0})
    by_meet: Dict[str, dict] = defaultdict(lambda: {"exposure_cents": 0, "count": 0})
    largest: dict = {"ticker": None, "exposure_cents": 0, "side": None}

    for ticker, p in positions.items():
        side        = p.get("side", "yes")
        entry_cents = int(p.get("entry_cents", 50) or 50)
        contracts   = int(p.get("contracts", 1) or 1)
        cost        = (100 - entry_cents) if side == "no" else entry_cents
        exp         = cost * contracts
        total_exp  += exp

        cat     = _category(ticker, p.get("model_source", "") or "")
        meeting = p.get("meeting") or "unknown"

        by_cat[cat]["exposure_cents"]    += exp
        by_cat[cat]["count"]             += 1
        by_meet[meeting]["exposure_cents"] += exp
        by_meet[meeting]["count"]          += 1

        if exp > largest["exposure_cents"]:
            largest = {"ticker": ticker, "exposure_cents": exp, "side": side}

    cat_result: dict  = {}
    max_cat_pct       = 0.0
    max_cat_name: Optional[str] = None
    for cat, d in by_cat.items():
        pct = round(d["exposure_cents"] / total_exp, 3) if total_exp > 0 else 0.0
        cat_result[cat] = {**d, "pct": pct}
        if pct > max_cat_pct:
            max_cat_pct  = pct
            max_cat_name = cat

    meet_result: dict = {}
    for m, d in by_meet.items():
        pct = round(d["exposure_cents"] / total_exp, 3) if total_exp > 0 else 0.0
        meet_result[m] = {**d, "pct": pct}

    return {
        "total_exposure_cents": total_exp,
        "total_exposure_usd":   round(total_exp / 100, 2),
        "by_category":          cat_result,
        "by_meeting":           meet_result,
        "largest_position":     largest,
        "max_category_pct":     round(max_cat_pct, 3),
        "max_category_name":    max_cat_name,
    }


def get_kelly_by_strategy(positions: dict, balance_cents: int) -> dict:
    """
    Return per-strategy deployed capital and implied Kelly fraction of balance.

        deployed_cents:  actual capital tied up (cost basis)
        deployed_usd:    float
        fraction:        deployed_cents / balance_cents
        position_count:  number of open positions for this strategy
    """
    by_strat: Dict[str, dict] = defaultdict(lambda: {"deployed_cents": 0, "position_count": 0})

    for ticker, p in positions.items():
        strat       = (p.get("model_source") or "unknown").strip() or "unknown"
        side        = p.get("side", "yes")
        entry_cents = int(p.get("entry_cents", 50) or 50)
        contracts   = int(p.get("contracts", 1) or 1)
        cost        = (100 - entry_cents) if side == "no" else entry_cents
        by_strat[strat]["deployed_cents"]  += cost * contracts
        by_strat[strat]["position_count"]  += 1

    result: dict = {}
    for strat, d in by_strat.items():
        frac = round(d["deployed_cents"] / balance_cents, 4) if balance_cents > 0 else 0.0
        result[strat] = {
            "deployed_cents": d["deployed_cents"],
            "deployed_usd":   round(d["deployed_cents"] / 100, 2),
            "fraction":       frac,
            "position_count": d["position_count"],
        }

    return result


# ── Threshold calibration from resolution history ─────────────────────────────

def compute_yes_entry_price_gate(
    min_trades_per_bucket: int = 10,
    bucket_size_cents:     int = 5,
    default:               float = 0.60,
) -> dict:
    """
    Compute the YES entry price threshold below which KXFED directional trades
    are unprofitable, derived from completed trade history.

    Returns a dict:
        calibrated:  float — lowest price (fraction 0–1) where EV turns positive
        default:     float — fallback value used if data is insufficient
        used_default: bool
        sample_size: int  — total KXFED YES trades analysed
        buckets:     dict — {bucket_floor_cents: {"n", "ev_cents", "win_rate"}}
        note:        str
    """
    now   = datetime.now(timezone.utc)
    since = now - timedelta(days=365)
    trades = _load_completed_trades(Path(cfg.TRADES_CSV), since)

    # Filter to KXFED YES directional trades only (not arb legs)
    kxfed_yes = [
        t for t in trades
        if t["side"] == "yes"
        and t["ticker"].startswith("KXFED")
        and "arb" not in (t["strategy"] or "").lower()
    ]

    buckets: Dict[int, list] = defaultdict(list)
    for t in kxfed_yes:
        floor = (t["entry_price_cents"] // bucket_size_cents) * bucket_size_cents
        buckets[floor].append(t["pnl_cents"])

    bucket_stats: dict = {}
    profitable_floors = []

    for floor_c in sorted(buckets):
        pnls = buckets[floor_c]
        if len(pnls) < min_trades_per_bucket:
            continue
        n      = len(pnls)
        ev     = sum(pnls) / n
        wins   = sum(1 for p in pnls if p > 0)
        wr     = round(wins / n, 3)
        bucket_stats[floor_c] = {"n": n, "ev_cents": round(ev, 1), "win_rate": wr}
        if ev > 0:
            profitable_floors.append(floor_c)

    if not profitable_floors or len(kxfed_yes) < min_trades_per_bucket * 2:
        return {
            "calibrated":  default,
            "default":     default,
            "used_default": True,
            "sample_size": len(kxfed_yes),
            "buckets":     bucket_stats,
            "note":        f"insufficient data ({len(kxfed_yes)} trades, need ≥{min_trades_per_bucket * 2})",
        }

    # The gate should be the floor of the lowest profitable bucket (as a fraction).
    # If the minimum profitable floor is ≥ 30¢ and ≤ 90¢ we trust it; else use default.
    calibrated_cents = min(profitable_floors)
    calibrated = calibrated_cents / 100.0
    if not (0.30 <= calibrated <= 0.90):
        return {
            "calibrated":  default,
            "default":     default,
            "used_default": True,
            "sample_size": len(kxfed_yes),
            "buckets":     bucket_stats,
            "note":        f"computed threshold {calibrated:.2f} outside safe range [0.30, 0.90]",
        }

    return {
        "calibrated":  calibrated,
        "default":     default,
        "used_default": False,
        "sample_size": len(kxfed_yes),
        "buckets":     bucket_stats,
        "note":        f"lowest profitable bucket: {calibrated_cents}¢",
    }


def compute_near_expiry_stop_days(
    min_trades: int   = 15,
    default:    int   = 7,
) -> dict:
    """
    Estimate the near-expiry stop-suppression window from trade outcomes.

    Method: look at KXFED trades with negative pnl (stopped or wrong direction).
    Among those, "correct_but_stopped" trades — where the model was right but
    the exit was premature — are proxied by: pnl_cents < 0 AND entry_price_cents
    between 30¢ and 70¢ (active zone where stop-loss noise is most common) AND
    hold_seconds < N * 86400.  Compare the stop-loss rate across hold-time buckets
    to find the cutoff where noise-driven exits dominate.

    Returns a dict:
        calibrated:   int — recommended suppression window in days
        default:      int — fallback
        used_default: bool
        sample_size:  int
        note:         str
    """
    now   = datetime.now(timezone.utc)
    since = now - timedelta(days=365)
    trades = _load_completed_trades(Path(cfg.TRADES_CSV), since)

    # KXFED trades in the active price zone (30–70¢) — neither near-certain nor speculative
    kxfed = [
        t for t in trades
        if t["ticker"].startswith("KXFED")
        and 30 <= t["entry_price_cents"] <= 70
        and "arb" not in (t["strategy"] or "").lower()
    ]

    if len(kxfed) < min_trades:
        return {
            "calibrated":  default,
            "default":     default,
            "used_default": True,
            "sample_size": len(kxfed),
            "note":        f"insufficient data ({len(kxfed)} trades, need ≥{min_trades})",
        }

    # Group by hold_days bucket and compute loss rate
    day_buckets: Dict[int, list] = defaultdict(list)
    for t in kxfed:
        hold_days = min(int(t["hold_seconds"] / 86400), 30)
        day_buckets[hold_days].append(t["pnl_cents"])

    # Find the hold-day threshold where the loss rate drops sharply.
    # Below this threshold, most losses are noise (suggest suppressing stops there).
    thresholds_to_check = list(range(3, 15))
    best_threshold = default

    for N in thresholds_to_check:
        short_trades = [p for d, ps in day_buckets.items() if d < N for p in ps]
        long_trades  = [p for d, ps in day_buckets.items() if d >= N for p in ps]
        if len(short_trades) < 5 or len(long_trades) < 5:
            continue
        short_loss_rate = sum(1 for p in short_trades if p < 0) / len(short_trades)
        long_loss_rate  = sum(1 for p in long_trades  if p < 0) / len(long_trades)
        # If short-hold loss rate is meaningfully worse, N is a candidate threshold
        if short_loss_rate - long_loss_rate >= 0.10:
            best_threshold = N
            break  # take the first (smallest) N that shows the improvement

    if best_threshold < 3 or best_threshold > 21:
        return {
            "calibrated":  default,
            "default":     default,
            "used_default": True,
            "sample_size": len(kxfed),
            "note":        f"computed threshold {best_threshold}d outside safe range [3, 21]",
        }

    return {
        "calibrated":  best_threshold,
        "default":     default,
        "used_default": best_threshold == default,
        "sample_size": len(kxfed),
        "note":        f"short-hold loss rate materially higher for holds <{best_threshold}d",
    }
