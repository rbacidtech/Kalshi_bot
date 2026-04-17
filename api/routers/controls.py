"""
api/routers/controls.py — Bot configuration and status endpoints.

Endpoints (all admin-only):
  GET  /controls/config  — Current bot config (Redis overrides + env defaults)
  PATCH /controls/config — Write config overrides to Redis
  GET  /controls/status  — Latest bot status snapshot from Redis
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api.config import get_settings
from api.dependencies import require_admin
from api.models import User
from api.routers.auth import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/controls", tags=["controls"])

_CONFIG_KEY = "ep:bot:config"
_STATUS_KEY = "ep:bot:status"
_BALANCE_KEY = "ep:balance"


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(settings.redis_url, decode_responses=True)


def _env_defaults() -> dict[str, Any]:
    """Return defaults pulled from current env vars (same as config.py)."""
    def _float(k: str, d: float) -> float:
        try: return float(os.getenv(k, d))
        except ValueError: return d

    def _int(k: str, d: int) -> int:
        try: return int(os.getenv(k, d))
        except ValueError: return d

    def _bool(k: str, d: bool) -> bool:
        v = os.getenv(k)
        if v is None: return d
        return v.lower() in ("true", "1", "yes")

    return {
        "enable_fomc":         True,
        "enable_weather":      True,
        "enable_economic":     True,
        "enable_sports":       True,
        "enable_crypto_price": True,
        "enable_gdp":          True,
        "paper_trade":         _bool("KALSHI_PAPER_TRADE", False),
        "edge_threshold":      _float("KALSHI_EDGE_THRESHOLD", 0.10),
        "max_contracts":       _int("KALSHI_MAX_CONTRACTS", 5),
        "poll_interval":       _int("KALSHI_POLL_INTERVAL", 120),
        "min_confidence":      _float("KALSHI_MIN_CONFIDENCE", 0.60),
        "kelly_fraction":      _float("KALSHI_KELLY_FRACTION", 0.25),
        "max_market_exposure": _float("KALSHI_MAX_MARKET_EXPOSURE", 0.05),
        "daily_drawdown_limit":_float("KALSHI_DAILY_DRAWDOWN_LIMIT", 0.10),
    }


class BotConfig(BaseModel):
    enable_fomc:          bool  = True
    enable_weather:       bool  = True
    enable_economic:      bool  = True
    enable_sports:        bool  = True
    enable_crypto_price:  bool  = True
    enable_gdp:           bool  = True
    paper_trade:          bool  = False
    edge_threshold:       float = Field(0.10, ge=0.05, le=0.50)
    max_contracts:        int   = Field(5,    ge=1,    le=100)
    poll_interval:        int   = Field(120,  ge=30,   le=3600)
    min_confidence:       float = Field(0.60, ge=0.10, le=1.00)
    kelly_fraction:       float = Field(0.25, ge=0.05, le=1.00)
    max_market_exposure:  float = Field(0.05, ge=0.01, le=0.50)
    daily_drawdown_limit: float = Field(0.10, ge=0.01, le=0.50)


@router.get("/config", response_model=BotConfig)
@limiter.limit("60/minute")
async def get_config(
    request: Request,
    _: User = Depends(require_admin),
) -> BotConfig:
    """Return effective bot config: Redis overrides merged over env defaults."""
    r = await _get_redis()
    try:
        raw = await r.get(_CONFIG_KEY)
    finally:
        await r.aclose()

    cfg = _env_defaults()
    if raw:
        try:
            cfg.update(json.loads(raw))
        except Exception:
            logger.warning("controls: failed to parse ep:bot:config, using defaults")

    return BotConfig(**cfg)


@router.patch("/config", response_model=BotConfig)
@limiter.limit("30/minute")
async def patch_config(
    body: BotConfig,
    request: Request,
    _: User = Depends(require_admin),
) -> BotConfig:
    """Write config overrides to Redis. Bot picks them up on next cycle."""
    r = await _get_redis()
    try:
        await r.set(_CONFIG_KEY, body.model_dump_json())
    finally:
        await r.aclose()
    return body


@router.get("/status")
@limiter.limit("60/minute")
async def get_status(
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Return latest bot status from Redis (cycle count, last scan, balance)."""
    r = await _get_redis()
    try:
        raw_status  = await r.get(_STATUS_KEY)
        raw_balance = await r.hgetall(_BALANCE_KEY)
    finally:
        await r.aclose()

    status: dict[str, Any] = {}
    if raw_status:
        try:
            status = json.loads(raw_status)
        except Exception:
            pass

    # Surface the most recent balance timestamp as a proxy for "bot alive"
    last_balance_at: Optional[str] = None
    for v in raw_balance.values():
        try:
            obj = json.loads(v)
            ts = obj.get("updated_at") or obj.get("timestamp")
            if ts:
                last_balance_at = ts
                break
        except Exception:
            pass

    status.setdefault("last_balance_at", last_balance_at)
    return status
