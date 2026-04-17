from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from api.models import AuditLog


async def record(
    db: AsyncSession,
    action: str,
    *,
    user_id: Optional[UUID] = None,
    resource: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    success: bool = True,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Write one immutable audit entry. Never raises — audit failure must not block the request."""
    try:
        entry = AuditLog(
            user_id=user_id,
            action=action,
            resource=resource,
            ip_address=ip_address,
            user_agent=user_agent,
            success=success,
            detail=detail,
        )
        db.add(entry)
        await db.flush()
    except Exception:
        pass  # audit failure is non-fatal; logged by SQLAlchemy's own error handling
