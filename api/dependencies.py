"""
EdgePulse API FastAPI dependency providers.

Exports:
  get_db                — yields an async SQLAlchemy session
  get_current_user      — decodes Bearer JWT and loads the User row
  get_current_active_user — asserts user.is_active
  require_admin         — asserts user.is_admin
  oauth2_scheme         — OAuth2PasswordBearer instance (tokenUrl="/auth/login")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.security import decode_access_token

if TYPE_CHECKING:
    # Import only for type checking to avoid circular imports at runtime.
    from api.models import User

# Re-export get_db so callers can import from a single location.
__all__ = [
    "get_db",
    "get_current_user",
    "get_current_active_user",
    "require_admin",
    "oauth2_scheme",
]

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> "User":
    """
    Resolve the Bearer JWT to a User ORM instance.

    Steps:
      1. Decode and validate the access token (raises 401 on failure).
      2. Extract the ``sub`` claim as the user id.
      3. Fetch the User row from the database.
      4. Raise 401 if the user is not found or not active.

    Returns:
        The authenticated User ORM object.

    Raises:
        HTTPException(401): token invalid, user not found, or user inactive.
    """
    # Lazy import avoids circular dependency: models imports Base from database,
    # and database does not import dependencies.
    from api.models import User  # noqa: PLC0415

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload: dict = decode_access_token(token)  # raises 401 on bad token

    user_id: str | None = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise credentials_exception

    return user


async def get_current_active_user(
    current_user: "User" = Depends(get_current_user),
) -> "User":
    """
    Assert that the resolved user has an active account.

    This is a thin guard layered on top of ``get_current_user``.
    ``get_current_user`` already rejects inactive users with a 401; this
    dependency provides an explicit 403 path for routes that need to
    distinguish "not authenticated" from "authenticated but deactivated".

    Raises:
        HTTPException(403): if ``current_user.is_active`` is False.

    Returns:
        The active User ORM object.
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive",
        )
    return current_user


async def require_admin(
    current_user: "User" = Depends(get_current_user),
) -> "User":
    """
    Assert that the resolved user holds admin privileges.

    Raises:
        HTTPException(403): if ``current_user.is_admin`` is False.

    Returns:
        The admin User ORM object.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )
    return current_user
