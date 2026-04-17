"""
EdgePulse auth router — /auth

Endpoints:
  POST /auth/register    Create a new free-tier account
  POST /auth/login       Authenticate and issue tokens (JSON or OAuth2 form)
  POST /auth/refresh     Rotate refresh token and issue new access token
  POST /auth/logout      Revoke the active refresh token
  GET  /auth/me          Return the authenticated user's profile
"""

from __future__ import annotations

import hashlib
import logging
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Annotated, Optional

import redis.asyncio as aioredis
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import OAuth2PasswordBearer as _OAuth2PasswordBearer
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api import audit
from api.config import get_settings
from api.database import get_db
from api.models import RefreshToken, Subscription, User
from api.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from api.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)


def _send_security_email(subject: str, body: str) -> None:
    import os, json, urllib.request
    api_key = os.getenv("RESEND_API_KEY", "")
    to_addr = os.getenv("ALERT_TO_EMAIL", "")
    if not (api_key and to_addr):
        return
    def _send():
        try:
            payload = json.dumps({
                "from":    "EdgePulse <onboarding@resend.dev>",
                "to":      [to_addr],
                "subject": f"[EdgePulse] {subject}",
                "text":    body,
            }).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as _e:
            logger.warning("Failed to send security email: %s", _e)
    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
# Rate limiter — shared application-level instance expected to be attached
# to app.state.limiter in the FastAPI application factory.  The router uses
# the same Limiter singleton so that the middleware can track counts.
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

_ACCOUNT_DISABLED = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Account disabled",
)


def _hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest of *raw*."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _set_refresh_cookie(response: Response, raw_token: str, settings) -> None:
    """Write the refresh token as an httponly, secure, SameSite=strict cookie."""
    max_age = REFRESH_TOKEN_EXPIRE_DAYS * 86_400
    response.set_cookie(
        key="refresh_token",
        value=raw_token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=max_age,
        path="/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key="refresh_token",
        path="/auth",
        httponly=True,
        secure=True,
        samesite="strict",
    )


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def _get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


def _build_token_response(access_token: str) -> TokenResponse:
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ---------------------------------------------------------------------------
# Dependency — resolve current user from Bearer token
# Defined locally so this router has no hard dependency on dependencies.py
# (which does not exist yet).  When dependencies.py is created with
# get_current_user it can be swapped in transparently.
# ---------------------------------------------------------------------------

_oauth2_scheme = _OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=True)


