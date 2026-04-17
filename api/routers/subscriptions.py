from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record as audit_record
from api.config import get_settings
from api.dependencies import get_current_user, get_db, require_admin
from api.routers.auth import limiter
from api.models import Subscription, User
from api.schemas import SubscriptionResponse, UserTier

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _volume_limit_for_tier(tier: str) -> int:
    cfg = get_settings()
    return {
        UserTier.free:          cfg.tier_volume_free,
        UserTier.starter:       cfg.tier_volume_starter,
        UserTier.pro:           cfg.tier_volume_pro,
        UserTier.institutional: cfg.tier_volume_institutional,
    }.get(UserTier(tier), cfg.tier_volume_free)


async def _get_subscription(db: AsyncSession, user_id: uuid.UUID) -> Subscription:
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription record not found for this user.",
        )
    return sub


# ---------------------------------------------------------------------------
# GET /subscriptions/me
# ---------------------------------------------------------------------------

@router.get(
    "/me",
    response_model=SubscriptionResponse,
    summary="Get current user's subscription and volume status",
)
@limiter.limit("60/minute")
async def get_my_subscription(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionResponse:
    sub = await _get_subscription(db, current_user.id)
    return SubscriptionResponse.model_validate(sub)


# ---------------------------------------------------------------------------
# POST /subscriptions/me/reset-volume  (admin only)
# ---------------------------------------------------------------------------

@router.post(
    "/me/reset-volume",
    summary="Reset a user's monthly volume counter (admin only)",
)
@limiter.limit("60/minute")
async def reset_volume(
    request: Request,
    user_id: uuid.UUID = Query(..., description="UUID of the user whose volume counter to reset"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _get_subscription(db, user_id)

    sub.current_month_volume_cents = 0
    sub.billing_cycle_start = datetime.now(tz=timezone.utc)
    db.add(sub)
    await db.flush()

    await audit_record(
        db,
        action="subscription_volume_reset",
        user_id=admin.id,
        resource=f"subscription:user:{user_id}",
        detail={"target_user_id": str(user_id)},
    )

    return {"ok": True, "user_id": str(user_id), "volume_used_cents": 0}


# ---------------------------------------------------------------------------
# GET /subscriptions/tiers  (public, no auth)
# ---------------------------------------------------------------------------

@router.get(
    "/tiers",
    summary="List all subscription tiers and their volume limits",
)
@limiter.limit("60/minute")
async def list_tiers(request: Request) -> dict[str, Any]:
    cfg = get_settings()
    return {
        "free": {
            "volume_limit_cents": cfg.tier_volume_free,
            "label": "$500/month",
        },
        "starter": {
            "volume_limit_cents": cfg.tier_volume_starter,
            "label": "$5,000/month",
        },
        "pro": {
            "volume_limit_cents": cfg.tier_volume_pro,
            "label": "$50,000/month",
        },
        "institutional": {
            "volume_limit_cents": cfg.tier_volume_institutional,
            "label": "Unlimited",
        },
    }


# ---------------------------------------------------------------------------
# POST /subscriptions/me/track  (admin only — called by exec node after fills)
# ---------------------------------------------------------------------------

class TrackVolumeRequest(BaseModel):
    ticker: str
    volume_cents: int


@router.post(
    "/me/track",
    summary="Increment a user's monthly volume counter after a fill (exec node, admin only)",
)
@limiter.limit("60/minute")
async def track_volume(
    request: Request,
    body: TrackVolumeRequest,
    user_id: uuid.UUID = Query(..., description="UUID of the user for whom to record volume"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _get_subscription(db, user_id)

    limit_cents = _volume_limit_for_tier(sub.tier)
    new_total = sub.current_month_volume_cents + body.volume_cents
    blocked = (limit_cents > 0) and (new_total > limit_cents)

    if not blocked:
        sub.current_month_volume_cents = new_total
        db.add(sub)
        await db.flush()

    await audit_record(
        db,
        action="subscription_volume_track",
        user_id=admin.id,
        resource=f"subscription:user:{user_id}",
        detail={
            "target_user_id": str(user_id),
            "ticker": body.ticker,
            "volume_cents": body.volume_cents,
            "new_total": sub.current_month_volume_cents,
            "limit_cents": limit_cents,
            "blocked": blocked,
        },
    )

    return {
        "ok": True,
        "volume_used_cents": sub.current_month_volume_cents,
        "limit_cents": limit_cents,
        "blocked": blocked,
    }
