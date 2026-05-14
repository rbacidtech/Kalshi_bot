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
_MAX_RETRIES  = 3

# Required fields per table — derived from the Postgres schema's NOT NULL
# columns minus those auto-supplied by _insert_*() (e.g. ts_us → emitted_at,
# server defaults). Validated at enqueue time so bad payloads fail loudly at
# the call site rather than poisoning a 100-row batch.
_REQUIRED_FIELDS: Dict[str, set] = {
    "signals":           {"signal_id", "ts_us", "strategy", "ticker", "side", "asset_class"},
    "executions":        {"exec_id", "ts_us", "status"},
    "balance_snapshots": {"ts_us", "asset_class", "balance_cents", "open_pos_count", "exposure_cents"},
    "llm_decisions":     {"ts_us", "model"},
    "position_history":  {"ticker", "side", "contracts", "entry_cents", "exit_cents",
                          "realized_pnl_cents"},
    "market_snapshots":  {"ts_us", "ticker"},
}

_writer: Optional["PgAuditWriter"] = None
_disabled_write_count: int = 0
_last_disabled_warn_ts: float = 0.0


class _NoopWriter:
    """Stand-in returned by audit() when the real writer is unavailable.
    Counts dropped writes; emits one WARN per minute (rate-limited)."""
    def write(self, table: str, payload: Dict[str, Any]) -> None:
        global _disabled_write_count, _last_disabled_warn_ts
        _disabled_write_count += 1
        import time as _t
        now = _t.time()
        if now - _last_disabled_warn_ts > 60.0:
            log.warning("pg_audit disabled — %d writes dropped (most recent: %s)",
                        _disabled_write_count, table)
            _last_disabled_warn_ts = now

    @property
    def _queue(self):
        # Heartbeat code in ep_intel.py:1142 and ep_exec.py:1282 reads
        # _audit_writer()._queue.qsize() — return a stub so it keeps working.
        class _Q:
            def qsize(self): return 0
        return _Q()