async def _require_current_user(
    token: Annotated[str, Depends(_oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Decode the Bearer JWT and return the matching active User."""
    payload = decode_access_token(token)  # raises 401 on any failure
    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise _INVALID_CREDENTIALS

    result = await db.execute(select(User).where(User.id == user_id))
    user: Optional[User] = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise _INVALID_CREDENTIALS
    return user


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserResponse,
    summary="Register a new user account",
)
@limiter.limit("5/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserResponse:
    settings = get_settings()
    ip = get_remote_address(request)
    user_agent: str = request.headers.get("user-agent", "")

    # --- duplicate check -------------------------------------------------- #
    existing = await _get_user_by_email(db, body.email)
    if existing is not None:
        # Audit the attempt before raising so the event is persisted.
        await audit.record(
            db,
            "register",
            user_id=None,
            resource="users",
            ip_address=ip,
            user_agent=user_agent,
            success=False,
            detail={"reason": "email_taken"},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # --- create user ------------------------------------------------------ #
    try:
        user = User(
            email=body.email,
            hashed_password=hash_password(body.password),
            tier="free",
            is_active=True,
        )
        db.add(user)
        await db.flush()  # populate user.id before FK insertion

        subscription = Subscription(
            user_id=user.id,
            tier="free",
            volume_limit_cents=settings.tier_volume_free,
        )
        db.add(subscription)

        await audit.record(
            db,
            "register",
            user_id=user.id,
            resource="users",
            ip_address=ip,
            user_agent=user_agent,
            success=True,
        )

        await db.commit()
        await db.refresh(user)
    except Exception:
        await db.rollback()
        logger.exception("register: DB error for email=%s", body.email)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration failed. Please try again.",
        )

    return UserResponse.model_validate(user)


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate and obtain access + refresh tokens",
)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """
    Accepts either:
      - application/json  with a LoginRequest body, OR
      - application/x-www-form-urlencoded  (OAuth2 password grant, username/password fields)

    This manual dispatch is intentional: FastAPI cannot declare both a JSON
    body and an OAuth2PasswordRequestForm on the same endpoint without one
    silently eating the other.
    """
    settings = get_settings()
    ip = get_remote_address(request)
    user_agent: str = request.headers.get("user-agent", "")

    content_type: str = request.headers.get("content-type", "")

    email: str
    plain_password: str

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        # OAuth2 password grant — username field carries the email address.
        form = await request.form()
        raw_username = form.get("username", "")
        raw_password = form.get("password", "")
        if not raw_username or not raw_password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="username and password fields are required for form login.",
            )
        email = str(raw_username)
        plain_password = str(raw_password)
    else:
        # JSON body — parse into LoginRequest for validation.
        try:
            raw_body = await request.body()
            body = LoginRequest.model_validate_json(raw_body)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid request body. Expected JSON with 'email' and 'password'.",
            )
        email = body.email
        plain_password = body.password

    # --- account lockout check -------------------------------------------- #
    _lockout_key = f"ep:lockout:{email}"
    redis = await _get_redis()
    try:
        lockout_val = await redis.get(_lockout_key)
        if lockout_val is not None and int(lockout_val) >= 5:
            _send_security_email(
                "Account Lockout",
                f"Login locked for {email} after 5 failed attempts.",
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Account temporarily locked. Try again in 15 minutes.",
            )
    finally:
        await redis.aclose()

    # --- lookup ----------------------------------------------------------- #
    user = await _get_user_by_email(db, email)

    async def _audit_failure(reason: str) -> None:
        await audit.record(
            db,
            "login_failed",
            user_id=user.id if user else None,
            resource="users",
            ip_address=ip,
            user_agent=user_agent,
            success=False,
            detail={"reason": reason},
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()

    async def _record_lockout_failure() -> None:
        r = await _get_redis()
        try:
            count = await r.incr(_lockout_key)
            if count >= 5:
                await r.expire(_lockout_key, 900)
            else:
                # Keep a rolling window; reset expiry on each failed attempt
                # so the window is relative to the last failure.
                await r.expire(_lockout_key, 900)
        finally:
            await r.aclose()

    if user is None:
        # Run a dummy verify to prevent timing attacks that could reveal
        # whether an email exists.
        verify_password(plain_password, hash_password("dummy-timing-guard"))
        await _audit_failure("user_not_found")
        raise _INVALID_CREDENTIALS

    if not verify_password(plain_password, user.hashed_password):
        await _record_lockout_failure()
        await _audit_failure("bad_password")
        raise _INVALID_CREDENTIALS

    if not user.is_active:
        await _audit_failure("account_disabled")
        raise _ACCOUNT_DISABLED

    # --- clear lockout on successful authentication ----------------------- #
    r_clear = await _get_redis()
    try:
        await r_clear.delete(_lockout_key)
    finally:
        await r_clear.aclose()

    # --- issue tokens ----------------------------------------------------- #
    try:
        user.last_login_at = datetime.now(tz=timezone.utc)
        db.add(user)

        access_token = create_access_token(subject=str(user.id))
        raw_refresh, token_hash = create_refresh_token()

        refresh_record = RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(tz=timezone.utc)
            + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
            ip_address=ip,
        )
        db.add(refresh_record)

        await audit.record(
            db,
            "login",
            user_id=user.id,
            resource="users",
            ip_address=ip,
            user_agent=user_agent,
            success=True,
        )

        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("login: DB error for user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Login failed. Please try again.",
        )

    _send_security_email(
        "Admin Login",
        f"Successful login from {request.client.host} at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    )
    _set_refresh_cookie(response, raw_refresh, settings)
    return _build_token_response(access_token)


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate refresh token and issue a new access token",
)
@limiter.limit("20/minute")
async def refresh_token_endpoint(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Optional[RefreshRequest] = None,
    cookie_token: Annotated[Optional[str], Cookie(alias="refresh_token")] = None,
) -> TokenResponse:
    settings = get_settings()

    # Resolve the raw refresh token — body wins over cookie.
    raw_token: Optional[str] = None
    if body is not None:
        raw_token = body.refresh_token
    elif cookie_token is not None:
        raw_token = cookie_token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    incoming_hash = _hash_token(raw_token)

    # --- validate stored token -------------------------------------------- #
    try:
        result = await db.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == incoming_hash)
            .with_for_update()
        )
        stored: Optional[RefreshToken] = result.scalar_one_or_none()
    except Exception:
        await db.rollback()
        logger.exception("refresh: DB lookup failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token validation failed. Please try again.",
        )

    _invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if stored is None or stored.revoked:
        raise _invalid

    now = datetime.now(tz=timezone.utc)
    expires_at = stored.expires_at
    # SQLAlchemy may return a naive datetime from some drivers; normalise.
    if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise _invalid

    # --- validate owning user --------------------------------------------- #
    try:
        result = await db.execute(select(User).where(User.id == stored.user_id))
        user: Optional[User] = result.scalar_one_or_none()
    except Exception:
        await db.rollback()
        logger.exception("refresh: user lookup failed for user_id=%s", stored.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token validation failed. Please try again.",
        )

    if user is None or not user.is_active:
        raise _invalid

    # --- rotate tokens ---------------------------------------------------- #
    try:
        # Revoke the consumed token.
        stored.revoked = True
        stored.revoked_at = now
        db.add(stored)

        # Issue new pair.
        new_access = create_access_token(subject=str(user.id))
        new_raw_refresh, new_hash = create_refresh_token()

        ip = get_remote_address(request)
        new_refresh_record = RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
            ip_address=ip,
        )
        db.add(new_refresh_record)

        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("refresh: rotation failed for user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token rotation failed. Please log in again.",
        )

    _set_refresh_cookie(response, new_raw_refresh, settings)
    return _build_token_response(new_access)


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the active refresh token and clear the cookie",
)
@limiter.limit("20/minute")
async def logout(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(_require_current_user)],
    body: Optional[RefreshRequest] = None,
    cookie_token: Annotated[Optional[str], Cookie(alias="refresh_token")] = None,
) -> None:
    ip = get_remote_address(request)
    user_agent: str = request.headers.get("user-agent", "")

    # Resolve the raw refresh token — body wins over cookie.
    raw_token: Optional[str] = None
    if body is not None:
        raw_token = body.refresh_token
    elif cookie_token is not None:
        raw_token = cookie_token

    if raw_token:
        incoming_hash = _hash_token(raw_token)
        try:
            result = await db.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == incoming_hash,
                    RefreshToken.user_id == current_user.id,
                    RefreshToken.revoked.is_(False),
                )
            )
            stored: Optional[RefreshToken] = result.scalar_one_or_none()
            if stored is not None:
                stored.revoked = True
                stored.revoked_at = datetime.now(tz=timezone.utc)
                db.add(stored)
        except Exception:
            await db.rollback()
            logger.exception("logout: token revocation DB error for user_id=%s", current_user.id)
            # Non-fatal: proceed to clear cookie and audit.

    try:
        await audit.record(
            db,
            "logout",
            user_id=current_user.id,
            resource="users",
            ip_address=ip,
            user_agent=user_agent,
            success=True,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("logout: audit/commit failed for user_id=%s", current_user.id)

    _clear_refresh_cookie(response)


# ---------------------------------------------------------------------------
# DELETE /auth/sessions  — revoke all refresh tokens ("logout everywhere")
# ---------------------------------------------------------------------------


@router.delete(
    "/sessions",
    summary="Revoke all active sessions (logout everywhere)",
)
@limiter.limit("10/minute")
async def revoke_all_sessions(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(_require_current_user)],
) -> dict:
    ip = get_remote_address(request)
    user_agent: str = request.headers.get("user-agent", "")

    # Count tokens before deletion so we can return the number revoked.
    count_result = await db.execute(
        select(RefreshToken).where(RefreshToken.user_id == current_user.id)
    )
    tokens = count_result.scalars().all()
    revoked_count = len(tokens)

    try:
        await db.execute(
            delete(RefreshToken).where(RefreshToken.user_id == current_user.id)
        )

        await audit.record(
            db,
            "auth.sessions.revoke_all",
            user_id=current_user.id,
            resource="users",
            ip_address=ip,
            user_agent=user_agent,
            success=True,
            detail={"revoked": revoked_count},
        )

        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception(
            "revoke_all_sessions: DB error for user_id=%s", current_user.id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke sessions. Please try again.",
        )

    _send_security_email(
        "All Sessions Revoked",
        f"All sessions revoked for {current_user.email} from {request.client.host}",
    )
    _clear_refresh_cookie(response)
    return {"revoked": revoked_count}


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Return the authenticated user's profile",
)
@limiter.limit("60/minute")
async def get_me(
    request: Request,
    current_user: Annotated[User, Depends(_require_current_user)],
) -> UserResponse:
    return UserResponse.model_validate(current_user)
