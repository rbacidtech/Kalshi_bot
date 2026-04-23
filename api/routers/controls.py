"""
api/routers/controls.py — Bot configuration and status endpoints.

Endpoints (all admin-only):
  GET  /controls/config  — Current bot config (Redis overrides + env defaults)
  PATCH /controls/config — Write config overrides to Redis
  GET  /controls/status  — Latest bot status snapshot from Redis
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from api.dependencies import require_admin
from api.models import User
from api.redis_client import get_redis
from api.routers.auth import limiter
from api.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/controls", tags=["controls"])

_CONFIG_KEY  = "ep:bot:config"   # dashboard UI state (full JSON blob)
_BOT_CFG_KEY = "ep:config"       # bot live overrides (hash, read each cycle)
_STATUS_KEY  = "ep:bot:status"
_BALANCE_KEY = "ep:balance"


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
    r = get_redis()
    try:
        raw_ui   = await r.get(_CONFIG_KEY)
        raw_live = await r.hgetall(_BOT_CFG_KEY)
    except Exception:
        raw_ui, raw_live = None, {}

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
    r = get_redis()
    try:
        # Full UI state for display
        await r.set(_CONFIG_KEY, body.model_dump_json())
        # Live overrides the bot reads each scan cycle
        await r.hset(_BOT_CFG_KEY, mapping={
            "override_edge_threshold": str(body.edge_threshold),
            "override_max_contracts":  str(body.max_contracts),
            "override_min_confidence": str(body.min_confidence),
        })
    except Exception:
        pass
    return body


_HEALTH_KEY = "ep:health"


def _ts_us_to_iso(ts_us: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _node_heartbeats_from_stream(entries: list) -> dict[str, float]:
    """Scan ep:system entries (xrevrange order) → {node_prefix: latest_ts_s}."""
    seen: dict[str, float] = {}
    for _eid, fields in entries:
        raw = fields.get("payload") or fields.get(b"payload")
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        if ev.get("event_type") != "HEARTBEAT":
            continue
        node = str(ev.get("node", ""))
        ts_s = float(ev.get("ts_us", 0)) / 1_000_000
        # Keep only the first (latest) per node prefix
        prefix = "intel" if node.startswith("intel") else "exec" if node.startswith("exec") else node
        if prefix not in seen and ts_s:
            seen[prefix] = ts_s
        if len(seen) >= 2:
            break
    return seen


async def _session_pnl_from_db(db: AsyncSession) -> Optional[int]:
    """
    Compute today's session P&L from pnl_snapshots.
    Session P&L = (latest total value) - (first-of-day total value)
    Total value = balance_cents + deployed_cents + unrealized_pnl_cents
    Returns None if fewer than 2 snapshots exist for today.
    """
    try:
        today_utc = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = await db.execute(text("""
            SELECT balance_cents, deployed_cents, unrealized_pnl_cents
            FROM pnl_snapshots
            WHERE ts >= :today
            ORDER BY ts ASC
            LIMIT 1
        """), {"today": today_utc})
        start_row = result.fetchone()
        if start_row is None:
            return None

        result2 = await db.execute(text("""
            SELECT balance_cents, deployed_cents, unrealized_pnl_cents
            FROM pnl_snapshots
            ORDER BY ts DESC
            LIMIT 1
        """))
        latest_row = result2.fetchone()
        if latest_row is None:
            return None

        def _total(row) -> int:
            return (row[0] or 0) + (row[1] or 0) + (row[2] or 0)

        return _total(latest_row) - _total(start_row)
    except Exception as exc:
        logger.debug("session_pnl query failed: %s", exc)
        return None


@router.get("/status")
@limiter.limit("60/minute")
async def get_status(
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return latest bot status built from ep:health, ep:balance, and ep:system."""
    r = get_redis()
    try:
        raw_health, raw_balance, sys_entries = await asyncio.gather(
            r.hgetall(_HEALTH_KEY),
            r.hgetall(_BALANCE_KEY),
            r.xrevrange("ep:system", count=200),
            return_exceptions=True,
        )
        if isinstance(raw_health,  Exception): raw_health  = {}
        if isinstance(raw_balance, Exception): raw_balance = {}
        if isinstance(sys_entries, Exception): sys_entries = []
    except Exception:
        raw_health, raw_balance, sys_entries = {}, {}, []

    status: dict[str, Any] = {}
    now = datetime.now(timezone.utc).timestamp()

    # ── Intel node health (ep:health hash) ──────────────────────────────────
    for node_id, raw in raw_health.items():
        try:
            h  = json.loads(raw)
            ws = h.get("sources", {}).get("kalshi_ws", {})
            status = {
                "node_id":       node_id,
                "health":        h.get("overall", "unknown"),
                "ws_connected":  ws.get("status") == "ok",
                "last_cycle_at": _ts_us_to_iso(h.get("ts_us")),
                "sources":       h.get("sources", {}),
                "cycle_count":   h.get("cycle_count"),
                "uptime_seconds":h.get("uptime_seconds"),
                "session_pnl":   await _session_pnl_from_db(db),
            }
        except Exception:
            pass
        break  # only first intel entry

    # ── Per-node heartbeats from ep:system ───────────────────────────────────
    heartbeats = _node_heartbeats_from_stream(sys_entries)
    nodes: dict[str, Any] = {}
    for prefix, ts_s in heartbeats.items():
        age_s = round(now - ts_s, 1)
        nodes[prefix] = {
            "last_heartbeat_at": datetime.fromtimestamp(ts_s, tz=timezone.utc).isoformat(),
            "age_s": age_s,
            "alive": age_s < 180,   # stale if > 3 min (heartbeat cadence = 60s)
        }
    status["nodes"] = nodes

    # ── Balance — Kalshi and Coinbase kept separate ──────────────────────────
    kalshi_cents: int           = 0
    coinbase_cents: int         = 0
    mode: Optional[str]         = None
    last_balance_at: Optional[str] = None
    for k, raw in raw_balance.items():
        key = k.decode() if isinstance(k, bytes) else k
        try:
            b = json.loads(raw)
            amt = b.get("balance_cents", 0)
            if key == "coinbase":
                coinbase_cents += amt
            else:
                kalshi_cents += amt   # intel node = Kalshi available cash
                if not mode:           mode            = b.get("mode")
                if not last_balance_at: last_balance_at = _ts_us_to_iso(b.get("ts_us"))
        except Exception:
            pass

    status["balance_cents"]         = kalshi_cents      # Kalshi available cash only
    status["coinbase_balance_cents"] = coinbase_cents   # Coinbase separately
    status["mode"]                  = mode
    status["last_balance_at"]       = last_balance_at

    # ── Halt state ───────────────────────────────────────────────────────────
    try:
        halt_val = await r.hget(_BOT_CFG_KEY, "HALT_TRADING")
        status["halt_active"] = halt_val in ("1", "true", "True")
    except Exception:
        status["halt_active"] = False

    return status


