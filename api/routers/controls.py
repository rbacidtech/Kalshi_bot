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
from datetime import datetime, timezone
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

_CONFIG_KEY  = "ep:bot:config"   # dashboard UI state (full JSON blob)
_BOT_CFG_KEY = "ep:config"       # bot live overrides (hash, read each cycle)
_STATUS_KEY  = "ep:bot:status"
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
    """Return effective bot config: env defaults → ep:bot:config UI state → ep:config live overrides."""
    r = await _get_redis()
    try:
        raw_ui   = await r.get(_CONFIG_KEY)
        raw_live = await r.hgetall(_BOT_CFG_KEY)
    finally:
        await r.aclose()

    cfg = _env_defaults()

    # Layer 1: dashboard-saved UI state
    if raw_ui:
        try:
            cfg.update(json.loads(raw_ui))
        except Exception:
            logger.warning("controls: failed to parse ep:bot:config, using defaults")

    # Layer 2: live bot overrides from ep:config hash (source of truth for bot)
    try:
        if raw_live.get("override_edge_threshold"):
            cfg["edge_threshold"] = float(raw_live["override_edge_threshold"])
        if raw_live.get("override_max_contracts"):
            cfg["max_contracts"] = int(float(raw_live["override_max_contracts"]))
        if raw_live.get("override_min_confidence"):
            cfg["min_confidence"] = float(raw_live["override_min_confidence"])
    except Exception:
        pass

    return BotConfig(**cfg)


@router.patch("/config", response_model=BotConfig)
@limiter.limit("30/minute")
async def patch_config(
    body: BotConfig,
    request: Request,
    _: User = Depends(require_admin),
) -> BotConfig:
    """Write config to Redis. UI state → ep:bot:config; live overrides → ep:config hash."""
    r = await _get_redis()
    try:
        # Full UI state for display
        await r.set(_CONFIG_KEY, body.model_dump_json())
        # Live overrides the bot reads each scan cycle
        await r.hset(_BOT_CFG_KEY, mapping={
            "override_edge_threshold": str(body.edge_threshold),
            "override_max_contracts":  str(body.max_contracts),
            "override_min_confidence": str(body.min_confidence),
        })
    finally:
        await r.aclose()
    return body


_HEALTH_KEY = "ep:health"


def _ts_us_to_iso(ts_us: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc).isoformat()
    except Exception:
        return None


@router.get("/status")
@limiter.limit("60/minute")
async def get_status(
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Return latest bot status built from ep:health and ep:balance hashes."""
    r = await _get_redis()
    try:
        raw_health  = await r.hgetall(_HEALTH_KEY)
        raw_balance = await r.hgetall(_BALANCE_KEY)
    finally:
        await r.aclose()

    status: dict[str, Any] = {}

    # Parse health from first available node entry
    for node_id, raw in raw_health.items():
        try:
            h = json.loads(raw)
            ws = h.get("sources", {}).get("kalshi_ws", {})
            status = {
                "node_id":      node_id,
                "health":       h.get("overall", "unknown"),
                "ws_connected": ws.get("status") == "ok",
                "last_cycle_at": _ts_us_to_iso(h.get("ts_us")),
                "sources":      h.get("sources", {}),
            }
            break
        except Exception:
            pass

    # Parse balance — sum across sources, pick mode and timestamp
    total_balance  = 0
    mode: Optional[str] = None
    last_balance_at: Optional[str] = None
    for raw in raw_balance.values():
        try:
            b = json.loads(raw)
            total_balance += b.get("balance_cents", 0)
            if not mode:
                mode = b.get("mode")
            if not last_balance_at:
                last_balance_at = _ts_us_to_iso(b.get("ts_us"))
        except Exception:
            pass

    status["balance_cents"]    = total_balance
    status["mode"]             = mode
    status["last_balance_at"]  = last_balance_at
    return status


@router.post("/ai-suggest")
@limiter.limit("6/minute")
async def ai_suggest(
    request: Request,
    body: dict,
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    Pass current config + question to Claude and get trading parameter suggestions.
    """
    import os
    import anthropic

    config   = body.get("config", {})
    question = body.get("question", "Review my current settings and suggest improvements.")
    perf     = body.get("performance", {})

    system_prompt = (
        "You are an expert algorithmic trading advisor for a Kalshi prediction market bot. "
        "The bot trades FOMC rate decisions, economic indicators, and crypto price markets. "
        "You help operators tune risk parameters to maximize edge while managing drawdown. "
        "Be concise, specific, and use bullet points. Limit your response to 300 words max."
    )

    config_summary = (
        f"Current config:\n"
        f"- edge_threshold: {config.get('edge_threshold', '?')} (min edge to enter)\n"
        f"- min_confidence: {config.get('min_confidence', '?')} (signal confidence gate)\n"
        f"- kelly_fraction: {config.get('kelly_fraction', '?')} (sizing fraction)\n"
        f"- max_contracts: {config.get('max_contracts', '?')} (position size cap)\n"
        f"- max_market_exposure: {config.get('max_market_exposure', '?')} (per-market %)\n"
        f"- daily_drawdown_limit: {config.get('daily_drawdown_limit', '?')}\n"
        f"- paper_trade: {config.get('paper_trade', '?')}\n"
        f"Active strategies: FOMC={config.get('enable_fomc')}, "
        f"Economic={config.get('enable_economic')}, Crypto={config.get('enable_crypto_price')}, "
        f"GDP={config.get('enable_gdp')}, Weather={config.get('enable_weather')}, Sports={config.get('enable_sports')}\n"
    )

    if perf and perf.get("total_trades", 0) > 0:
        config_summary += (
            f"\nRecent performance ({perf.get('period_days', 30)}d): "
            f"{perf.get('total_trades')} trades, "
            f"win rate {round(perf.get('win_rate', 0) * 100, 1)}%, "
            f"total P&L ${perf.get('total_pnl_cents', 0) / 100:.2f}, "
            f"Sharpe {perf.get('sharpe_daily', 'N/A')}"
        )

    user_message = f"{config_summary}\n\nQuestion: {question}"

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return {"suggestion": msg.content[0].text, "ok": True}
    except Exception as exc:
        return {"suggestion": f"Error: {exc}", "ok": False}
