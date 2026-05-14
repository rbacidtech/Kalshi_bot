"""Per-strategy P&L attribution — Engineering A.5.

The load-bearing measurement layer. Every downstream Tier S/A/B component
that operates on per-strategy economics reads from here:
  - S.3.3 per-strategy circuit breaker (already wired against ep:performance:7)
  - A.4 quarterly cap recalibration
  - B.4 tuning loop's Bayesian update
  - B.5 cost accounting's per-strategy net contribution

Three time horizons:
  - **Realtime intraday** — `ep:strategy_pnl_realtime` Redis hash, keyed by
    strategy. Fields: realized_cents, unrealized_cents, fees_cents,
    slippage_cents, signal_count, fill_count, settlement_count, last_update_us.
  - **Daily snapshots** — `strategy_pnl_daily` Postgres table (append-only,
    primary key (date, strategy)). Written by daily_rollover() at 00:00 UTC.
  - **Lifetime cumulative** — derived from the daily snapshots via SQL.

Realized + unrealized are kept separate. Unrealized recomputes every 5 min
using bid prices (conservative). Multi-strategy contributions on shared
positions are split proportionally by contracts contributed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)


_REALTIME_HASH = "ep:strategy_pnl_realtime"
_DAILY_TABLE = "strategy_pnl_daily"
_ROLLOVER_CHECK_INTERVAL_S = 60    # check for UTC-midnight rollover once per minute
# Unrealized recompute is invoked by callers on their own cadence (typically
# every 5 min via _business_health_loop) rather than a dedicated loop.


# ── Recording (called from fill / settlement code paths) ──────────────────────

async def record_fill(
    bus_redis: Any,
    strategy: str,
    fee_cents: int = 0,
    slippage_cents: Optional[int] = None,
) -> None:
    """Increment intraday realtime counters for a filled execution.

    Called from ep_exec.py after ExecutionReport.status == 'filled' is
    confirmed. Realized P&L is added later via record_settlement().
    """
    if not strategy:
        return
    try:
        await bus_redis.hincrby(_REALTIME_HASH, f"{strategy}:fill_count", 1)
        if fee_cents:
            await bus_redis.hincrby(_REALTIME_HASH, f"{strategy}:fees_cents", int(fee_cents))
        if slippage_cents is not None:
            await bus_redis.hincrby(_REALTIME_HASH, f"{strategy}:slippage_cents", int(slippage_cents))
        await bus_redis.hset(_REALTIME_HASH, f"{strategy}:last_update_us", str(int(time.time() * 1_000_000)))
    except Exception as exc:
        log.debug("record_fill(%s) failed: %s", strategy, exc)


async def record_signal(bus_redis: Any, strategy: str) -> None:
    """Increment signal counter when a strategy emits a signal (pre-execution)."""
    if not strategy:
        return
    try:
        await bus_redis.hincrby(_REALTIME_HASH, f"{strategy}:signal_count", 1)
    except Exception as exc:
        log.debug("record_signal(%s) failed: %s", strategy, exc)


async def record_settlement(
    bus_redis: Any,
    strategy: str,
    realized_pnl_cents: int,
) -> None:
    """Add settled P&L to the strategy's realtime accumulator.

    Called from ep_settlements.py on every settlement that closes a position.
    realized_pnl_cents is net of fees (the original fees were already
    recorded at fill time via record_fill).
    """
    if not strategy:
        return
    try:
        await bus_redis.hincrby(_REALTIME_HASH, f"{strategy}:realized_pnl_cents", int(realized_pnl_cents))
        await bus_redis.hincrby(_REALTIME_HASH, f"{strategy}:settlement_count", 1)
        await bus_redis.hset(_REALTIME_HASH, f"{strategy}:last_update_us", str(int(time.time() * 1_000_000)))
    except Exception as exc:
        log.debug("record_settlement(%s) failed: %s", strategy, exc)


# ── Unrealized P&L (periodic recompute) ───────────────────────────────────────

async def compute_unrealized_by_strategy(
    bus_redis: Any,
    positions_by_strategy: dict[str, list[dict]],
    price_lookup: dict[str, float],
) -> dict[str, int]:
    """Mark-to-market unrealized P&L per strategy using **bid prices**.

    `positions_by_strategy`: {strategy: [position_dict, ...]}.
    `price_lookup`: {ticker: yes_bid_dollars}.  Conservative: bid for YES
    positions (what we'd realize selling); bid is YES-equivalent so for
    NO positions use (1 - bid).

    Writes per-strategy result to ep:strategy_pnl_realtime as
    `{strategy}:unrealized_cents`. Returns the dict for callers that want it.
    """
    result: dict[str, int] = {}
    for strategy, positions in positions_by_strategy.items():
        unrealized = 0
        for pos in positions:
            ticker = pos.get("ticker") or ""
            contracts = int(pos.get("contracts") or 0)
            entry_cents = int(pos.get("entry_cents") or 0)
            side = (pos.get("side") or "").lower()
            yes_bid = price_lookup.get(ticker)
            if contracts <= 0 or yes_bid is None:
                continue
            if side == "yes":
                cur_cents = int(round(float(yes_bid) * 100))
            elif side == "no":
                cur_cents = int(round((1.0 - float(yes_bid)) * 100))
            else:
                continue
            unrealized += (cur_cents - entry_cents) * contracts
        result[strategy] = unrealized
        try:
            await bus_redis.hset(_REALTIME_HASH, f"{strategy}:unrealized_cents", str(unrealized))
        except Exception as exc:
            log.debug("write unrealized(%s) failed: %s", strategy, exc)
    return result


# ── Daily rollover ────────────────────────────────────────────────────────────

async def daily_rollover(bus_redis: Any, dsn: Optional[str] = None) -> dict[str, int]:
    """Snapshot today's realtime counters to strategy_pnl_daily; reset counters.

    Idempotent across calls within the same UTC day — uses ON CONFLICT to
    upsert (date, strategy) row. Returns {strategy: row_count} indicating
    which strategies were snapshotted.
    """
    actual_dsn = dsn or os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
    if not actual_dsn:
        log.warning("daily_rollover: DATABASE_URL not set; skipping snapshot")
        return {}

    try:
        all_fields = await bus_redis.hgetall(_REALTIME_HASH)
    except Exception as exc:
        log.warning("daily_rollover: redis hgetall failed: %s", exc)
        return {}

    # Decode (Redis may return bytes)
    by_strategy: dict[str, dict[str, int]] = {}
    for k, v in (all_fields or {}).items():
        if isinstance(k, bytes):
            k = k.decode()
        if isinstance(v, bytes):
            v = v.decode()
        if ":" not in k:
            continue
        strat, _, field = k.partition(":")
        try:
            value = int(float(v))
        except (TypeError, ValueError):
            continue
        by_strategy.setdefault(strat, {})[field] = value

    if not by_strategy:
        log.info("daily_rollover: no strategies to snapshot")
        return {}

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pool: Optional[asyncpg.Pool] = None
    try:
        pool = await asyncpg.create_pool(actual_dsn, min_size=1, max_size=2)
        async with pool.acquire() as conn:
            for strat, fields in by_strategy.items():
                await conn.execute(
                    f"""
                    INSERT INTO {_DAILY_TABLE}
                      (date, strategy, realized_pnl_cents, unrealized_eod_cents,
                       fees_cents, slippage_cents, signal_count, fill_count,
                       settlement_count)
                    VALUES ($1::date, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (date, strategy) DO UPDATE SET
                      realized_pnl_cents   = EXCLUDED.realized_pnl_cents,
                      unrealized_eod_cents = EXCLUDED.unrealized_eod_cents,
                      fees_cents           = EXCLUDED.fees_cents,
                      slippage_cents       = EXCLUDED.slippage_cents,
                      signal_count         = EXCLUDED.signal_count,
                      fill_count           = EXCLUDED.fill_count,
                      settlement_count     = EXCLUDED.settlement_count,
                      inserted_at          = NOW()
                    """,
                    date_str, strat,
                    int(fields.get("realized_pnl_cents", 0)),
                    int(fields.get("unrealized_cents", 0)),
                    int(fields.get("fees_cents", 0)),
                    int(fields.get("slippage_cents", 0)),
                    int(fields.get("signal_count", 0)),
                    int(fields.get("fill_count", 0)),
                    int(fields.get("settlement_count", 0)),
                )
    finally:
        if pool is not None:
            await pool.close()

    # Reset realtime counters (preserve unrealized — recomputed every 5 min)
    counter_fields = (
        "realized_pnl_cents", "fees_cents", "slippage_cents",
        "signal_count", "fill_count", "settlement_count",
    )
    try:
        for strat in by_strategy:
            for field in counter_fields:
                await bus_redis.hdel(_REALTIME_HASH, f"{strat}:{field}")
    except Exception as exc:
        log.warning("daily_rollover: counter reset failed: %s", exc)

    log.info("daily_rollover: snapshotted %d strategies for %s", len(by_strategy), date_str)
    return {strat: 1 for strat in by_strategy}


# ── Drift detection (z-score) ─────────────────────────────────────────────────

async def detect_drift(
    strategy: str,
    expected_daily_pnl_cents: float,
    expected_daily_std_cents: float,
    dsn: Optional[str] = None,
) -> dict[str, Any]:
    """Return drift report for `strategy` using 1w/4w/12w z-score thresholds.

    Returns dict with: {window_days: z_score} plus a `flagged` field set to
    the first window that breaches its threshold (3σ at 1w, 2σ at 4w/12w),
    or None. Empty dict if not enough history.

    Reads from strategy_pnl_daily. Used by B.4 tuning loop quarterly and
    by S.3 per-strategy halts mid-quarter for fast-moving regressions.
    """
    actual_dsn = dsn or os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
    if not actual_dsn:
        return {}
    if expected_daily_std_cents <= 0:
        return {}

    pool: Optional[asyncpg.Pool] = None
    try:
        pool = await asyncpg.create_pool(actual_dsn, min_size=1, max_size=2)
    except Exception:
        return {}

    report: dict[str, Any] = {"strategy": strategy, "flagged": None}
    try:
        async with pool.acquire() as conn:
            for window_days, sigma_threshold in ((7, 3.0), (28, 2.0), (84, 2.0)):
                row = await conn.fetchrow(
                    f"""
                    SELECT SUM(realized_pnl_cents) AS pnl, COUNT(*) AS days
                    FROM {_DAILY_TABLE}
                    WHERE strategy = $1
                      AND date >= CURRENT_DATE - make_interval(days => $2)
                    """,
                    strategy, window_days,
                )
                if row is None or row["days"] is None or row["days"] < window_days // 2:
                    continue
                pnl = float(row["pnl"] or 0)
                exp_total = expected_daily_pnl_cents * float(row["days"])
                exp_std = expected_daily_std_cents * (float(row["days"]) ** 0.5)
                z = (pnl - exp_total) / exp_std if exp_std > 0 else 0.0
                report[f"{window_days}d_z"] = round(z, 3)
                if abs(z) >= sigma_threshold and report["flagged"] is None:
                    report["flagged"] = f"{window_days}d@{sigma_threshold}σ"
    finally:
        await pool.close()
    return report


# ── Background loop wiring helper ─────────────────────────────────────────────

async def rollover_loop(bus_redis: Any) -> None:
    """Once-per-minute check for UTC-midnight rollover. Idempotent within the day.

    Wire from ep_exec.py exec_main alongside the other periodic loops:
        asyncio.create_task(rollover_loop(bus._r))
    """
    log.info("PNL attribution rollover loop started")
    _last_rollover_date: Optional[str] = None
    while True:
        try:
            await asyncio.sleep(_ROLLOVER_CHECK_INTERVAL_S)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Trigger at any time after the date changes from the last rollover.
            # Effectively fires on the first cycle after 00:00 UTC.
            if _last_rollover_date is not None and today != _last_rollover_date:
                log.info("PNL rollover trigger: %s → %s", _last_rollover_date, today)
                await daily_rollover(bus_redis)
            _last_rollover_date = today
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("rollover_loop: %s", exc)
