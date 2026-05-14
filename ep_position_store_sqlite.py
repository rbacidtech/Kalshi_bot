"""SQLite WAL position store — Engineering S.2 durability backup.

Engineering S.2 §2: "SQLite is the right persistent store: durability by
default (WAL mode), ACID transactions for multi-leg arbs, no operational
overhead vs Redis." Current bot uses Redis as primary; this module adds a
WAL-backed SQLite snapshot as durability backup so the bot can recover
position state after a Redis outage.

Architecture:
  - SQLite database at /var/lib/edgepulse/positions.sqlite (WAL mode)
  - `positions` table: one row per (ticker), columns mirror ep:positions
    Redis hash payload schema
  - `reconciliations` audit table: one row per drift-detection cycle
    tracking source (boot / periodic / post-fill), divergences, resolutions
  - Periodic snapshot from Redis → SQLite every 5 min (write-through pattern
    in MVP; later phases can flip to SQLite-primary with Redis cache)
  - Boot recovery: if Redis ep:positions is empty/missing AND SQLite has
    rows, write a flag file `/root/EdgePulse/.recovery_pending` so operator
    must confirm before trading resumes (Engineering S.2 §3 mandate:
    "drift at boot warrants investigation").

This module is read-only with respect to live trading — it doesn't write
to Redis. The hot path (entry/exit) continues to write Redis only;
SQLite is a passive backup updated by `snapshot_from_redis()` invoked
from the periodic loop.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_DB_PATH = Path(os.environ.get("EP_POSITIONS_SQLITE", "/var/lib/edgepulse/positions.sqlite"))
_RECOVERY_FLAG = Path(os.environ.get("EP_RECOVERY_FLAG", "/root/EdgePulse/.recovery_pending"))


def _ensure_parent_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    _ensure_parent_dir()
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0)
    # Engineering S.2: WAL mode for durability + concurrent reads.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL+NORMAL is durable across crashes
    return conn


def ensure_schema() -> None:
    """Create tables + indexes if missing. Safe to call at every boot."""
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS positions (
                ticker            TEXT PRIMARY KEY,
                payload_json      TEXT NOT NULL,
                snapshotted_at_us INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_positions_snapshotted
                ON positions (snapshotted_at_us DESC);

            CREATE TABLE IF NOT EXISTS reconciliations (
                rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_us           INTEGER NOT NULL,
                source          TEXT NOT NULL,   -- 'boot' / 'periodic' / 'post_fill'
                redis_count     INTEGER NOT NULL,
                sqlite_count    INTEGER NOT NULL,
                kalshi_count    INTEGER,         -- nullable; not always available
                divergences     TEXT,            -- JSON-encoded list of {ticker, side, where}
                resolution      TEXT,            -- 'auto_reconciled' / 'flag_for_operator' / 'no_action'
                notes           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reconciliations_ts
                ON reconciliations (ts_us DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def snapshot_from_redis(redis_positions: dict[str, str]) -> int:
    """Upsert `redis_positions` (ticker → JSON payload string) into SQLite.

    Drops rows whose ticker is no longer in `redis_positions` (positions
    that closed since last snapshot). Returns number of rows in SQLite
    after the operation.
    """
    if redis_positions is None:
        return 0
    now_us = int(time.time() * 1_000_000)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        # Upsert all current Redis positions
        for ticker, payload in redis_positions.items():
            if isinstance(payload, bytes):
                payload = payload.decode()
            cur.execute(
                """
                INSERT INTO positions (ticker, payload_json, snapshotted_at_us)
                VALUES (?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  snapshotted_at_us = excluded.snapshotted_at_us
                """,
                (str(ticker), str(payload), now_us),
            )
        # Drop positions no longer in Redis
        if redis_positions:
            placeholders = ",".join("?" for _ in redis_positions)
            cur.execute(
                f"DELETE FROM positions WHERE ticker NOT IN ({placeholders})",
                tuple(str(t) for t in redis_positions.keys()),
            )
        else:
            cur.execute("DELETE FROM positions")
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM positions")
        count = int(cur.fetchone()[0])
    finally:
        conn.close()
    return count


def read_all_positions() -> dict[str, dict]:
    """Return {ticker: parsed payload dict} for every row in SQLite."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ticker, payload_json FROM positions")
        rows = cur.fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for ticker, payload_json in rows:
        try:
            out[ticker] = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
    return out


