"""
api/routers/ws.py — Real-time WebSocket push channel.

Clients connect at  ws(s)://host/ws?token=<access_jwt>

The server polls Redis every 3 s and pushes deltas only when data changes:

  {"type": "portfolio",  "data": {...}}   — PortfolioResponse shape
  {"type": "status",     "data": {...}}   — BotStatus shape
  {"type": "activity",   "data": [...]}   — new ep:system events (delta only)
  {"type": "ping",       "ts": float}     — keepalive every 10 s
  {"type": "error",      "detail": "..."}

Authentication: JWT passed as ?token= query param (WS can't send HTTP headers).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select, text

from api.database import AsyncSessionLocal
from api.models import User
from api.redis_client import get_redis
from api.security import decode_access_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])

_POLL_S = 3.0    # how often to poll Redis for deltas
_PING_S = 10.0   # keepalive ping interval


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _ws_auth(ws: WebSocket, token: str) -> Optional[User]:
    """Verify JWT, load User from DB. Returns None (and closes WS) on failure."""
    try:
        from fastapi import HTTPException
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("missing sub")
    except Exception:
        await ws.close(code=4001, reason="Invalid token")
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user: Optional[User] = result.scalar_one_or_none()

    if user is None or not user.is_active:
        await ws.close(code=4003, reason="Unauthorized")
        return None
    return user


# ── Helpers ───────────────────────────────────────────────────────────────────

def _djson(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _hash(data: Any) -> str:
    return hashlib.md5(_djson(data).encode()).hexdigest()


def _ts_us_to_iso(ts_us: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc).isoformat()
    except Exception:
        return None


# ── Portfolio snapshot ────────────────────────────────────────────────────────

async def _portfolio(r) -> dict[str, Any]:
    try:
        raw_pos, raw_prices, raw_bal = await asyncio.gather(
            r.hgetall("ep:positions"),
            r.hgetall("ep:prices"),
            r.hgetall("ep:balance"),
            return_exceptions=True,
        )
    except Exception:
        return {}
    if isinstance(raw_pos,    Exception): raw_pos    = {}
    if isinstance(raw_prices, Exception): raw_prices = {}
    if isinstance(raw_bal,    Exception): raw_bal    = {}

    prices: dict[str, Any] = {}
    for k, v in raw_prices.items():
        try: prices[k] = json.loads(v)
        except Exception: pass

    positions: list[dict] = []
    total_deployed   = 0
    total_unrealized = 0
    missing          = 0

    for ticker, raw_val in raw_pos.items():
        try: p = json.loads(raw_val)
        except Exception: continue
        contracts = int(p.get("contracts", 0))
        if contracts == 0:
            continue
        side  = p.get("side", "yes")
        entry = int(p.get("entry_cents", 0))
        cost  = ((100 - entry) * contracts) if side == "no" else (entry * contracts)
        total_deployed += cost

        price_data = prices.get(ticker, {})
        cur_yes = price_data.get("yes_price")
        if cur_yes is not None:
            cur_yes = int(cur_yes)
            pnl = (cur_yes - entry) * contracts if side == "yes" else (entry - cur_yes) * contracts
            total_unrealized += pnl
        else:
            pnl = None
            missing += 1

        raw_conf = p.get("confidence")
        positions.append({
            "ticker":               ticker,
            "side":                 side,
            "contracts":            contracts,
            "entry_cents":          entry,
            "fair_value":           p.get("fair_value"),
            "fill_confirmed":       bool(p.get("fill_confirmed", False)),
            "entered_at":           p.get("entered_at"),
            "close_time":           p.get("close_time"),
            "unrealized_pnl_cents": pnl,
            "model_source":         p.get("model_source"),
            "confidence":           float(raw_conf) if raw_conf is not None else None,
            "outcome":              p.get("outcome"),
            "meeting":              p.get("meeting"),
        })

    balance_cents: Optional[int] = None
    portfolio_value: Optional[int] = None
    for k, v in raw_bal.items():
        key = k.decode() if isinstance(k, bytes) else k
        if "intel" in key.lower() or "kalshi" in key.lower():
            try:
                b = json.loads(v)
                balance_cents = int(b.get("balance_cents", 0))
                pv = b.get("portfolio_value_cents")
                if pv is not None:
                    portfolio_value = int(pv)
                break
            except Exception:
                pass

    if balance_cents is not None:
        total_value = (
            balance_cents + portfolio_value
            if portfolio_value is not None
            else balance_cents + total_deployed + total_unrealized
        )
    else:
        total_value = None

    return {
        "positions":                  sorted(positions, key=lambda x: x["ticker"]),
        "total_deployed_cents":       total_deployed,
        "total_unrealized_pnl_cents": total_unrealized,
        "balance_cents":              balance_cents,
        "total_value_cents":          total_value,
        "position_count":             len(positions),
        "positions_without_price":    missing,
    }


# ── Status snapshot ───────────────────────────────────────────────────────────

async def _status(r) -> dict[str, Any]:
    try:
        raw_health, raw_bal, sys_entries, halt_val = await asyncio.gather(
            r.hgetall("ep:health"),
            r.hgetall("ep:balance"),
            r.xrevrange("ep:system", count=200),
            r.hget("ep:config", "HALT_TRADING"),
            return_exceptions=True,
        )
    except Exception:
        return {}
    if isinstance(raw_health,  Exception): raw_health  = {}
    if isinstance(raw_bal,     Exception): raw_bal     = {}
    if isinstance(sys_entries, Exception): sys_entries = []
    if isinstance(halt_val,    Exception): halt_val    = None

    status: dict[str, Any] = {
        "halt_active": halt_val in ("1", "true", "True"),
    }
    now = datetime.now(timezone.utc).timestamp()
    business_issues: list[str] = []
    intel_data: dict[str, Any] = {}
    exec_health: Optional[str] = None

    for node_id_raw, raw in raw_health.items():
        node_id = node_id_raw.decode() if isinstance(node_id_raw, bytes) else node_id_raw
        try:
            h = json.loads(raw)
        except Exception:
            continue
        if node_id.startswith("intel"):
            ws_src = h.get("sources", {}).get("kalshi_ws", {})
            intel_data = {
                "node_id":        node_id,
                "health":         h.get("overall", "unknown"),
                "ws_connected":   ws_src.get("status") == "ok",
                "last_cycle_at":  _ts_us_to_iso(h.get("ts_us")),
                "sources":        h.get("sources", {}),
                "cycle_count":    h.get("cycle_count"),
                "uptime_seconds": h.get("uptime_seconds"),
            }
        elif node_id.startswith("exec"):
            exec_health = h.get("overall", "unknown")
            biz = h.get("sources", {}).get("business", {}).get("error", "")
            if biz:
                business_issues = [s.strip() for s in biz.split(";") if s.strip()]

    status.update(intel_data)
    status["exec_health"]     = exec_health
    status["business_issues"] = business_issues
    if business_issues and status.get("health") not in ("critical",):
        status["health"] = "degraded"

    # Per-node heartbeats from ep:system stream
    seen: set[str] = set()
    nodes: dict[str, Any] = {}
    for _eid, fields in sys_entries:
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
        prefix = "intel" if node.startswith("intel") else "exec" if node.startswith("exec") else node
        if prefix in seen:
            continue
        seen.add(prefix)
        ts_s = float(ev.get("ts_us", 0)) / 1_000_000
        if ts_s:
            age_s = round(now - ts_s, 1)
            nodes[prefix] = {
                "last_heartbeat_at": datetime.fromtimestamp(ts_s, tz=timezone.utc).isoformat(),
                "age_s":  age_s,
                "alive":  age_s < 180,
            }
        if len(seen) >= 2:
            break
    status["nodes"] = nodes

    # Balance
    for k, raw in raw_bal.items():
        key = k.decode() if isinstance(k, bytes) else k
        if "intel" in key.lower() or "kalshi" in key.lower():
            try:
                b = json.loads(raw)
                status["balance_cents"] = int(b.get("balance_cents", 0))
                status["mode"] = b.get("mode")
                break
            except Exception:
                pass

    return status


# ── Activity stream delta ─────────────────────────────────────────────────────

async def _new_activity(r, last_id: str) -> tuple[list[dict], str]:
    """Return new ep:system events since last_id, and the updated last_id."""
    try:
        results = await r.xread({"ep:system": last_id}, count=20)
    except Exception:
        return [], last_id

    events: list[dict] = []
    new_last = last_id
    for _stream, entries in results:
        for entry_id, fields in entries:
            new_last = entry_id
            raw = fields.get("payload") or fields.get(b"payload")
            event_type = node = detail = ""
            ts_iso: Optional[str] = None
            if raw:
                try:
                    ev = json.loads(raw)
                    event_type = ev.get("event_type", "")
                    node       = ev.get("node", "")
                    detail     = ev.get("detail", "")
                    ts_iso     = _ts_us_to_iso(ev.get("ts_us"))
                except Exception:
                    pass
            if not ts_iso:
                ts_iso = _ts_us_to_iso(fields.get("ts_us"))
            events.append({
                "id":         str(entry_id),
                "event_type": event_type,
                "node":       node,
                "detail":     detail,
                "ts":         ts_iso,
            })
    return events, new_last


# ── Session P&L (DB, cached) ──────────────────────────────────────────────────

async def _session_pnl() -> Optional[int]:
    try:
        today_utc = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with AsyncSessionLocal() as db:
            result = await db.execute(text("""
                SELECT balance_cents, deployed_cents, unrealized_pnl_cents
                FROM pnl_snapshots WHERE ts >= :today ORDER BY ts ASC LIMIT 1
            """), {"today": today_utc})
            start_row = result.fetchone()
            if start_row is None:
                result = await db.execute(text("""
                    SELECT balance_cents, deployed_cents, unrealized_pnl_cents
                    FROM pnl_snapshots WHERE ts < :today ORDER BY ts DESC LIMIT 1
                """), {"today": today_utc})
                start_row = result.fetchone()
            if start_row is None:
                return None
            result2 = await db.execute(text("""
                SELECT balance_cents, deployed_cents, unrealized_pnl_cents
                FROM pnl_snapshots ORDER BY ts DESC LIMIT 1
            """))
            latest = result2.fetchone()
        if not latest:
            return None
        def _t(row) -> int:
            return (row[0] or 0) + (row[1] or 0) + (row[2] or 0)
        return _t(latest) - _t(start_row)
    except Exception:
        return None


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    token: str = Query(..., description="Access JWT from localStorage"),
) -> None:
    await ws.accept()

    user = await _ws_auth(ws, token)
    if user is None:
        return

    logger.info("WS connected: %s", user.email)
    r = get_redis()

    # Anchor stream position to current tail — we only push *new* events
    try:
        tail = await r.xrevrange("ep:system", count=1)
        last_stream_id: str = tail[0][0] if tail else "0"
    except Exception:
        last_stream_id = "0"

    portfolio_hash = ""
    status_hash    = ""
    last_ping      = time.monotonic()
    last_pnl_fetch = 0.0   # timestamp of last DB session_pnl fetch
    cached_pnl: Optional[int] = None

    async def _send(msg_type: str, data: Any) -> None:
        await ws.send_text(json.dumps({"type": msg_type, "data": data}, default=str))

    try:
        # ── Initial snapshot ─────────────────────────────────────────────────
        port, stat = await asyncio.gather(_portfolio(r), _status(r))
        cached_pnl = await _session_pnl()
        last_pnl_fetch = time.monotonic()
        stat["session_pnl"] = cached_pnl

        if port: await _send("portfolio", port)
        if stat: await _send("status",    stat)
        portfolio_hash = _hash(port)
        status_hash    = _hash(stat)

        # ── Poll loop ────────────────────────────────────────────────────────
        while True:
            await asyncio.sleep(_POLL_S)
            now_m = time.monotonic()

            # Keepalive ping
            if now_m - last_ping >= _PING_S:
                await ws.send_text(json.dumps({"type": "ping", "ts": time.time()}))
                last_ping = now_m

            # Refresh session P&L from DB every 30 s
            if now_m - last_pnl_fetch >= 30:
                cached_pnl = await _session_pnl()
                last_pnl_fetch = now_m

            # Portfolio delta
            try:
                port = await _portfolio(r)
                h = _hash(port)
                if h != portfolio_hash:
                    await _send("portfolio", port)
                    portfolio_hash = h
            except Exception as exc:
                logger.debug("WS portfolio error: %s", exc)

            # Status delta (inject cached session_pnl to avoid per-cycle DB hit)
            try:
                stat = await _status(r)
                stat["session_pnl"] = cached_pnl
                h = _hash(stat)
                if h != status_hash:
                    await _send("status", stat)
                    status_hash = h
            except Exception as exc:
                logger.debug("WS status error: %s", exc)

            # Activity stream delta
            try:
                events, last_stream_id = await _new_activity(r, last_stream_id)
                if events:
                    await _send("activity", events)
            except Exception as exc:
                logger.debug("WS activity error: %s", exc)

    except WebSocketDisconnect:
        logger.info("WS disconnected: %s", user.email)
    except Exception as exc:
        logger.warning("WS error (user=%s): %s", user.email, exc)
        try:
            await ws.close(code=1011, reason="Server error")
        except Exception:
            pass