@router.get("/activity", summary="Recent system events from ep:system Redis stream")
@limiter.limit("60/minute")
async def get_activity(
    request: Request,
    limit: int = Query(20, ge=5, le=100),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    Returns the last N events from the ep:system Redis stream (newest first).
    Used by the DashboardPage live activity feed.
    """
    r = get_redis()
    try:
        entries = await r.xrevrange("ep:system", count=limit)
    except Exception:
        entries = []

    events = []
    for entry_id, fields in entries:
        ts_iso = _ts_us_to_iso(fields.get("ts_us")) if fields.get("ts_us") else None
        # System events are stored as {"payload": json.dumps({event_type, node, detail, ts_us})}
        raw = fields.get("payload") or fields.get(b"payload")
        event_type = node = detail = ""
        if raw:
            try:
                ev = json.loads(raw)
                event_type = ev.get("event_type", "")
                node       = ev.get("node", "")
                detail     = ev.get("detail", "")
                if not ts_iso:
                    ts_iso = _ts_us_to_iso(ev.get("ts_us"))
            except Exception:
                pass
        events.append({
            "id":         str(entry_id),
            "event_type": event_type,
            "node":       node,
            "detail":     detail,
            "ts":         ts_iso,
        })
    return {"events": events}


@router.post("/halt", summary="Emergency: halt all new trading immediately")
@limiter.limit("30/minute")
async def halt_trading(
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    r = get_redis()
    try:
        await r.hset(_BOT_CFG_KEY, "HALT_TRADING", "1")
        await r.set("ep:tombstone:HALT_FLAG", "1", ex=86400)  # visible sentinel
        logger.warning("HALT_TRADING activated via dashboard")
        return {"ok": True, "halt_active": True}
    except Exception as exc:
        logger.error("halt_trading error: %s", exc)
        return {"ok": False, "halt_active": None, "error": str(exc)}


@router.post("/resume", summary="Resume trading after halt")
@limiter.limit("30/minute")
async def resume_trading(
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    r = get_redis()
    try:
        await r.hdel(_BOT_CFG_KEY, "HALT_TRADING")
        await r.delete("ep:tombstone:HALT_FLAG")
        logger.info("HALT_TRADING cleared via dashboard")
        return {"ok": True, "halt_active": False}
    except Exception as exc:
        return {"ok": False, "halt_active": None, "error": str(exc)}


@router.get("/halt-status", summary="Check whether HALT_TRADING is active")
@limiter.limit("60/minute")
async def get_halt_status(
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    r = get_redis()
    try:
        val = await r.hget(_BOT_CFG_KEY, "HALT_TRADING")
        halt_active = val in ("1", "true", "True")
    except Exception:
        halt_active = False
    return {"halt_active": halt_active}


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
