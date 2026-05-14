"""Severity-tiered alerting with cooldowns — Engineering B.1.

5-tier alerting (Engineering B.1 §):
  INFO     — operational; daily digest only, no notifications
  NOTICE   — informational; daily digest + push if 3+ per hour
  WARNING  — actionable but not urgent; push notification, 30m cooldown
  ERROR    — bot may be degraded; push, 10m cooldown
  CRITICAL — bot may be losing money or unable to trade; push, no cooldown

The bot already has ep_telegram.py for push delivery. This module adds:
  - Severity classification + threshold table
  - Per-alert-type cooldown (Redis-backed timestamp ZSET)
  - SQLite metrics persistence at /var/lib/edgepulse/alerts.sqlite

Engineering B.1 §13 calls for alert rate limiting: "3 alerts of same type
within 30 min → coalesce + suppress." Defaults below; configurable via
ep:config:override_alert_*.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class Severity(IntEnum):
    INFO = 1
    NOTICE = 2
    WARNING = 3
    ERROR = 4
    CRITICAL = 5


# Per-severity cooldown (seconds). Engineering B.1 §3 defaults.
_COOLDOWN_S: dict[int, int] = {
    Severity.INFO:     0,        # never push; daily digest only
    Severity.NOTICE:   3600,     # 1h
    Severity.WARNING:  1800,     # 30m
    Severity.ERROR:    600,      # 10m
    Severity.CRITICAL: 0,        # no cooldown — always push
}

_REDIS_LAST_TS_HASH = "ep:alerts:last_ts_us"
_SQLITE_PATH = Path(os.environ.get("EP_ALERTS_SQLITE", "/var/lib/edgepulse/alerts.sqlite"))


def _ensure_db() -> sqlite3.Connection:
    _SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema() -> None:
    """Create tables + indexes if missing."""
    conn = _ensure_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_us       INTEGER NOT NULL,
                severity    INTEGER NOT NULL,
                alert_type  TEXT NOT NULL,
                message     TEXT NOT NULL,
                metadata    TEXT,
                delivered   INTEGER NOT NULL DEFAULT 0,
                suppressed  INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_ts
                ON alerts (ts_us DESC);
            CREATE INDEX IF NOT EXISTS idx_alerts_type
                ON alerts (alert_type, ts_us DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


async def _check_cooldown(bus_redis: Any, alert_type: str, cooldown_s: int) -> bool:
    """Return True if we should suppress (within cooldown window)."""
    if cooldown_s <= 0:
        return False
    if bus_redis is None:
        return False
    try:
        raw = await bus_redis.hget(_REDIS_LAST_TS_HASH, alert_type)
        if raw is None:
            return False
        last_us = int(raw.decode() if isinstance(raw, bytes) else raw)
        age_s = (time.time() * 1_000_000 - last_us) / 1_000_000
        return age_s < cooldown_s
    except Exception:
        return False


async def _stamp_cooldown(bus_redis: Any, alert_type: str) -> None:
    if bus_redis is None:
        return
    try:
        await bus_redis.hset(_REDIS_LAST_TS_HASH, alert_type, str(int(time.time() * 1_000_000)))
    except Exception:
        pass


def _persist_alert(
    severity: int,
    alert_type: str,
    message: str,
    metadata: Optional[str],
    delivered: bool,
    suppressed: bool,
) -> int:
    conn = _ensure_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO alerts
              (ts_us, severity, alert_type, message, metadata, delivered, suppressed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1_000_000),
                int(severity),
                str(alert_type),
                str(message),
                metadata,
                1 if delivered else 0,
                1 if suppressed else 0,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


async def emit(
    bus_redis: Any,
    severity: Severity,
    alert_type: str,
    message: str,
    metadata: Optional[dict] = None,
    push_fn: Optional[Any] = None,
) -> dict[str, Any]:
    """Emit an alert. Returns {persisted, delivered, suppressed, alert_id}.

    `push_fn` is an optional async callable for push delivery (e.g.
    ep_telegram.send_alert). When None, the alert is only persisted to
    SQLite + cooldown stamp.

    INFO severity NEVER pushes (daily digest only).
    CRITICAL ALWAYS pushes (no cooldown).
    Others push if cooldown not active.
    """
    if not isinstance(severity, Severity):
        severity = Severity(int(severity))

    cooldown_s = _COOLDOWN_S.get(severity, 0)
    suppressed = await _check_cooldown(bus_redis, alert_type, cooldown_s)

    # INFO is daily-digest only — never push, never cooldown-stamp
    should_push = (severity >= Severity.NOTICE) and (not suppressed) and (push_fn is not None)

    delivered = False
    if should_push:
        try:
            res = push_fn(message)
            if hasattr(res, "__await__"):
                res = await res
            delivered = bool(res) if res is not None else True
        except Exception as exc:
            log.warning("alert push failed (%s/%s): %s", severity.name, alert_type, exc)

    import json as _json
    meta_json = _json.dumps(metadata) if metadata else None
    try:
        alert_id = _persist_alert(int(severity), alert_type, message, meta_json, delivered, suppressed)
    except Exception as exc:
        log.warning("alert SQLite persist failed: %s", exc)
        alert_id = -1

    if delivered:
        await _stamp_cooldown(bus_redis, alert_type)

    return {
        "alert_id":   alert_id,
        "delivered":  delivered,
        "suppressed": suppressed,
        "severity":   severity.name,
    }


def daily_digest(since_unix: float) -> list[dict]:
    """Return all alerts since `since_unix` for the daily digest builder."""
    since_us = int(since_unix * 1_000_000)
    conn = _ensure_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT rowid, ts_us, severity, alert_type, message, metadata,
                   delivered, suppressed
            FROM alerts
            WHERE ts_us >= ?
            ORDER BY ts_us DESC
            """,
            (since_us,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for r in rows:
        out.append({
            "rowid":      int(r[0]),
            "ts_us":      int(r[1]),
            "severity":   Severity(int(r[2])).name,
            "alert_type": r[3],
            "message":    r[4],
            "metadata":   r[5],
            "delivered":  bool(r[6]),
            "suppressed": bool(r[7]),
        })
    return out
