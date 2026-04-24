"""
api/routers/keys.py — API key vault router for EdgePulse multi-tenant SaaS.

Handles encrypted storage and retrieval of per-user exchange credentials
(Kalshi, Coinbase). Key material is encrypted with AES-256-GCM at rest and
is NEVER returned in API responses or written to logs.

Endpoints:
  POST   /keys              — Store (upsert) credentials for an exchange
  GET    /keys              — List stored key metadata (no key material)
  DELETE /keys/{exchange}   — Remove stored credentials
  GET    /keys/{exchange}/verify — Connectivity test using stored credentials
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record as audit_record
from api.dependencies import get_current_user, get_db
from api.routers.auth import limiter
from api.models import APIKeyStore, User
from api.schemas import APIKeyResponse, APIKeyStoreRequest, ExchangeType
from api.security import decrypt_value, encrypt_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/keys", tags=["keys"])

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_KALSHI_BOT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "kalshi_bot")
_KALSHI_BASE_URL = os.environ.get(
    "KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2"
)


def _b64(data: bytes) -> str:
    """Base64-encode bytes to a URL-safe string."""
    return base64.b64encode(data).decode()


def _fromb64(s: str) -> bytes:
    """Decode a base64 string back to bytes."""
    return base64.b64decode(s)


def _pack_encrypted(ct: bytes, iv: bytes, tag: bytes) -> bytes:
    """
    Serialize an (ciphertext, iv, tag) triple as a JSON blob stored in a
    LargeBinary column.  Using JSON avoids fragile fixed-length slicing and
    makes the on-disk format self-describing.
    """
    blob = json.dumps({"ct": _b64(ct), "iv": _b64(iv), "tag": _b64(tag)})
    return blob.encode()


def _unpack_encrypted(raw: bytes) -> tuple[bytes, bytes, bytes]:
    """
    Deserialize a blob produced by :func:`_pack_encrypted`.

    Returns:
        (ciphertext, iv, tag)

    Raises:
        ValueError: if the blob is malformed or missing required keys.
    """
    try:
        obj = json.loads(raw.decode())
        ct  = _fromb64(obj["ct"])
        iv  = _fromb64(obj["iv"])
        tag = _fromb64(obj["tag"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed encrypted blob: {exc}") from exc
    return ct, iv, tag


def _validate_private_key(value: str) -> None:
    """
    Enforce that the supplied private_key is either a PEM block or a secret
    string of at least 32 characters.

    Raises:
        HTTPException(422): on validation failure.
    """
    if value.startswith("-----BEGIN"):
        return  # Looks like a PEM key — accept
    if len(value) >= 32:
        return  # Sufficiently long opaque secret — accept
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            "private_key must be a PEM key (starting with '-----BEGIN') "
            "or a secret string of at least 32 characters."
        ),
    )


def _get_client_ip(request: Request) -> str | None:
    """Extract best-effort client IP from request headers."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else None


async def _fetch_key_record(
    db: AsyncSession,
    user_id: uuid.UUID,
    exchange: ExchangeType,
) -> APIKeyStore | None:
    """Return the APIKeyStore row for (user_id, exchange), or None."""
    result = await db.execute(
        select(APIKeyStore).where(
            APIKeyStore.user_id == user_id,
            APIKeyStore.exchange == exchange.value,
        )
    )
    return result.scalar_one_or_none()


def _decrypt_key_record(record: APIKeyStore) -> tuple[str, str]:
    """
    Decrypt key_id and private_key from an APIKeyStore row.

    Returns:
        (key_id_plaintext, private_key_plaintext)

    Raises:
        HTTPException(422): if either field cannot be decrypted (tampered/corrupt).
    """
    try:
        ct_kid, iv_kid, tag_kid = _unpack_encrypted(record.key_id_enc)
        key_id = decrypt_value(ct_kid, iv_kid, tag_kid)
    except Exception:
        logger.warning(
            "key_id decryption failed for user_id=%s exchange=%s",
            record.user_id,
            record.exchange,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Key data corrupted",
        )

    try:
        ct_pk, iv_pk, tag_pk = _unpack_encrypted(record.private_key_enc)
        private_key = decrypt_value(ct_pk, iv_pk, tag_pk)
    except Exception:
        logger.warning(
            "private_key decryption failed for user_id=%s exchange=%s",
            record.user_id,
            record.exchange,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Key data corrupted",
        )

    return key_id, private_key


