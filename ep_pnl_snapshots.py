"""
Lightweight asyncpg module for writing P&L snapshots to Postgres.
Called from ep_intel._heartbeat_loop; read by the FastAPI performance router.
"""
from __future__ import annotations

import asyncpg
import logging
import os

log = logging.getLogger(__name__)

_DB_URL = os.getenv("DATABASE_URL", "")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id                   BIGSERIAL PRIMARY KEY,
    ts                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    balance_cents        BIGINT,
    deployed_cents       BIGINT,
    unrealized_pnl_cents BIGINT,
    realized_pnl_cents   BIGINT,
    position_count       INT,
    source               TEXT NOT NULL DEFAULT 'intel'
);
CREATE INDEX IF NOT EXISTS ix_pnl_snapshots_ts ON pnl_snapshots (ts DESC);
"""

_INSERT_SQL = """
INSERT INTO pnl_snapshots
    (balance_cents, deployed_cents, unrealized_pnl_cents,
     realized_pnl_cents, position_count, source)
VALUES ($1, $2, $3, $4, $5, $6)
"""


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def ensure_table() -> None:
    try:
        conn = await asyncpg.connect(_dsn(_DB_URL))
        await conn.execute(_CREATE_SQL)
        await conn.close()
        log.info("pnl_snapshots: table ready")
    except Exception as exc:
        log.warning("pnl_snapshots: ensure_table failed: %s", exc)


async def write_snapshot(
    *,
    balance_cents: int | None,
    deployed_cents: int | None,
    unrealized_pnl_cents: int | None,
    realized_pnl_cents: int | None,
    position_count: int | None,
    source: str = "intel",
) -> None:
    try:
        conn = await asyncpg.connect(_dsn(_DB_URL))
        await conn.execute(
            _INSERT_SQL,
            balance_cents,
            deployed_cents,
            unrealized_pnl_cents,
            realized_pnl_cents,
            position_count,
            source,
        )
        await conn.close()
    except Exception as exc:
        log.debug("pnl_snapshots: write failed: %s", exc)
