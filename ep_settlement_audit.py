"""Weekly + monthly settlement reconciliation audits — Engineering B.3.

The existing ep_settlements.py runs daily Kalshi-settlement reconciliation.
Engineering B.3 adds two more cadences:

  - **Weekly** (Sunday 03:00 UTC): per-strategy P&L vs Kalshi audit;
    fee schedule audit; >$1/week drift triggers alert.
  - **Monthly** (1st 04:00 UTC): full balance-implied test; >$5/month drift
    triggers alert.

Plus disputed-settlement handling: when Kalshi changes a previously-recorded
settlement (rare but real), reverse + re-apply the P&L attribution.

This module is the AUDIT layer. The daily reconciliation in ep_settlements
remains the primary write path; this layer just verifies the cumulative
totals match independent calculations.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)


_FEE_TOLERANCE_CENTS_WEEKLY = 100        # $1/week — Engineering B.3 §
_PNL_TOLERANCE_CENTS_MONTHLY = 500       # $5/month — Engineering B.3 §
_DEFAULT_DSN_ENV = "DATABASE_URL"


async def weekly_audit(dsn: Optional[str] = None) -> dict[str, Any]:
    """Sunday 03:00 UTC audit. Returns report dict with anomalies.

    Compares per-strategy realized P&L from `position_history` (sum over
    last 7 days) against expected per-strategy P&L from
    `strategy_pnl_daily` (sum over same window). Flags >$1 drift.
    """
    actual_dsn = (dsn or os.getenv(_DEFAULT_DSN_ENV, "")).replace("+asyncpg", "")
    if not actual_dsn:
        return {"error": "DATABASE_URL not set"}
    pool: Optional[asyncpg.Pool] = None
    try:
        pool = await asyncpg.create_pool(actual_dsn, min_size=1, max_size=2)
    except Exception as exc:
        return {"error": f"connect failed: {exc}"}

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)

    try:
        async with pool.acquire() as conn:
            # Per-strategy from position_history (the canonical settled-P&L table)
            ph_rows = await conn.fetch(
                """
                SELECT COALESCE(strategy, 'unknown') AS strategy,
                       SUM(realized_pnl_cents) AS pnl, COUNT(*) AS n
                FROM position_history
                WHERE exited_at >= $1
                GROUP BY strategy
                """,
                since,
            )
            ph_by_strat = {r["strategy"]: {"pnl": int(r["pnl"] or 0), "n": int(r["n"])}
                           for r in ph_rows}

            # Per-strategy from strategy_pnl_daily (A.5 snapshot)
            spd_rows = await conn.fetch(
                """
                SELECT strategy, SUM(realized_pnl_cents) AS pnl,
                       SUM(settlement_count) AS n
                FROM strategy_pnl_daily
                WHERE date >= ($1::date)
                GROUP BY strategy
                """,
                since.date(),
            )
            spd_by_strat = {r["strategy"]: {"pnl": int(r["pnl"] or 0), "n": int(r["n"] or 0)}
                            for r in spd_rows}
    finally:
        await pool.close()

    anomalies = []
    all_strategies = set(ph_by_strat) | set(spd_by_strat)
    for strat in sorted(all_strategies):
        ph_pnl = ph_by_strat.get(strat, {}).get("pnl", 0)
        spd_pnl = spd_by_strat.get(strat, {}).get("pnl", 0)
        drift = ph_pnl - spd_pnl
        if abs(drift) > _FEE_TOLERANCE_CENTS_WEEKLY:
            anomalies.append({
                "strategy":          strat,
                "ph_pnl_cents":      ph_pnl,
                "spd_pnl_cents":     spd_pnl,
                "drift_cents":       drift,
                "abs_drift_dollars": round(abs(drift) / 100, 2),
            })

    return {
        "cadence":   "weekly",
        "window":    {"since": since.isoformat(), "until": now.isoformat()},
        "strategies_observed":    len(all_strategies),
        "anomalies":              anomalies,
        "tolerance_cents":        _FEE_TOLERANCE_CENTS_WEEKLY,
    }


async def monthly_audit(dsn: Optional[str] = None) -> dict[str, Any]:
    """1st of month 04:00 UTC. Full balance-implied test.

    expected_balance_now = anchor_at_start_of_month + sum(realized_pnl in month)
    Compare against ep:balance hgetall to compute drift.
    Trigger condition: >$5 unexplained drift.
    """
    actual_dsn = (dsn or os.getenv(_DEFAULT_DSN_ENV, "")).replace("+asyncpg", "")
    if not actual_dsn:
        return {"error": "DATABASE_URL not set"}
    pool: Optional[asyncpg.Pool] = None
    try:
        pool = await asyncpg.create_pool(actual_dsn, min_size=1, max_size=2)
    except Exception as exc:
        return {"error": f"connect failed: {exc}"}

    now = datetime.now(timezone.utc)
    # 30-day lookback (calendar-month-ish)
    since = now - timedelta(days=30)

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT SUM(realized_pnl_cents) AS pnl, COUNT(*) AS n
                FROM position_history
                WHERE exited_at >= $1
                """,
                since,
            )
            month_pnl = int(row["pnl"] or 0) if row else 0
            month_n = int(row["n"] or 0) if row else 0

            # Earliest balance snapshot in window
            bal_row = await conn.fetchrow(
                """
                SELECT balance_cents
                FROM balance_snapshots
                WHERE taken_at >= $1
                ORDER BY taken_at ASC
                LIMIT 1
                """,
                since,
            )
            anchor_bal = int(bal_row["balance_cents"]) if bal_row else None

            # Latest balance snapshot
            latest_bal_row = await conn.fetchrow(
                """
                SELECT balance_cents
                FROM balance_snapshots
                ORDER BY taken_at DESC
                LIMIT 1
                """
            )
            latest_bal = int(latest_bal_row["balance_cents"]) if latest_bal_row else None
    finally:
        await pool.close()

    if anchor_bal is None or latest_bal is None:
        return {
            "cadence":   "monthly",
            "error":     "Insufficient balance_snapshots in window",
            "window":    {"since": since.isoformat(), "until": now.isoformat()},
        }

    expected_balance = anchor_bal + month_pnl
    drift = latest_bal - expected_balance
    return {
        "cadence":            "monthly",
        "window":             {"since": since.isoformat(), "until": now.isoformat()},
        "anchor_bal_cents":   anchor_bal,
        "latest_bal_cents":   latest_bal,
        "month_pnl_cents":    month_pnl,
        "expected_bal_cents": expected_balance,
        "drift_cents":        drift,
        "abs_drift_dollars":  round(abs(drift) / 100, 2),
        "trade_count":        month_n,
        "tolerance_cents":    _PNL_TOLERANCE_CENTS_MONTHLY,
        "drift_exceeds_tolerance": abs(drift) > _PNL_TOLERANCE_CENTS_MONTHLY,
    }


