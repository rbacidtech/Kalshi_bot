from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies import get_current_active_user as require_auth

router = APIRouter(prefix="/performance", tags=["performance"])

REDIS_URL = os.getenv("REDIS_URL", "")


async def _get_redis():
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        return r
    except Exception:
        return None


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
    r = await _get_redis()
    raw: Optional[str] = None
    if r:
        try:
            raw = await r.get("ep:performance")
        except Exception:
            pass
        finally:
            try:
                await r.aclose()
            except Exception:
                pass

    if raw:
        try:
            data = json.loads(raw)
            # Override period_days with requested value for display purposes
            data["period_days"] = days
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
