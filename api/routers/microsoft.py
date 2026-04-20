"""
EdgePulse Microsoft OAuth2 router — /auth/microsoft

Endpoints:
  GET /auth/microsoft/login     Redirect to Microsoft authorization page
  GET /auth/microsoft/callback  Handle Microsoft OAuth2 callback and issue tokens
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Annotated, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from api import audit
from api.config import get_settings
from api.database import get_db
from api.models import RefreshToken, User
from api.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
)

logger = logging.getLogger(__name__)


def _send_security_email(subject: str, body: str) -> None:
    import json, urllib.request
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
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


router = APIRouter(prefix="/auth/microsoft", tags=["auth"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_EMAIL = "acidtechrb@outlook.com"
_REDIRECT_URI = "https://edgepulse.us/auth/microsoft/callback"
_TENANT = "consumers"
_AUTHORIZE_URL = f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/authorize"
_TOKEN_URL = f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/token"
_SCOPE = "openid email profile"
_STATE_TTL = 300  # seconds
_STATE_KEY_PREFIX = "ep:oauth:state:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(settings.redis_url, decode_responses=True)


def _decode_id_token_payload(id_token: str) -> dict:
    """
    Decode the JWT payload from an id_token without signature verification.
    Safe here because the token arrived directly from Microsoft over TLS.
    """
    parts = id_token.split(".")
    if len(parts) < 2:
        raise ValueError("Invalid id_token format")
    payload_b64 = parts[1]
    # Pad to a multiple of 4 for standard base64 decoding
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


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


def _build_token_response(access_token: str) -> dict:
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


# ---------------------------------------------------------------------------
# GET /auth/microsoft/login
# ---------------------------------------------------------------------------


@router.get(
    "/login",
    summary="Redirect to Microsoft OAuth2 authorization page",
    include_in_schema=True,
)
async def microsoft_login() -> RedirectResponse:
    settings = get_settings()

    if not settings.microsoft_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Microsoft login not configured",
        )

    state = secrets.token_hex(32)

    redis = await _get_redis()
    try:
        await redis.setex(f"{_STATE_KEY_PREFIX}{state}", _STATE_TTL, "1")
    finally:
        await redis.aclose()

    from urllib.parse import urlencode
    params = urlencode({
        "client_id":     settings.microsoft_client_id,
        "response_type": "code",
        "redirect_uri":  _REDIRECT_URI,
        "scope":         _SCOPE,
        "state":         state,
    })
    return RedirectResponse(url=f"{_AUTHORIZE_URL}?{params}", status_code=302)


# ---------------------------------------------------------------------------
# GET /auth/microsoft/callback
# ---------------------------------------------------------------------------


@router.get(
    "/callback",
    summary="Handle Microsoft OAuth2 callback and issue EdgePulse tokens",
    include_in_schema=True,
)
async def microsoft_callback(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: str = Query(...),
    state: str = Query(...),
) -> dict:
    settings = get_settings()
    ip: str = request.client.host if request.client else "unknown"
    user_agent: str = request.headers.get("user-agent", "")

    # --- validate state (one-time use) ------------------------------------- #
    redis = await _get_redis()
    try:
        state_key = f"{_STATE_KEY_PREFIX}{state}"
        stored = await redis.get(state_key)
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired OAuth state",
            )
        await redis.delete(state_key)
    finally:
        await redis.aclose()

    # --- exchange code for tokens ------------------------------------------ #
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.microsoft_client_id,
                    "client_secret": settings.microsoft_client_secret,
                    "redirect_uri": _REDIRECT_URI,
                    "scope": _SCOPE,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception:
        logger.exception("microsoft_callback: HTTP error contacting Microsoft token endpoint")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to contact Microsoft token endpoint",
        )

    if token_resp.status_code != 200:
        logger.error(
            "microsoft_callback: Microsoft token endpoint returned %d: %s",
            token_resp.status_code,
            token_resp.text,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Microsoft token exchange failed",
        )

    try:
        token_data = token_resp.json()
    except Exception:
        logger.exception("microsoft_callback: failed to parse Microsoft token response")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid response from Microsoft",
        )

    id_token: Optional[str] = token_data.get("id_token")
    if not id_token:
        logger.error("microsoft_callback: no id_token in Microsoft response")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No id_token in Microsoft response",
        )

    # --- extract email from id_token payload ------------------------------- #
    try:
        payload = _decode_id_token_payload(id_token)
    except Exception:
        logger.exception("microsoft_callback: failed to decode id_token payload")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to decode Microsoft id_token",
        )

    email: Optional[str] = payload.get("email")
    if not email:
        logger.error("microsoft_callback: no email claim in id_token payload=%s", payload)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No email claim in Microsoft id_token",
        )

    # --- whitelist check --------------------------------------------------- #
    if email.lower() != _ALLOWED_EMAIL:
        logger.warning("microsoft_callback: unauthorized email attempt: %s", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )

    # --- look up user in DB ------------------------------------------------ #
    result = await db.execute(select(User).where(User.email == _ALLOWED_EMAIL))
    user: Optional[User] = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )

    # --- issue tokens (same flow as password login) ------------------------ #
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
            "auth.microsoft.login",
            user_id=user.id,
            resource="users",
            ip_address=ip,
            user_agent=user_agent,
            success=True,
            detail={"email": email},
        )

        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("microsoft_callback: DB error for user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Login failed. Please try again.",
        )

    _send_security_email(
        "Admin Login (Microsoft OAuth)",
        f"Successful Microsoft OAuth login for {email} from {ip} at {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    )
    _set_refresh_cookie(response, raw_refresh, settings)
    redirect = RedirectResponse(url=f"/dashboard?ms_token={access_token}", status_code=302)
    _set_refresh_cookie(redirect, raw_refresh, settings)
    return redirect
