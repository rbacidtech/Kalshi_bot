"""
ep_bus.py — RedisBus: thin async wrapper around Redis Streams + Hashes.

All I/O is non-blocking — never yields the event loop to a blocking call.
"""

import asyncio
import json
import time
from dataclasses import asdict
from typing import Dict, List, Optional

import redis.asyncio as aioredis

from ep_config import (
    EP_SIGNALS, EP_EXECUTIONS, EP_POSITIONS, EP_PRICES,
    EP_BALANCE, EP_SYSTEM, EP_CONFIG, EP_HEALTH,
    EXEC_GROUP, INTEL_GROUP, STREAM_BLOCK, log,
)
from ep_schema import ExecutionReport, PriceSnapshot, SignalMessage
from ep_pg_audit import audit


class RedisBus:
    """Redis-backed message bus connecting the Intel and Exec nodes.

    Uses Redis Streams for signal (Intel→Exec) and execution report (Exec→Intel)
    delivery, and a Redis Hash for shared position state.
    """

    def __init__(self, url: str, node_id: str):
        self.url     = url
        self.node_id = node_id
        self._r: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Connect to Redis and idempotently create consumer groups for all streams.

        Called once at startup before any publish or consume operations.
        """
        self._r = await aioredis.from_url(
            self.url,
            encoding               = "utf-8",
            decode_responses       = False,   # keep raw bytes; decoded in helpers
            socket_connect_timeout = 5,
            socket_timeout         = 10,      # per-command read timeout; retry_on_timeout handles retries
            retry_on_timeout       = True,
        )
        await self._ensure_consumer_groups()
        log.info("Redis connected (node=%s)", self.node_id)

    async def _ensure_consumer_groups(self) -> None:
        """Create streams + consumer groups idempotently.

        Strategy: try mkstream=False first so we only create the stream when it
        genuinely doesn't exist.  If the stream is missing (ERR no such key or
        NOGROUP variant), retry with mkstream=True.  BUSYGROUP means the group
        already exists — that's the happy path on every non-first startup.
        """
        for stream, group in [
            (EP_SIGNALS,    EXEC_GROUP),
            (EP_EXECUTIONS, INTEL_GROUP),
        ]:
            # EP_SIGNALS: id="$" — exec only processes NEW signals after join.
            #   Replaying from 0 on fresh creation would dump the entire signal
            #   history (thousands of entries) onto exec, causing duplicate orders.
            # EP_EXECUTIONS: id="0" — intel replays from start to recover any
            #   execution reports missed while intel was down (safe; read-only).
            group_start_id = "$" if stream == EP_SIGNALS else "0"
            for mkstream in (False, True):
                try:
                    await self._r.xgroup_create(stream, group, id=group_start_id, mkstream=mkstream)
                    log.info(
                        "Consumer group created: %s on %s (node=%s mkstream=%s)",
                        group, stream, self.node_id, mkstream,
                    )
                    break   # success — move to next stream
                except aioredis.ResponseError as exc:
                    err = str(exc)
                    if "BUSYGROUP" in err:
                        # Group already exists — nothing to do
                        break
                    if not mkstream and (
                        "no such key" in err.lower()
                        or "requires the key to exist" in err.lower()
                        or "MKSTREAM" in err
                    ):
                        # Stream doesn't exist yet; retry with mkstream=True
                        continue
                    raise   # unexpected error — propagate

    # ── Signal stream (Intel → Exec) ──────────────────────────────────────────

    async def publish_signal(self, sig: SignalMessage) -> str:
        """Publish a trading signal to the ep:signals stream (consumed by Exec).

        Returns the Redis stream entry ID assigned to this message.
        """
        sig.source_node = self.node_id
        eid = await self._r.xadd(EP_SIGNALS, sig.to_redis(), maxlen=10_000, approximate=True)
        try:
            audit().write("signals", asdict(sig))
        except Exception:
            log.debug("audit skipped for signal %s", sig.signal_id)
        return eid.decode() if isinstance(eid, bytes) else eid

    async def consume_signals(self, consumer_name: str):
        """
        Async generator: yields (entry_id, SignalMessage) from ep:signals.
        Blocks up to STREAM_BLOCK ms waiting for new entries.
        Caller must call ack_signal(entry_id) after processing.

        On startup, drains the PEL (pending entries from a prior crash) before
        switching to ">" (new only). This ensures crash-recovery: any signal
        that was delivered but not ACK'd before a crash is re-processed.

        On Redis error, logs and retries after 2 s — never raises.
        """
        # ── Phase 1: drain PEL from any prior crash ───────────────────────────
        # Read with ID "0" returns all pending (delivered-not-ACK'd) entries.
        # Loop until PEL is empty, then fall through to live ">" reads.
        log.info("consume_signals: draining PEL for consumer=%s", consumer_name)
        while True:
            try:
                pending = await self._r.xreadgroup(
                    groupname    = EXEC_GROUP,
                    consumername = consumer_name,
                    streams      = {EP_SIGNALS: "0"},   # "0" = pending entries
                    count        = 100,
                    block        = 0,   # non-blocking
                )
                if not pending:
                    break
                had_entries = False
                for _stream, entries in pending:
                    for entry_id, mapping in entries:
                        had_entries = True
                        try:
                            sig = SignalMessage.from_redis(mapping)
                            log.debug("PEL re-delivery: %s (age=%dms)",
                                      sig.ticker,
                                      (int(time.time() * 1_000_000) - sig.ts_us) // 1_000)
                            yield entry_id, sig
                        except Exception as exc:
                            log.warning("Malformed PEL entry %s — acking: %s", entry_id, exc)
                            await self.ack_signal(entry_id)
                if not had_entries:
                    break
            except aioredis.RedisError as exc:
                log.error("Redis PEL drain error: %s — retrying in 2s", exc)
                await asyncio.sleep(2)
                # continue the loop — do not break; breaking silently drops
                # unACK'd signals and they won't be retried until next restart
        log.info("consume_signals: PEL drained, switching to live stream")

        # ── Phase 2: live stream ──────────────────────────────────────────────
        while True:
            try:
                results = await self._r.xreadgroup(
                    groupname    = EXEC_GROUP,
                    consumername = consumer_name,
                    streams      = {EP_SIGNALS: ">"},   # ">" = undelivered only
                    count        = 10,
                    block        = STREAM_BLOCK,
                )
                if not results:
                    continue
                _batch: list = []
                for _stream, entries in results:
                    for entry_id, mapping in entries:
                        try:
                            sig = SignalMessage.from_redis(mapping)
                            _batch.append((entry_id, sig))
                        except Exception as exc:
                            log.warning("Malformed signal %s — acking and skipping: %s",
                                        entry_id, exc)
                            await self.ack_signal(entry_id)
                _batch.sort(key=lambda x: getattr(x[1], "priority", 3))
                for entry_id, sig in _batch:
                    yield entry_id, sig

            except aioredis.ResponseError as exc:
                if "NOGROUP" in str(exc):
                    # Consumer group was lost (Intel restart can wipe it).
                    # Recreate at "0" (stream start) so signals published
                    # during the Intel restart window are replayed rather
                    # than dropped.
                    log.warning("Consumer group lost — recreating %s on %s",
                                EXEC_GROUP, EP_SIGNALS)
                    try:
                        await self._r.xgroup_create(
                            EP_SIGNALS, EXEC_GROUP, id="0", mkstream=True
                        )
                        log.info("Consumer group %s recreated at stream start (backlog replay)", EXEC_GROUP)
                    except aioredis.ResponseError as _cg_exc:
                        if "BUSYGROUP" not in str(_cg_exc):
                            log.error("Failed to recreate consumer group: %s", _cg_exc)
                    await asyncio.sleep(1)
                else:
                    log.error("Redis consume error: %s — retry in 2s", exc)
                    await asyncio.sleep(2)
            except aioredis.RedisError as exc:
                log.error("Redis consume error: %s — retry in 2s", exc)
                await asyncio.sleep(2)

    async def ack_signal(self, entry_id) -> None:
        """Acknowledge a signal entry, removing it from the ep:signals pending-entries list."""
        await self._r.xack(EP_SIGNALS, EXEC_GROUP, entry_id)

    # ── Execution stream (Exec → Intel) ───────────────────────────────────────

    async def publish_execution(self, report: ExecutionReport) -> str:
        """Publish an execution report to the ep:executions stream (consumed by Intel).

        Used by Intel for dedup and P&L tracking. Returns the stream entry ID.
        """
        report.source_node = self.node_id
        eid = await self._r.xadd(EP_EXECUTIONS, report.to_redis(),
                                  maxlen=5_000, approximate=True)
        try:
            audit().write("executions", asdict(report))
        except Exception:
            log.debug("audit skipped for exec %s", report.exec_id)
        return eid.decode() if isinstance(eid, bytes) else eid

    async def consume_executions(self, consumer_name: str) -> List[ExecutionReport]:
        """
        Non-blocking read of pending execution reports for Intel.
        Returns immediately with whatever is available in the stream.
        """
        reports = []
        try:
            results = await self._r.xreadgroup(
                groupname    = INTEL_GROUP,
                consumername = consumer_name,
                streams      = {EP_EXECUTIONS: ">"},
                count        = 50,
                block        = 0,   # non-blocking
            )
            if results:
                for _stream, entries in results:
                    for entry_id, mapping in entries:
                        try:
                            report = ExecutionReport.from_redis(mapping)
                            reports.append(report)
                        except Exception as exc:
                            log.debug("Malformed execution report: %s", exc)
                        await self._r.xack(EP_EXECUTIONS, INTEL_GROUP, entry_id)
        except aioredis.ResponseError as exc:
            if "NOGROUP" in str(exc):
                log.warning(
                    "intel-consumers group lost on ep:executions — recreating from id=0 "
                    "(node=%s)", self.node_id,
                )
                recreated = False
                for mkstream in (False, True):
                    try:
                        await self._r.xgroup_create(
                            EP_EXECUTIONS, INTEL_GROUP, id="0", mkstream=mkstream
                        )
                        log.info(
                            "intel-consumers group recreated on ep:executions at id=0 "
                            "(mkstream=%s)", mkstream,
                        )
                        recreated = True
                        break
                    except aioredis.ResponseError as _cg_exc:
                        cg_err = str(_cg_exc)
                        if "BUSYGROUP" in cg_err:
                            recreated = True
                            break
                        if not mkstream and (
                            "no such key" in cg_err.lower()
                            or "requires the key to exist" in cg_err.lower()
                            or "MKSTREAM" in cg_err
                        ):
                            continue   # stream missing — retry with mkstream=True
                        log.error(
                            "Failed to recreate intel-consumers group: %s", _cg_exc
                        )
                        break
                # Immediately retry the read so this cycle doesn't silently return [].
                if recreated:
                    try:
                        results = await self._r.xreadgroup(
                            groupname    = INTEL_GROUP,
                            consumername = consumer_name,
                            streams      = {EP_EXECUTIONS: ">"},
                            count        = 50,
                            block        = 0,
                        )
                        if results:
                            for _stream, entries in results:
                                for entry_id, mapping in entries:
                                    try:
                                        report = ExecutionReport.from_redis(mapping)
                                        reports.append(report)
                                    except Exception as exc2:
                                        log.debug("Malformed execution report after recovery: %s", exc2)
                                    await self._r.xack(EP_EXECUTIONS, INTEL_GROUP, entry_id)
                    except aioredis.RedisError as _retry_exc:
                        log.debug("consume_executions retry after recovery failed: %s", _retry_exc)
            else:
                log.debug("consume_executions error: %s", exc)
        except aioredis.RedisError as exc:
            log.debug("consume_executions error: %s", exc)
        return reports

    # ── Shared position state (Exec writes, Intel reads for dedup) ────────────

    async def set_position(self, ticker: str, pos: dict) -> None:
        """Write or overwrite a position record in the ep:positions Redis hash, keyed by ticker."""
        await self._r.hset(EP_POSITIONS, ticker, json.dumps(pos))

    async def delete_position(self, ticker: str) -> None:
        """Remove a position from the ep:positions Redis hash (called on close or tombstone)."""
        await self._r.hdel(EP_POSITIONS, ticker)

    async def position_exists(self, ticker: str) -> bool:
        """Return True if a position record exists in ep:positions for this ticker."""
        return bool(await self._r.hexists(EP_POSITIONS, ticker))

    async def get_all_positions(self) -> Dict[str, dict]:
        """Return all open positions as a dict keyed by ticker.

        Used for dedup checks, exposure calculation, and P&L.
        """
        raw    = await self._r.hgetall(EP_POSITIONS)
        result = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                result[key] = json.loads(v)
            except (json.JSONDecodeError, Exception) as _e:
                log.warning("Corrupted position data for %s — skipping (will not dedup): %s", key, _e)
        return result

    # ── Price state (Intel writes, Exec reads for exits) ──────────────────────

    async def publish_prices(self, snapshot: PriceSnapshot) -> None:
        """Publish a price snapshot to the ep:prices Redis hash (latest mid prices for all tracked tickers)."""
        if snapshot.prices:
            await self._r.hset(EP_PRICES, mapping=snapshot.to_redis_hash())

    async def get_prices(self, tickers: List[str] = None) -> Dict[str, dict]:
        """Fetch latest Kalshi mid prices from the ep:prices Redis hash.

        Optionally filters to a subset of tickers; returns all if tickers is None.
        """
        if tickers:
            values = await self._r.hmget(EP_PRICES, *tickers)
            raw    = {t.encode(): v for t, v in zip(tickers, values) if v is not None}
        else:
            raw = await self._r.hgetall(EP_PRICES)
        result = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            if v:
                try:
                    result[key] = json.loads(v)
                except json.JSONDecodeError:
                    pass
        return result

    # ── Balance state ─────────────────────────────────────────────────────────

    async def set_balance(self, balance_cents: int, mode: str, portfolio_value_cents: int = 0) -> None:
        await self._r.hset(EP_BALANCE, self.node_id, json.dumps({
            "balance_cents":         balance_cents,
            "portfolio_value_cents": portfolio_value_cents,
            "mode":                  mode,
            "ts_us":                 int(time.time() * 1_000_000),
        }))

    async def get_all_balances(self) -> Dict[str, dict]:
        raw    = await self._r.hgetall(EP_BALANCE)
        result = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                result[key] = json.loads(v)
            except json.JSONDecodeError:
                pass
        return result

    # ── Runtime config overrides ──────────────────────────────────────────────

    async def get_config_override(self, key: str) -> Optional[str]:
        val = await self._r.hget(EP_CONFIG, key)
        return val.decode() if isinstance(val, bytes) else val

    async def is_halted(self) -> bool:
        """Check ops-level emergency stop flag."""
        return await self.get_config_override("HALT_TRADING") == "1"

    # ── System events ─────────────────────────────────────────────────────────

    async def publish_system_event(self, event_type: str, detail: str = "") -> None:
        await self._r.xadd(EP_SYSTEM, {"payload": json.dumps({
            "event_type": event_type,
            "node":       self.node_id,
            "detail":     detail,
            "ts_us":      int(time.time() * 1_000_000),
        })}, maxlen=1_000, approximate=True)

    async def get_latest_heartbeat(self, node_prefix: str) -> Optional[float]:
        """
        Scan the ep:system stream (most-recent-first) for the latest HEARTBEAT
        event from a node whose ID starts with `node_prefix`.

        Returns the wall-clock timestamp (seconds, float) of the most recent
        matching heartbeat, or None if no matching entry is found.

        Reads up to the last 200 entries — sufficient for several hours of
        60-second heartbeat cadence from both nodes.
        """
        try:
            entries = await self._r.xrevrange(EP_SYSTEM, count=200)
            for _entry_id, mapping in entries:
                key     = b"payload" if b"payload" in mapping else "payload"
                raw     = mapping.get(key)
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if (
                    ev.get("event_type") == "HEARTBEAT"
                    and str(ev.get("node", "")).startswith(node_prefix)
                ):
                    ts_us = ev.get("ts_us")
                    if ts_us:
                        return float(ts_us) / 1_000_000
            return None
        except Exception:
            return None

    # ── BTC price history (rolling list for dashboard chart) ─────────────────

    async def push_btc_history(
        self,
        price: float,
        rsi:   Optional[float],
        z:     Optional[float],
        maxlen: int = 240,
    ) -> None:
        """
        Prepend a BTC snapshot to ep:btc_history (newest first) and trim to
        `maxlen` entries.  The dashboard reads this list to draw the price chart.
        """
        payload = json.dumps({
            "price": round(price, 2),
            "rsi":   round(rsi, 2) if rsi is not None else None,
            "z":     round(z,   2) if z   is not None else None,
            "ts":    int(time.time()),
        })
        await self._r.lpush("ep:btc_history", payload)
        await self._r.ltrim("ep:btc_history", 0, maxlen - 1)

    # ── Health / connectivity ─────────────────────────────────────────────────

    async def publish_health(self, summary: dict) -> None:
        """
        Write the health summary from ep_health.get_health_summary() into
        the ep:health Redis hash.  Intel calls this every 60 seconds from
        its heartbeat loop.

        The hash field is the node_id so multi-node deployments each
        contribute their own slice without overwriting each other.
        """
        try:
            payload = json.dumps({**summary, "ts_us": int(time.time() * 1_000_000)})
            await self._r.hset(EP_HEALTH, self.node_id, payload)
        except Exception as exc:
            log.warning("Failed to publish health summary: %s", exc)

    async def ping(self) -> bool:
        """
        Test Redis connectivity.  Returns True if the server responds.

        Call this from the intel heartbeat loop every 60 seconds.
        Three consecutive failures should trigger a CRITICAL log — the caller
        owns that counter and log call.
        """
        try:
            result = await self._r.ping()
            return bool(result)
        except Exception as exc:
            log.warning("Redis ping failed: %s", exc)
            return False

    async def close(self) -> None:
        if self._r:
            await self._r.aclose()
