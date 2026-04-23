from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies import get_current_active_user as require_auth
from api.redis_client import get_redis

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("", summary="Performance summary from exec node")
async def get_performance(
    days: int = Query(30, ge=1, le=365),
    _user=Depends(require_auth),
):
    """
    Returns the performance summary written by the exec node's
    _performance_publisher_loop to Redis ep:performance.

    Falls back to zeros if the key is absent or Redis is unreachable.
    """
    r = get_redis()
    raw: Optional[str] = None
    if r:
        try:
            # Prefer period-specific key; fall back to the 30d default
            if days in (7, 30, 90):
                raw = await r.get(f"ep:performance:{days}")
            if not raw:
                raw = await r.get("ep:performance")
        except Exception:
            pass

    if raw:
        try:
            data = json.loads(raw)
            return data
        except Exception:
            pass

    # Empty fallback
    return {
        "period_days": days,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl_cents": 0,
        "avg_pnl_per_trade": 0.0,
        "by_strategy": {},
        "best_trade": None,
        "worst_trade": None,
        "avg_hold_time_hours": 0.0,
        "sharpe_daily": None,
        "streak_current": 0,
        "streak_best": 0,
        "avg_win_cents": 0.0,
        "avg_loss_cents": 0.0,
        "expectancy_cents": 0.0,
        "pnl_distribution": [],
    }


@router.get("/history", summary="P&L snapshot history for charting")
async def get_pnl_history(
    hours: int = Query(24, ge=1, le=720),
    _user=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns bucketed P&L snapshots (one per hour) over the requested window.
    Used by the dashboard sparkline chart.
    """
    from api.models import PnlSnapshot
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        result = await db.execute(
            select(PnlSnapshot)
            .where(PnlSnapshot.ts >= since)
            .order_by(PnlSnapshot.ts.asc())
        )
        rows = result.scalars().all()
        # Bucket by hour — keep the last snapshot per hour
        buckets: dict[str, dict] = {}
        for row in rows:
            ts = row.ts if isinstance(row.ts, datetime) else datetime.fromisoformat(str(row.ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bucket = ts.strftime("%Y-%m-%dT%H:00:00Z")
            buckets[bucket] = {
                "ts": ts.isoformat(),
                "balance_cents": row.balance_cents,
                "deployed_cents": row.deployed_cents,
                "unrealized_pnl_cents": row.unrealized_pnl_cents,
                "realized_pnl_cents": row.realized_pnl_cents,
                "position_count": row.position_count,
            }
        return list(buckets.values())
    except Exception:
        return []


@router.get("/equity-curve", summary="Daily cumulative realized P&L for equity curve chart")
async def get_equity_curve(
    days: int = Query(90, ge=7, le=365),
    _user=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns daily cumulative realized P&L over the requested window.
    For each UTC calendar day, takes the last snapshot's realized_pnl_cents value
    (which is already cumulative from the source).
    Used by the PerformancePage equity curve chart.
    """
    from api.models import PnlSnapshot
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        result = await db.execute(
            select(PnlSnapshot)
            .where(PnlSnapshot.ts >= since)
            .order_by(PnlSnapshot.ts.asc())
        )
        rows = result.scalars().all()
        # Bucket by UTC calendar day — keep the last snapshot per day
        buckets: dict[str, int] = {}
        for row in rows:
            ts = row.ts if isinstance(row.ts, datetime) else datetime.fromisoformat(str(row.ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            day = ts.strftime("%Y-%m-%d")
            buckets[day] = row.realized_pnl_cents
        return [
            {"date": day, "cumulative_pnl_cents": pnl}
            for day, pnl in buckets.items()
        ]
    except Exception:
        return []
