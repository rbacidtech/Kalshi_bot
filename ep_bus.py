"""
ep_bus.py — RedisBus: thin async wrapper around Redis Streams + Hashes.

All I/O is non-blocking — never yields the event loop to a blocking call.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional

import redis.asyncio as aioredis

from ep_config import (
    EP_SIGNALS, EP_EXECUTIONS, EP_POSITIONS, EP_PRICES,
    EP_BALANCE, EP_SYSTEM, EP_CONFIG,
    EXEC_GROUP, INTEL_GROUP, STREAM_BLOCK, log,
)
from ep_schema import ExecutionReport, PriceSnapshot, SignalMessage


class RedisBus:

    def __init__(self, url: str, node_id: str):
        self.url     = url
        self.node_id = node_id
        self._r: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        self._r = await aioredis.from_url(
            self.url,
            encoding               = "utf-8",
            decode_responses       = False,   # keep raw bytes; decoded in helpers
            socket_keepalive       = True,
            socket_connect_timeout = 5,
            retry_on_timeout       = True,
        )
        await self._ensure_consumer_groups()
        log.info("Redis connected (node=%s)", self.node_id)

    async def _ensure_consumer_groups(self) -> None:
        """Create streams + consumer groups idempotently."""
        for stream, group in [
            (EP_SIGNALS,    EXEC_GROUP),
            (EP_EXECUTIONS, INTEL_GROUP),
        ]:
            try:
                await self._r.xgroup_create(stream, group, id="0", mkstream=True)
                log.debug("Created consumer group %s on %s", group, stream)
            except aioredis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise   # unexpected — propagate

    # ── Signal stream (Intel → Exec) ──────────────────────────────────────────

    async def publish_signal(self, sig: SignalMessage) -> str:
        sig.source_node = self.node_id
        eid = await self._r.xadd(EP_SIGNALS, sig.to_redis(), maxlen=10_000, approximate=True)
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
                log.error("Redis PEL drain error: %s — retry in 2s", exc)
                await asyncio.sleep(2)
                break   # fall through to live reads; PEL will retry next restart
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
                for _stream, entries in results:
                    for entry_id, mapping in entries:
                        try:
                            sig = SignalMessage.from_redis(mapping)
                            yield entry_id, sig
                        except Exception as exc:
                            log.warning("Malformed signal %s — acking and skipping: %s",
                                        entry_id, exc)
                            await self.ack_signal(entry_id)

            except aioredis.RedisError as exc:
                log.error("Redis consume error: %s — retry in 2s", exc)
                await asyncio.sleep(2)

    async def ack_signal(self, entry_id) -> None:
        await self._r.xack(EP_SIGNALS, EXEC_GROUP, entry_id)

    # ── Execution stream (Exec → Intel) ───────────────────────────────────────

    async def publish_execution(self, report: ExecutionReport) -> str:
        report.source_node = self.node_id
        eid = await self._r.xadd(EP_EXECUTIONS, report.to_redis(),
                                  maxlen=5_000, approximate=True)
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
        except aioredis.RedisError as exc:
            log.debug("consume_executions error: %s", exc)
        return reports

    # ── Shared position state (Exec writes, Intel reads for dedup) ────────────

    async def set_position(self, ticker: str, pos: dict) -> None:
        await self._r.hset(EP_POSITIONS, ticker, json.dumps(pos))

    async def delete_position(self, ticker: str) -> None:
        await self._r.hdel(EP_POSITIONS, ticker)

    async def position_exists(self, ticker: str) -> bool:
        return bool(await self._r.hexists(EP_POSITIONS, ticker))

    async def get_all_positions(self) -> Dict[str, dict]:
        raw    = await self._r.hgetall(EP_POSITIONS)
        result = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                result[key] = json.loads(v)
            except json.JSONDecodeError:
                pass
        return result

    # ── Price state (Intel writes, Exec reads for exits) ──────────────────────

    async def publish_prices(self, snapshot: PriceSnapshot) -> None:
        if snapshot.prices:
            await self._r.hset(EP_PRICES, mapping=snapshot.to_redis_hash())

    async def get_prices(self, tickers: List[str] = None) -> Dict[str, dict]:
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

    async def set_balance(self, balance_cents: int, mode: str) -> None:
        await self._r.hset(EP_BALANCE, self.node_id, json.dumps({
            "balance_cents": balance_cents,
            "mode":          mode,
            "ts_us":         int(time.time() * 1_000_000),
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

    async def close(self) -> None:
        if self._r:
            await self._r.aclose()
