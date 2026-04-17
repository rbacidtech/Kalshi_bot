from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid as _uuid
from typing import Any, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request

from api.audit import record
from api.config import get_settings
from api.dependencies import get_current_user, require_admin
from api.models import User
from api.routers.auth import limiter
from api.schemas import PortfolioResponse, PositionResponse, PositionSide

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/positions", tags=["positions"])


def _redis_key(user_id: str, key: str) -> str:
    """
    Namespace Redis keys per-user for multi-tenant isolation.
    Owner account (system) uses the bare key; subscriber accounts use ep:{uid}:{key}.
    """
    return f"ep:{user_id}:{key}"


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(settings.redis_url, decode_responses=False)


def _compute_pnl(pos: dict[str, Any], prices: dict[str, Any]) -> Optional[int]:
    ticker   = pos.get("ticker", "")
    side     = pos.get("side", "yes")
    contracts = int(pos.get("contracts", 0))
    entry    = int(pos.get("entry_cents", 0))
    price_data = prices.get(ticker, {})
    cur_yes  = price_data.get("yes_price")
    if cur_yes is None:
        return None
    cur_yes = int(cur_yes)
    if side == "yes":
        return (cur_yes - entry) * contracts
    else:
        return (entry - cur_yes) * contracts


@router.get("", response_model=PortfolioResponse)
@limiter.limit("60/minute")
async def get_positions(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> PortfolioResponse:
    """
    Return all open positions and portfolio summary for the authenticated user.
    Owner (admin/system) account reads from the canonical ep:positions key.
    Subscriber accounts read from their namespaced ep:{uid}:positions key.
    """
    r = await _get_redis()
    try:
        uid = str(current_user.id)
        pos_key   = "ep:positions" if current_user.is_admin else _redis_key(uid, "positions")
        price_key = "ep:prices"

        raw_positions: dict = await r.hgetall(pos_key)
        raw_prices: dict    = await r.hgetall(price_key)

        prices: dict[str, Any] = {}
        for k, v in raw_prices.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                prices[key] = json.loads(v)
            except Exception:
                pass

        positions: list[PositionResponse] = []
        total_deployed   = 0
        total_unrealized = 0

        for raw_key, raw_val in raw_positions.items():
            ticker = (raw_key.decode() if isinstance(raw_key, bytes) else raw_key)
            try:
                p = json.loads(raw_val)
            except Exception:
                continue

            contracts = int(p.get("contracts", 0))
            if contracts == 0:
                continue

            side      = p.get("side", "yes")
            entry     = int(p.get("entry_cents", 0))
            cost      = (100 - entry) * contracts if side == "no" else entry * contracts
            pnl       = _compute_pnl({**p, "ticker": ticker}, prices)

            total_deployed += cost
            if pnl is not None:
                total_unrealized += pnl

            positions.append(PositionResponse(
                ticker            = ticker,
                side              = PositionSide(side),
                contracts         = contracts,
                entry_cents       = entry,
                fair_value        = p.get("fair_value"),
                fill_confirmed    = bool(p.get("fill_confirmed", False)),
                entered_at        = p.get("entered_at"),
                close_time        = p.get("close_time"),
                unrealized_pnl_cents = pnl,
            ))

        # Balance from Redis (intel node publishes each cycle)
        raw_balance = await r.hgetall("ep:balance")
        balance_cents: Optional[int] = None
        for k, v in raw_balance.items():
            key = k.decode() if isinstance(k, bytes) else k
            if "intel" in key.lower() or "kalshi" in key.lower():
                try:
                    balance_cents = int(json.loads(v).get("balance_cents", 0))
                    break
                except Exception:
                    pass

        return PortfolioResponse(
            positions               = sorted(positions, key=lambda p: p.ticker),
            total_deployed_cents    = total_deployed,
            total_unrealized_pnl_cents = total_unrealized,
            balance_cents           = balance_cents,
            position_count          = len(positions),
        )
    finally:
        await r.aclose()


@router.get("/prices")
@limiter.limit("60/minute")
async def get_prices(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return latest price snapshot from Redis (read-only market data, shared across all users)."""
    r = await _get_redis()
    try:
        raw = await r.hgetall("ep:prices")
        result: dict[str, Any] = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                result[key] = json.loads(v)
            except Exception:
                pass
        return result
    finally:
        await r.aclose()


@router.get("/balance")
@limiter.limit("60/minute")
async def get_balance(
    request: Request,
    current_user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Return latest Kalshi balance published by Intel node."""
    r = await _get_redis()
    try:
        raw = await r.hgetall("ep:balance")
        result: dict[str, Any] = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                result[key] = json.loads(v)
            except Exception:
                pass
        return result
    finally:
        await r.aclose()


def _cb_b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _cb_make_jwt(key_name: str, pem: str, method: str, path: str) -> str:
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    import json as _json

    now = int(time.time())
    header  = {"alg": "ES256", "kid": key_name}
    payload = {
        "sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
        "uri": f"{method} api.coinbase.com{path}",
    }
    h64 = _cb_b64url(_json.dumps(header,  separators=(",", ":")).encode())
    p64 = _cb_b64url(_json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h64}.{p64}".encode()
    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_val, s_val = decode_dss_signature(der_sig)
    raw_sig = r_val.to_bytes(32, "big") + s_val.to_bytes(32, "big")
    return f"{h64}.{p64}.{_cb_b64url(raw_sig)}"


@router.get("/coinbase")
@limiter.limit("30/minute")
async def get_coinbase_balance(
    request: Request,
    current_user: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    Fetch live Coinbase account balances using credentials from env.
    Returns USD cash and BTC holdings.
    """
    import httpx

    key_name = os.getenv("COINBASE_API_KEY_NAME", "")
    key_path = os.getenv("COINBASE_PRIVATE_KEY_PATH", "")

    if not key_name:
        return {"error": "COINBASE_API_KEY_NAME not configured"}

    pem = ""
    if key_path and os.path.exists(key_path):
        with open(key_path) as fh:
            pem = fh.read()
    else:
        pem = os.getenv("COINBASE_PRIVATE_KEY_PEM", "").replace("\\n", "\n")

    if not pem:
        return {"error": "Coinbase private key not found"}

    path = "/api/v3/brokerage/accounts"
    try:
        jwt_token = _cb_make_jwt(key_name, pem, "GET", path)
    except Exception as exc:
        logger.warning("Coinbase balance JWT error: %s", exc)
        return {"error": f"JWT error: {exc}"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(
                f"https://api.coinbase.com{path}",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
    except Exception as exc:
        logger.warning("Coinbase balance request error: %s", exc)
        return {"error": f"Request failed: {exc}"}

    if resp.status_code != 200:
        logger.warning("Coinbase balance HTTP %d: %s", resp.status_code, resp.text[:200])
        return {"error": f"Coinbase API returned HTTP {resp.status_code}"}

    try:
        accounts = resp.json().get("accounts", [])
    except Exception:
        return {"error": "Failed to parse Coinbase response"}

    result: dict[str, Any] = {"accounts": []}
    total_usd_cents = 0
    btc_amount = 0.0

    for acct in accounts:
        currency = acct.get("currency", "")
        avail    = float(acct.get("available_balance", {}).get("value", "0") or "0")
        hold     = float(acct.get("hold", {}).get("value", "0") or "0")
        if avail <= 0 and hold <= 0:
            continue
        entry: dict[str, Any] = {
            "currency":  currency,
            "available": avail,
            "hold":      hold,
        }
        result["accounts"].append(entry)
        if currency == "USD":
            total_usd_cents = int(avail * 100)
            result["usd_available"] = avail
            result["usd_cents"]     = total_usd_cents
        elif currency == "BTC":
            btc_amount = avail
            result["btc_available"] = avail

    result["paper_mode"] = os.getenv("COINBASE_PAPER", "true").lower() in ("1", "true", "yes")
    return result
