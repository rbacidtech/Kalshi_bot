"""
api/routers/notifications.py — Dashboard notification feed.

Endpoints:
  GET /notifications?limit=50&type=&severity=   Recent notifications from ep:notifications stream
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from api.dependencies import require_admin
from api.models import User
from api.redis_client import get_redis
from api.routers.auth import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_STREAM_KEY = "ep:notifications"


@router.get("", summary="Recent notifications from ep:notifications stream")
@limiter.limit("120/minute")
async def get_notifications(
    request:  Request,
    limit:    int            = Query(50, ge=1, le=200),
    type:     Optional[str] = Query(None, description="Filter: fill|exit|alert|trade_alert|circuit_breaker|daily_summary"),
    severity: Optional[str] = Query(None, description="Filter: info|warning|critical"),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    Returns the last `limit` notifications from the ep:notifications Redis stream,
    newest first.  Each entry's flat fields are returned as-is.
    """
    r = get_redis()
    fetch = limit * 4 if (type or severity) else limit
    try:
        entries = await r.xrevrange(_STREAM_KEY, count=fetch)
    except Exception:
        entries = []

    notifications = []
    for entry_id, fields in entries:
        n: dict[str, Any] = {"id": str(entry_id)}
        for k, v in fields.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            # Coerce numeric strings back to numbers for known fields
            if key in ("contracts", "price_cents", "entry_cents", "current_cents",
                       "pnl_cents", "failure_count", "open_positions", "trades"):
                try:
                    val = int(float(val))
                except (ValueError, TypeError):
                    pass
            elif key in ("edge", "win_rate"):
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    pass
            n[key] = val

        if type and n.get("type") != type:
            continue
        if severity and n.get("severity") != severity:
            continue
        notifications.append(n)
        if len(notifications) >= limit:
            break

    return {"notifications": notifications, "count": len(notifications)}
