from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api import audit
from api.config import get_settings
from api.dependencies import get_db, require_admin
from api.routers.auth import limiter
from api.models import AuditLog, Subscription, User
from api.schemas import UserResponse, UserTier, UserUpdate

router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TIER_VOLUME_MAP: dict[str, str] = {
    UserTier.free:          "tier_volume_free",
    UserTier.starter:       "tier_volume_starter",
    UserTier.pro:           "tier_volume_pro",
    UserTier.institutional: "tier_volume_institutional",
}


async def _get_user_or_404(user_id: uuid.UUID, db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=dict)
@limiter.limit("30/minute")
async def list_users(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    offset = (page - 1) * per_page

    total_result = await db.execute(select(func.count()).select_from(User))
    total: int = total_result.scalar_one()

    users_result = await db.execute(
        select(User).order_by(User.created_at.desc()).offset(offset).limit(per_page)
    )
    users = users_result.scalars().all()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "users": [UserResponse.model_validate(u) for u in users],
    }


# ---------------------------------------------------------------------------
# GET /admin/users/{user_id}
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}", response_model=UserResponse)
@limiter.limit("30/minute")
async def get_user(
    request: Request,
    user_id: uuid.UUID,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await _get_user_or_404(user_id, db)
    return UserResponse.model_validate(user)


# ---------------------------------------------------------------------------
# PATCH /admin/users/{user_id}
# ---------------------------------------------------------------------------


@router.patch("/users/{user_id}", response_model=UserResponse)
@limiter.limit("30/minute")
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await _get_user_or_404(user_id, db)

    changes: dict[str, Any] = {}

    if body.is_active is not None and body.is_active != user.is_active:
        changes["is_active"] = {"before": user.is_active, "after": body.is_active}
        user.is_active = body.is_active

    if body.is_admin is not None and body.is_admin != user.is_admin:
        changes["is_admin"] = {"before": user.is_admin, "after": body.is_admin}
        user.is_admin = body.is_admin

    if body.tier is not None and body.tier.value != user.tier:
        changes["tier"] = {"before": user.tier, "after": body.tier.value}
        user.tier = body.tier.value

        # Keep the Subscription row in sync.
        sub_result = await db.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub: Optional[Subscription] = sub_result.scalar_one_or_none()
        if sub is not None:
            sub.tier = body.tier.value
            settings = get_settings()
            volume_attr = _TIER_VOLUME_MAP[body.tier]
            new_limit: int = getattr(settings, volume_attr)
            # Only update volume_limit_cents when the new tier imposes a finite cap.
            # institutional tier uses 0 to signal "no cap"; preserve that semantic.
            sub.volume_limit_cents = new_limit

    db.add(user)
    await db.flush()

    await audit.record(
        db,
        "admin.user.update",
        user_id=admin.id,
        resource=f"user:{user_id}",
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        success=True,
        detail=changes if changes else None,
    )

    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


# ---------------------------------------------------------------------------
# DELETE /admin/users/{user_id}  — soft delete only
# ---------------------------------------------------------------------------


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_user(
    user_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    user = await _get_user_or_404(user_id, db)

    # Prevent admins from deactivating themselves — that would be a self-lockout.
    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account",
        )

    user.is_active = False
    db.add(user)
    await db.flush()

    await audit.record(
        db,
        "admin.user.deactivate",
        user_id=admin.id,
        resource=f"user:{user_id}",
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        success=True,
        detail={"target_user_id": str(user_id)},
    )

    await db.commit()


# ---------------------------------------------------------------------------
# GET /admin/users/{user_id}/audit
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/audit")
@limiter.limit("30/minute")
async def get_user_audit(
    request: Request,
    user_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    # Verify the target user exists before querying audit logs.
    await _get_user_or_404(user_id, db)

    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.user_id == user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    entries = result.scalars().all()

    return [
        {
            "id": str(e.id),
            "action": e.action,
            "resource": e.resource,
            "ip_address": e.ip_address,
            "user_agent": e.user_agent,
            "success": e.success,
            "detail": e.detail,
            "created_at": e.created_at,
        }
        for e in entries
    ]


# ---------------------------------------------------------------------------
# GET /admin/stats
# ---------------------------------------------------------------------------


@router.get("/stats")
@limiter.limit("30/minute")
async def get_stats(
    request: Request,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    total_result = await db.execute(select(func.count()).select_from(User))
    total_users: int = total_result.scalar_one()

    active_result = await db.execute(
        select(func.count()).select_from(User).where(User.is_active.is_(True))
    )
    active_users: int = active_result.scalar_one()

    # One query: count of users grouped by tier value.
    tier_rows = await db.execute(
        select(User.tier, func.count().label("cnt"))
        .group_by(User.tier)
    )
    users_per_tier: dict[str, int] = {row.tier: row.cnt for row in tier_rows}

    # Ensure all known tiers are represented in the response even if count is 0.
    for tier in UserTier:
        users_per_tier.setdefault(tier.value, 0)

    total_positions_cents = await _sum_positions_from_redis()

    return {
        "total_users": total_users,
        "active_users": active_users,
        "users_per_tier": users_per_tier,
        "total_positions_deployed_cents": total_positions_cents,
    }


async def _sum_positions_from_redis() -> int:
    """
    Read the global ep:positions hash and sum the deployed cost across all
    open positions.  Returns 0 if Redis is unreachable or the key is absent.

    Deployed cost per position:
      - yes side: entry_cents * contracts
      - no  side: (100 - entry_cents) * contracts
    """
    settings = get_settings()
    r: aioredis.Redis = aioredis.from_url(settings.redis_url, decode_responses=False)
    try:
        raw: dict = await r.hgetall("ep:positions")
        total = 0
        for raw_val in raw.values():
            try:
                p: dict[str, Any] = json.loads(raw_val)
            except Exception:
                continue
            contracts = int(p.get("contracts", 0))
            if contracts == 0:
                continue
            entry = int(p.get("entry_cents", 0))
            side = p.get("side", "yes")
            cost = (100 - entry) * contracts if side == "no" else entry * contracts
            total += cost
        return total
    except Exception:
        return 0
    finally:
        await r.aclose()
