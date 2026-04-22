"""
ep_pg_audit.py — Non-blocking async Postgres audit writer.

Fire-and-forget writes via an in-memory asyncio.Queue; a background task
drains it every 2 s in batches of 100 rows.  A full queue drops the oldest
entry rather than blocking or raising — Postgres outages never stall Exec.
"""

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

log = logging.getLogger("edgepulse.pg_audit")

_BATCH_SIZE   = 100
_FLUSH_SECS   = 2.0
_QUEUE_MAXLEN = 10_000

_writer: Optional["PgAuditWriter"] = None


class PgAuditWriter:

    def __init__(self, dsn: str) -> None:
        self._dsn   = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._queue: asyncio.Queue         = asyncio.Queue(maxsize=_QUEUE_MAXLEN)
        self._task:  Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=4,
            command_timeout=10,
        )
        self._task = asyncio.create_task(self._drain_loop(), name="pg_audit_drain")
        log.info("PgAuditWriter started (dsn=redacted)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush_all()
        if self._pool:
            await self._pool.close()
        log.info("PgAuditWriter stopped")

    def write(self, table: str, payload: Dict[str, Any]) -> None:
        """Enqueue a row.  Drops the oldest entry if the queue is full."""
        item = (table, payload)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                log.debug("audit queue full — dropping %s row", table)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(_FLUSH_SECS)
            await self._flush_all()

    async def _flush_all(self) -> None:
        if self._queue.empty():
            return
        batch: List[tuple] = []
        for _ in range(_BATCH_SIZE):
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        by_table: Dict[str, List[Dict]] = {}
        for table, payload in batch:
            by_table.setdefault(table, []).append(payload)
        for table, rows in by_table.items():
            try:
                await self._insert_batch(table, rows)
            except Exception as exc:
                log.warning("pg_audit insert failed (%s, %d rows): %s", table, len(rows), exc)

    async def _insert_batch(self, table: str, rows: List[Dict]) -> None:
        if not self._pool:
            return
        if table == "signals":
            await self._insert_signals(rows)
        elif table == "executions":
            await self._insert_executions(rows)
        elif table == "balance_snapshots":
            await self._insert_balance_snapshots(rows)
        elif table == "llm_decisions":
            await self._insert_llm_decisions(rows)
        else:
            log.debug("pg_audit: unknown table %r — dropping %d rows", table, len(rows))

    async def _insert_signals(self, rows: List[Dict]) -> None:
        records = []
        for r in rows:
            ts_us = r.get("ts_us") or 0
            records.append((
                r.get("signal_id"),
                datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc),
                r.get("strategy", ""),
                r.get("ticker", ""),
                r.get("side", ""),
                r.get("asset_class", ""),
                r.get("market_price"),
                r.get("fair_value"),
                r.get("edge"),
                r.get("confidence"),
                r.get("suggested_size"),
                json.dumps(r),
            ))
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO signals
                    (signal_id, emitted_at, strategy, ticker, side, asset_class,
                     market_price, fair_value, edge, confidence, suggested_size, payload)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (signal_id) DO NOTHING
                """,
                records,
            )

    async def _insert_executions(self, rows: List[Dict]) -> None:
        records = []
        for r in rows:
            ts_us = r.get("ts_us") or 0
            records.append((
                r.get("exec_id"),
                r.get("signal_id") or None,
                datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc),
                r.get("status", "unknown"),
                r.get("reject_reason"),
                r.get("ticker"),
                r.get("side"),
                r.get("asset_class"),
                r.get("contracts"),
                r.get("fill_price"),
                r.get("fee_cents"),
                r.get("cost_cents"),
                r.get("edge_captured"),
                r.get("order_id"),
                r.get("mode"),
                json.dumps(r),
            ))
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO executions
                    (exec_id, signal_id, reported_at, status, reject_reason,
                     ticker, side, asset_class, contracts, fill_price,
                     fee_cents, cost_cents, edge_captured, order_id, mode, payload)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                ON CONFLICT (exec_id) DO NOTHING
                """,
                records,
            )

    async def _insert_balance_snapshots(self, rows: List[Dict]) -> None:
        records = []
        for r in rows:
            ts_us = r.get("ts_us") or 0
            records.append((
                datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc),
                r.get("asset_class", ""),
                r.get("balance_cents", 0),
                r.get("open_pos_count", 0),
                r.get("exposure_cents", 0),
                r.get("daily_pnl_cents"),
            ))
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO balance_snapshots
                    (taken_at, asset_class, balance_cents, open_pos_count,
                     exposure_cents, daily_pnl_cents)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                records,
            )

    async def _insert_llm_decisions(self, rows: List[Dict]) -> None:
        records = []
        for r in rows:
            ts_us = r.get("ts_us") or 0
            records.append((
                datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc),
                r.get("model", ""),
                r.get("prompt_tokens"),
                r.get("completion_tokens"),
                json.dumps(r.get("config_before", {})),
                json.dumps(r.get("config_after", {})),
                r.get("reasoning", ""),
            ))
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO llm_decisions
                    (decided_at, model, prompt_tokens, completion_tokens,
                     config_before, config_after, reasoning)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                records,
            )


# ── Singleton lifecycle ───────────────────────────────────────────────────────

async def init_audit_writer() -> None:
    global _writer
    dsn = os.environ.get("DATABASE_URL", "")
    # asyncpg requires postgresql:// not the SQLAlchemy postgresql+asyncpg:// prefix
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        log.warning("DATABASE_URL not set — audit writes disabled")
        return
    _writer = PgAuditWriter(dsn)
    await _writer.start()


async def stop_audit_writer() -> None:
    global _writer
    if _writer is not None:
        await _writer.stop()
        _writer = None


def audit() -> PgAuditWriter:
    if _writer is None:
        raise RuntimeError("audit writer not initialised — call init_audit_writer() first")
    return _writer
