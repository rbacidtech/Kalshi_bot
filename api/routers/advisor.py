"""
api/routers/advisor.py — Advisor alert feed and status endpoints.

Endpoints (all admin-only):
  GET /advisor/alerts?limit=20&severity=   Recent alerts from ep:alerts stream
  GET /advisor/status                      Last advisor run summary + strategy health
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from api.dependencies import require_admin
from api.models import User
from api.redis_client import get_redis
from api.routers.auth import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/advisor", tags=["advisor"])

_ALERTS_KEY  = "ep:alerts"
_STATUS_KEY  = "ep:advisor:status"


def _ts_us_to_iso(ts_us: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc).isoformat()
    except Exception:
        return None


@router.get("/alerts", summary="Recent advisor alerts from ep:alerts stream")
@limiter.limit("60/minute")
async def get_alerts(
    request: Request,
    limit:    int            = Query(20, ge=1, le=100),
    severity: Optional[str] = Query(None, description="Filter: info|warning|critical"),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    Returns the last `limit` alerts from the ep:alerts Redis stream, newest first.
    Optionally filtered by severity.
    """
    r = get_redis()
    try:
        entries = await r.xrevrange(_ALERTS_KEY, count=limit * 3 if severity else limit)
    except Exception:
        entries = []

    alerts = []
    for entry_id, fields in entries:
        raw = fields.get("payload") or fields.get(b"payload")
        if not raw:
            continue
        try:
            a = json.loads(raw if isinstance(raw, str) else raw.decode())
        except Exception:
            continue
        if severity and a.get("severity") != severity.lower():
            continue
        a["id"] = str(entry_id)
        alerts.append(a)
        if len(alerts) >= limit:
            break

    return {"alerts": alerts, "count": len(alerts)}


@router.get("/status", summary="Last advisor run summary and strategy health")
@limiter.limit("60/minute")
async def get_status(
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    Returns the status snapshot written by ep_advisor.py after each run.
    Includes strategy health grid, concentration metrics, and auto-apply history.
    """
    r = get_redis()
    try:
        raw = await r.get(_STATUS_KEY)
    except Exception:
        raw = None

    if not raw:
        return {
            "available":     False,
            "last_run_ts":   None,
            "message":       "No advisor run recorded yet. Start ep_advisor.py on exec node.",
        }

    try:
        status = json.loads(raw if isinstance(raw, str) else raw.decode())
        status["available"] = True
        return status
    except Exception as exc:
        logger.warning("advisor status parse error: %s", exc)
        return {"available": False, "error": "Status data corrupt"}