# ---------------------------------------------------------------------------
# POST /keys — store (upsert) credentials
# ---------------------------------------------------------------------------

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=APIKeyResponse,
    summary="Store exchange API credentials",
)
@limiter.limit("30/minute")
async def store_key(
    body: APIKeyStoreRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIKeyResponse:
    """
    Encrypt and store (or replace) the caller's credentials for the given
    exchange.  Existing credentials for the same exchange are overwritten
    in-place; no duplicate rows are created.

    The raw key values are encrypted with AES-256-GCM using independent IVs
    for key_id and private_key, then discarded from memory.  They are never
    written to logs or returned in responses.
    """
    _validate_private_key(body.private_key)

    ip = _get_client_ip(request)
    ua = request.headers.get("User-Agent")
    exchange = body.exchange
    success = False

    try:
        # Encrypt key_id and private_key with separate IVs.
        ct_kid, iv_kid, tag_kid = encrypt_value(body.key_id)
        ct_pk, iv_pk, tag_pk    = encrypt_value(body.private_key)

        key_id_blob   = _pack_encrypted(ct_kid, iv_kid, tag_kid)
        priv_key_blob = _pack_encrypted(ct_pk, iv_pk, tag_pk)

        existing = await _fetch_key_record(db, current_user.id, exchange)

        if existing is not None:
            # Update in-place to avoid violating the unique constraint.
            existing.key_id_enc      = key_id_blob
            existing.private_key_enc = priv_key_blob
            # Reset legacy iv/tag columns (not used for lookup; kept non-null).
            existing.iv              = iv_kid
            existing.tag             = tag_kid
            db.add(existing)
            row = existing
        else:
            row = APIKeyStore(
                user_id         = current_user.id,
                exchange        = exchange.value,
                key_id_enc      = key_id_blob,
                private_key_enc = priv_key_blob,
                # iv/tag columns are legacy scaffolding; pack kid iv/tag here
                # so the NOT NULL constraint is satisfied.
                iv              = iv_kid,
                tag             = tag_kid,
            )
            db.add(row)

        await db.flush()
        await db.refresh(row)
        success = True

        response = APIKeyResponse(
            id           = row.id,
            exchange     = ExchangeType(row.exchange),
            created_at   = row.created_at,
            last_used_at = row.last_used_at,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to store key for user=%s exchange=%s", current_user.id, exchange)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store credentials",
        ) from exc
    finally:
        await audit_record(
            db,
            action="key_store",
            user_id=current_user.id,
            resource=f"key:{exchange.value}",
            ip_address=ip,
            user_agent=ua,
            success=success,
        )

    return response


# ---------------------------------------------------------------------------
# GET /keys — list stored key metadata
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=list[APIKeyResponse],
    summary="List stored exchange credential metadata",
)
@limiter.limit("30/minute")
async def list_keys(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[APIKeyResponse]:
    """
    Return metadata for all exchange credentials stored by the authenticated
    user.  Key material is never included in the response.
    """
    result = await db.execute(
        select(APIKeyStore).where(APIKeyStore.user_id == current_user.id)
    )
    rows = result.scalars().all()

    return [
        APIKeyResponse(
            id           = row.id,
            exchange     = ExchangeType(row.exchange),
            created_at   = row.created_at,
            last_used_at = row.last_used_at,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# DELETE /keys/{exchange} — remove credentials
# ---------------------------------------------------------------------------

@router.delete(
    "/{exchange}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete stored credentials for an exchange",
)
@limiter.limit("30/minute")
async def delete_key(
    exchange: ExchangeType,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Permanently remove the authenticated user's stored credentials for the
    given exchange.
    """
    ip = _get_client_ip(request)
    ua = request.headers.get("User-Agent")
    success = False

    try:
        row = await _fetch_key_record(db, current_user.id, exchange)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No credentials stored for exchange '{exchange.value}'",
            )
        await db.delete(row)
        await db.flush()
        success = True
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Failed to delete key for user=%s exchange=%s", current_user.id, exchange
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete credentials",
        ) from exc
    finally:
        await audit_record(
            db,
            action="key_delete",
            user_id=current_user.id,
            resource=f"key:{exchange.value}",
            ip_address=ip,
            user_agent=ua,
            success=success,
        )


# ---------------------------------------------------------------------------
# GET /keys/{exchange}/verify — connectivity test
# ---------------------------------------------------------------------------

@router.get(
    "/{exchange}/verify",
    summary="Test connectivity using stored credentials",
)
@limiter.limit("30/minute")
async def verify_key(
    exchange: ExchangeType,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Decrypt the stored credentials for *exchange* in-memory and attempt a
    lightweight connectivity / authentication check against the exchange's
    API.

    Key material is:
      - decrypted in-memory only
      - never stored to any intermediate variable that outlives this function
      - never written to logs or returned in the response body

    On success updates ``last_used_at`` and returns ``{"status": "ok", ...}``.
    On failure returns ``{"status": "error", "detail": "..."}`` (safe message
    only; no key material, no stack traces).
    """
    ip = _get_client_ip(request)
    ua = request.headers.get("User-Agent")
    verify_ok = False
    result_payload: dict[str, Any] = {}

    row = await _fetch_key_record(db, current_user.id, exchange)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No credentials stored for exchange '{exchange.value}'",
        )

    # _decrypt_key_record raises 422 "Key data corrupted" on any failure.
    key_id, private_key = _decrypt_key_record(row)

    try:
        if exchange == ExchangeType.kalshi:
            result_payload = await _verify_kalshi(key_id, private_key)
        elif exchange == ExchangeType.coinbase:
            result_payload = await _verify_coinbase(key_id, private_key)
        else:
            # Defensive: unreachable given the ExchangeType enum, but guard anyway.
            result_payload = {"status": "error", "detail": "Unknown exchange"}

        verify_ok = result_payload.get("status") == "ok"

    except HTTPException:
        raise
    except Exception:
        # Do NOT log the exception with key material in scope; only log context.
        logger.warning(
            "Unexpected error during key verification user=%s exchange=%s",
            current_user.id,
            exchange.value,
        )
        result_payload = {
            "status": "error",
            "detail": "Connectivity check failed unexpectedly",
        }
    finally:
        # Clear local references to decrypted material as early as possible.
        del key_id, private_key

        await audit_record(
            db,
            action="key_verify",
            user_id=current_user.id,
            resource=f"key:{exchange.value}",
            ip_address=ip,
            user_agent=ua,
            success=verify_ok,
        )

    if verify_ok:
        try:
            row.last_used_at = datetime.now(tz=timezone.utc)
            db.add(row)
            await db.flush()
        except Exception:
            # Non-fatal — don't let a timestamp update failure mask a successful verify.
            logger.warning(
                "Failed to update last_used_at for user=%s exchange=%s",
                current_user.id,
                exchange.value,
            )

    return result_payload


# ---------------------------------------------------------------------------
# Kalshi connectivity helper
# ---------------------------------------------------------------------------

async def _verify_coinbase(key_id: str, private_key: str) -> dict[str, Any]:
    """
    Verify Coinbase credentials by calling GET /api/v3/brokerage/accounts.
    Uses ES256 JWT signing identical to positions.py (_cb_make_jwt).
    Returns {"status": "ok", "exchange": "coinbase"} or {"status": "error", ...}.
    """
    import time as _time
    import httpx

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _make_jwt(key_name: str, pem: str, method: str, path: str) -> str:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        now = int(_time.time())
        header  = {"alg": "ES256", "kid": key_name}
        payload = {
            "sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
            "uri": f"{method} api.coinbase.com{path}",
        }
        h64 = _b64url(json.dumps(header,  separators=(",", ":")).encode())
        p64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{h64}.{p64}".encode()
        priv = serialization.load_pem_private_key(pem.encode(), password=None)
        der_sig = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r_val, s_val = decode_dss_signature(der_sig)
        raw_sig = r_val.to_bytes(32, "big") + s_val.to_bytes(32, "big")
        return f"{h64}.{p64}.{_b64url(raw_sig)}"

    path = "/api/v3/brokerage/accounts"
    try:
        jwt_token = _make_jwt(key_id, private_key, "GET", path)
    except Exception as exc:
        logger.warning("Coinbase verify JWT error: %s", type(exc).__name__)
        return {"status": "error", "detail": "Invalid private key — expected EC P-256 PEM"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(
                f"https://api.coinbase.com{path}",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
    except Exception as exc:
        logger.warning("Coinbase verify request failed: %s", type(exc).__name__)
        return {"status": "error", "detail": "Network error contacting Coinbase API"}

    if resp.status_code == 200:
        return {"status": "ok", "exchange": "coinbase"}
    if resp.status_code == 401:
        return {"status": "error", "detail": "Authentication failed — check key name and PEM"}
    if resp.status_code == 403:
        return {"status": "error", "detail": "Key authenticated but missing required permissions"}
    return {"status": "error", "detail": f"Coinbase API returned HTTP {resp.status_code}"}


async def _verify_kalshi(key_id: str, private_key: str) -> dict[str, Any]:
    """
    Use the stored Kalshi credentials to call GET /portfolio/balance.

    The private key is written to a NamedTemporaryFile (mode 0o600) that is
    deleted immediately after KalshiAuth loads it.  The plaintext never
    touches disk in any other form.

    Returns:
        {"status": "ok", "exchange": "kalshi"}  on HTTP 200
        {"status": "error", "detail": "<safe message>"}  on any failure
    """
    # Ensure kalshi_bot is importable.
    kalshi_bot_path = os.path.normpath(_KALSHI_BOT_PATH)
    if kalshi_bot_path not in sys.path:
        sys.path.insert(0, kalshi_bot_path)

    try:
        from auth import KalshiAuth  # type: ignore[import]
        from client import KalshiClient  # type: ignore[import]
    except ImportError as exc:
        logger.error("Failed to import kalshi_bot modules: %s", exc)
        return {"status": "error", "detail": "Internal configuration error"}

    tmp_path: str | None = None
    try:
        # Write the PEM key to a secure temp file; KalshiAuth requires a Path.
        fd, tmp_path = tempfile.mkstemp(suffix=".pem", prefix="ep_kv_")
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(private_key)
            fd = None  # fdopen took ownership; prevent double-close

            from pathlib import Path as _Path
            auth   = KalshiAuth(api_key_id=key_id, private_key_path=_Path(tmp_path))
            client = KalshiClient(base_url=_KALSHI_BASE_URL, auth=auth, timeout=8)
        finally:
            # Remove temp file immediately — auth has already loaded the key.
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        # Perform the connectivity check synchronously (KalshiClient.get is sync).
        # Run in a thread pool to avoid blocking the async event loop.
        import asyncio
        import functools

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            functools.partial(client.get, "/portfolio/balance"),
        )
        return {"status": "ok", "exchange": "kalshi"}

    except Exception as exc:
        # Log only a safe, non-sensitive description.
        logger.warning("Kalshi connectivity check failed: %s", type(exc).__name__)
        detail = _safe_kalshi_error(exc)
        return {"status": "error", "detail": detail}


def _safe_kalshi_error(exc: Exception) -> str:
    """
    Map an exception from the Kalshi connectivity check to a user-visible
    message that contains no key material or internal paths.
    """
    exc_type = type(exc).__name__
    msg = str(exc)

    # requests / httpx HTTP errors carry a status code we can safely surface.
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        code: int = exc.response.status_code
        if code == 401:
            return "Authentication failed — check your key ID and private key"
        if code == 403:
            return "Permission denied — verify key permissions on Kalshi"
        if code == 429:
            return "Rate limited by Kalshi — try again shortly"
        return f"Kalshi API returned HTTP {code}"

    if "Timeout" in exc_type or "timeout" in msg.lower():
        return "Connection timed out — Kalshi API unreachable"
    if "Connection" in exc_type or "connect" in msg.lower():
        return "Could not connect to Kalshi API"

    return "Kalshi connectivity check failed"