class PgAuditWriter:

    def __init__(self, dsn: str) -> None:
        self._dsn   = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._queue: asyncio.Queue         = asyncio.Queue(maxsize=_QUEUE_MAXLEN)
        self._task:  Optional[asyncio.Task] = None
        self._failed: List[tuple]          = []   # [(table, rows, retries_left)]

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
        """Enqueue a row. Drops the oldest entry if the queue is full."""
        # H4 schema validation BEFORE enqueue — see _REQUIRED_FIELDS map at top of module.
        required = _REQUIRED_FIELDS.get(table)
        if required:
            missing = required - set(payload.keys())
            if missing:
                log.error("pg_audit: %s payload missing required fields %s — dropped",
                          table, sorted(missing))
                try:
                    from ep_metrics import metrics
                    metrics.invariant_violations.labels(
                        invariant=f"audit_payload_invalid_{table}").inc()
                except Exception:
                    pass
                return

        item = (table, payload)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Eviction site — this is where the actual drop happens.
            try:
                dropped_table, _ = self._queue.get_nowait()
                try:
                    from ep_metrics import metrics
                    metrics.invariant_violations.labels(
                        invariant=f"audit_queue_overflow_{dropped_table}").inc()
                except Exception:
                    pass
                log.warning(
                    "audit queue overflow: queue full at %d, evicting oldest %s row to make room for %s",
                    _QUEUE_MAXLEN, dropped_table, table,
                )
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                log.error("audit queue full immediately after eviction — dropping %s row", table)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(_FLUSH_SECS)
            await self._flush_all()

    async def _flush_all(self) -> None:
        # Retry previously-failed batches FIRST (head-of-line for outage recovery).
        if self._failed:
            still_failed: List[tuple] = []
            for table, rows, retries in self._failed:
                try:
                    await self._insert_batch(table, rows)
                except Exception as exc:
                    if retries > 0:
                        still_failed.append((table, rows, retries - 1))
                        log.warning("pg_audit retry %d remaining for %s (%d rows): %s",
                                    retries - 1, table, len(rows), exc)
                    else:
                        log.error("pg_audit DROPPING %d %s rows after %d retries: %s",
                                  len(rows), table, _MAX_RETRIES, exc)
                        try:
                            from ep_metrics import metrics
                            metrics.invariant_violations.labels(
                                invariant=f"audit_dropped_{table}").inc(len(rows))
                        except Exception:
                            pass
            self._failed = still_failed

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
                log.warning("pg_audit insert failed (%s, %d rows) — queued for retry: %s",
                            table, len(rows), exc)
                self._failed.append((table, rows, _MAX_RETRIES))

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
        elif table == "position_history":
            await self._insert_position_history(rows)
        elif table == "market_snapshots":
            await self._insert_market_snapshots(rows)
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

    @staticmethod
    def _compute_slippage_cents(r: Dict) -> Optional[int]:
        """Phase 1.4 S.1.1 — side-aware adverse slippage in cents.

        Positive = adverse (paid more / received less than quoted at signal time).
        Negative = favorable. Returns None when not computable: non-filled,
        missing market_price_at_signal (paper-mode or pre-migration code paths),
        or unknown side.

        Formula:
            YES: (fill_price - market_price_at_signal) * 100 * contracts
            NO:  (market_price_at_signal - fill_price) * 100 * contracts
        """
        if r.get("status") != "filled":
            return None
        fill = r.get("fill_price")
        mkt = r.get("market_price_at_signal")
        if fill is None or mkt is None or float(mkt) == 0.0:
            return None
        contracts = int(r.get("contracts") or 0)
        if contracts <= 0:
            return None
        side = (r.get("side") or "").lower()
        if side == "yes":
            per_contract = (float(fill) - float(mkt)) * 100
        elif side == "no":
            per_contract = (float(mkt) - float(fill)) * 100
        else:
            return None
        return int(round(per_contract * contracts))

    @staticmethod
    def _compute_decomp_components(r: Dict) -> tuple:
        """Engineering B.2 — compute 3 of 4 slippage components.
        cancel_replace remains NULL until order lifecycle logging lands.
        Returns (spread_cost_cents, adverse_move_cents, partial_fill_cents).
        """
        try:
            from ep_slippage_decomposition import decompose_fill_slippage
        except Exception:
            return (None, None, None)
        if r.get("status") != "filled":
            return (None, None, None)
        out = decompose_fill_slippage(
            side                       = (r.get("side") or "").lower(),
            contracts_requested        = int(r.get("contracts_requested") or r.get("contracts") or 0),
            contracts_filled           = int(r.get("contracts") or 0),
            fill_price_cents           = int(round(float(r.get("fill_price") or 0) * 100)),
            yes_bid_at_placement_cents = int(r.get("yes_bid_at_placement_cents") or 0),
            yes_ask_at_placement_cents = int(r.get("yes_ask_at_placement_cents") or 0),
            mid_at_placement_cents     = int(r.get("mid_at_placement_cents") or 0),
            mid_at_fill_cents          = int(r.get("mid_at_fill_cents") or 0),
        )
        return (
            out.get("spread_cost_cents"),
            out.get("adverse_move_cents"),
            out.get("partial_fill_cents"),
        )

    async def _insert_executions(self, rows: List[Dict]) -> None:
        records = []
        for r in rows:
            ts_us = r.get("ts_us") or 0
            _spread, _adverse, _partial = self._compute_decomp_components(r)
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
                r.get("predicted_edge"),
                r.get("realized_pnl_cents"),
                self._compute_slippage_cents(r),
                _spread,     # B.2 spread_cost_cents
                _adverse,    # B.2 adverse_move_cents
                _partial,    # B.2 partial_fill_cents
            ))
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO executions
                    (exec_id, signal_id, reported_at, status, reject_reason,
                     ticker, side, asset_class, contracts, fill_price,
                     fee_cents, cost_cents, edge_captured, order_id, mode, payload,
                     predicted_edge, realized_pnl_cents, slippage_cents,
                     spread_cost_cents, adverse_move_cents, partial_fill_cents)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
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


    async def _insert_position_history(self, rows: List[Dict]) -> None:
        records = []
        for r in rows:
            entered_at = r.get("entered_at")
            exited_at  = r.get("exited_at")
            try:
                if entered_at and not hasattr(entered_at, "tzinfo"):
                    entered_at = datetime.fromisoformat(entered_at.replace("Z", "+00:00"))
                if exited_at and not hasattr(exited_at, "tzinfo"):
                    exited_at = datetime.fromisoformat(exited_at.replace("Z", "+00:00"))
            except Exception:
                exited_at = datetime.now(timezone.utc)
            # Settlement-reconciliation columns (Phase 3 v2, 2026-05-02). All four are
            # NULL for non-settlement rows; existing 11-column writes pass nothing for
            # them and r.get(...) returns None — no behaviour change for legacy call
            # sites.
            settlement_ts = r.get("settlement_ts")
            try:
                if settlement_ts and not hasattr(settlement_ts, "tzinfo"):
                    settlement_ts = datetime.fromisoformat(
                        settlement_ts.replace("Z", "+00:00")
                    )
            except Exception:
                settlement_ts = None
            records.append((
                r.get("entry_exec_id"),    # nullable since 2026-04-29 schema relaxation
                r.get("ticker", ""),
                r.get("side", ""),
                r.get("contracts", 0),
                r.get("entry_cents", 0),
                r.get("exit_cents", 0),
                r.get("realized_pnl_cents", 0),
                r.get("exit_reason"),
                entered_at,
                exited_at or datetime.now(timezone.utc),
                r.get("strategy"),         # added 2026-04-29 — used by terminal_trades
                                            # view's COALESCE when entry_exec_id is NULL
                                            # or doesn't match an executions row
                settlement_ts,             # added 2026-05-02 (Phase 3 v2)
                r.get("cost_basis_source"),
                r.get("kalshi_fee_cents"),
                r.get("kalshi_revenue_cents"),
            ))
        # ON CONFLICT (ticker, settlement_ts) WHERE settlement_ts IS NOT NULL DO NOTHING
        # leverages position_history_settlement_uniq partial unique index for
        # settlement rows. For non-settlement rows (settlement_ts IS NULL) the
        # predicate fails, no arbiter applies, and the insert proceeds normally —
        # same behaviour as the prior `ON CONFLICT DO NOTHING` (pkey is BIGSERIAL,
        # no natural conflict).
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO position_history
                    (entry_exec_id, ticker, side, contracts, entry_cents, exit_cents,
                     realized_pnl_cents, exit_reason, entered_at, exited_at, strategy,
                     settlement_ts, cost_basis_source, kalshi_fee_cents, kalshi_revenue_cents)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (ticker, settlement_ts) WHERE settlement_ts IS NOT NULL DO NOTHING
                """,
                records,
            )


    async def _insert_market_snapshots(self, rows: List[Dict]) -> None:
        """Bulk-insert market snapshot rows using asyncpg COPY — ~10× faster than INSERT."""
        records = []
        for r in rows:
            close_time = r.get("close_time")
            if isinstance(close_time, str):
                try:
                    close_time = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                except Exception:
                    close_time = None
            # SMALLINT overflow guard: yes_price/bid/ask/spread are 0-100 but coerce defensively
            def _si(v) -> Optional[int]:
                if v is None:
                    return None
                try:
                    iv = int(v)
                    return max(-32768, min(32767, iv))
                except Exception:
                    return None
            def _int(v) -> Optional[int]:
                if v is None:
                    return None
                try:
                    return int(v)
                except Exception:
                    return None
            def _dec(v) -> Optional[float]:
                if v is None:
                    return None
                try:
                    return float(v)
                except Exception:
                    return None
            records.append((
                int(r["ts_us"]),
                r["ticker"],
                r.get("series_ticker"),
                _si(r.get("yes_bid")),
                _si(r.get("yes_ask")),
                _si(r.get("yes_price")),
                _si(r.get("spread")),
                _int(r.get("volume")),
                _int(r.get("open_interest")),
                close_time,
                _dec(r.get("signal_edge")),
                r.get("signal_side"),
                _dec(r.get("signal_fv")),
                _dec(r.get("signal_conf")),
            ))
        if not records:
            return
        async with self._pool.acquire() as conn:
            await conn.copy_records_to_table(
                "market_snapshots",
                records=records,
                columns=[
                    "ts_us", "ticker", "series_ticker",
                    "yes_bid", "yes_ask", "yes_price", "spread",
                    "volume", "open_interest", "close_time",
                    "signal_edge", "signal_side", "signal_fv", "signal_conf",
                ],
            )


# ── Singleton lifecycle ───────────────────────────────────────────────────────

async def init_audit_writer() -> None:
    global _writer
    dsn = os.environ.get("DATABASE_URL", "")
    # asyncpg requires postgresql:// not the SQLAlchemy postgresql+asyncpg:// prefix
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        log.warning("DATABASE_URL not set — audit writes disabled")
        _writer = None
        return
    candidate = PgAuditWriter(dsn)
    try:
        await candidate.start()
    except Exception as exc:
        log.error("init_audit_writer: pool creation failed (%s) — audit disabled", exc)
        # CRITICAL: do NOT leave the partially-constructed instance assigned.
        # Otherwise writes silently land in a queue that never drains.
        _writer = None
        return
    _writer = candidate


async def stop_audit_writer() -> None:
    global _writer
    if _writer is not None:
        await _writer.stop()
        _writer = None


def audit() -> "PgAuditWriter":
    if _writer is None:
        return _NoopWriter()  # type: ignore[return-value]
    return _writer