def record_reconciliation(
    source: str,
    redis_count: int,
    sqlite_count: int,
    kalshi_count: Optional[int] = None,
    divergences: Optional[list] = None,
    resolution: str = "no_action",
    notes: str = "",
) -> int:
    """Append a reconciliation audit row. Returns the new rowid."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reconciliations
              (ts_us, source, redis_count, sqlite_count, kalshi_count,
               divergences, resolution, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1_000_000),
                source,
                int(redis_count),
                int(sqlite_count),
                int(kalshi_count) if kalshi_count is not None else None,
                json.dumps(divergences) if divergences else None,
                resolution,
                notes,
            ),
        )
        conn.commit()
        new_rowid = int(cur.lastrowid or 0)
    finally:
        conn.close()
    return new_rowid


def detect_drift(
    redis_positions: dict[str, Any],
    sqlite_positions: Optional[dict[str, Any]] = None,
) -> list[dict]:
    """Return list of {ticker, where} for divergences between Redis & SQLite.

    Each entry's `where` is 'redis_only' or 'sqlite_only'. If a ticker
    exists in both, no entry (payload equality is not enforced — Redis
    is authoritative for live state).
    """
    if sqlite_positions is None:
        sqlite_positions = read_all_positions()
    redis_set = set(redis_positions.keys())
    sqlite_set = set(sqlite_positions.keys())
    out: list[dict] = []
    for ticker in redis_set - sqlite_set:
        out.append({"ticker": ticker, "where": "redis_only"})
    for ticker in sqlite_set - redis_set:
        out.append({"ticker": ticker, "where": "sqlite_only"})
    return out


def boot_recovery_check(redis_positions: dict[str, Any]) -> Optional[dict]:
    """Run at service startup. If Redis is empty AND SQLite has rows,
    write the recovery flag file and return a recovery report; trader
    must defensive-halt until operator clears the flag.

    Returns None when no recovery is needed (Redis has positions OR
    SQLite is also empty).
    """
    if redis_positions:
        return None
    sqlite_positions = read_all_positions()
    if not sqlite_positions:
        return None
    # Redis is empty but SQLite has state — anomaly. Engineering S.2 §3:
    # "drift at boot warrants investigation. Boot reconciliation must
    # complete with drift=0 or require explicit operator confirmation
    # (flag file) before trading resumes."
    try:
        _RECOVERY_FLAG.write_text(
            f"detected_at_us={int(time.time() * 1_000_000)}\n"
            f"redis_count=0\n"
            f"sqlite_count={len(sqlite_positions)}\n"
            f"reason=redis_empty_sqlite_has_rows\n"
            f"action=operator_must_review_then_rm_flag\n"
        )
    except Exception as exc:
        log.error("boot_recovery_check: failed to write flag file: %s", exc)
    record_reconciliation(
        source="boot",
        redis_count=0,
        sqlite_count=len(sqlite_positions),
        divergences=[{"ticker": t, "where": "sqlite_only"} for t in sqlite_positions],
        resolution="flag_for_operator",
        notes=f"Redis empty + {len(sqlite_positions)} SQLite rows — recovery flag written",
    )
    return {
        "recovery_needed":   True,
        "redis_count":       0,
        "sqlite_count":      len(sqlite_positions),
        "flag_path":         str(_RECOVERY_FLAG),
        "sqlite_positions":  list(sqlite_positions.keys()),
    }


def is_recovery_pending() -> bool:
    """True if the boot-recovery flag file exists. Trader checks this
    in its main loop and defensive-halts if set."""
    try:
        return _RECOVERY_FLAG.exists()
    except Exception:
        return False