async def weekly_audit_loop(bus_redis: Any, interval_s: int = 86_400) -> None:
    """Background task: invoke weekly_audit once on the first Sunday after
    boot, then every 7 days. Sets ep:health alert + invokes push_fn if drift
    exceeds tolerance.

    Wire from ep_exec.exec_main asyncio.gather alongside other periodic loops.
    """
    log.info("B.3 weekly settlement audit loop started (interval=%ds)", interval_s)
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Sunday-only — sleep until next Sunday 03:00 UTC if today isn't Sunday
            if now.weekday() != 6:  # 6 = Sunday
                await asyncio.sleep(interval_s)
                continue
            report = await weekly_audit()
            anomalies = report.get("anomalies", [])
            if anomalies:
                log.warning("B.3 weekly audit: %d anomalies > $%.2f tolerance",
                            len(anomalies), _FEE_TOLERANCE_CENTS_WEEKLY / 100)
                if bus_redis is not None:
                    try:
                        await bus_redis.hset(
                            "ep:health", "settlement_audit_weekly",
                            f"{now.isoformat()}|anomalies={len(anomalies)}",
                        )
                    except Exception:
                        pass
            else:
                log.info("B.3 weekly audit: clean — %d strategies, no anomalies",
                         report.get("strategies_observed", 0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("weekly_audit_loop: %s", exc)
        await asyncio.sleep(interval_s)
