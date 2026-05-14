"""Kill-switch supervisor — separate process from trader.

Engineering S.3 §5 mandate: "kill-switch supervisor runs as separate systemd
unit from trader. Trader-process failure modes (deadlock, OOM, infinite loop,
runaway exception) must not be the same failure modes that take down the
supervisor."

This module is the supervisor entry point (`python -m ep_supervisor` or
`/root/EdgePulse/.venv/bin/python3 ep_supervisor.py`). It runs under
edgepulse-supervisor.service with Restart=always.

What it does every 10 seconds:
  1. Read halt-relevant state from Redis (ep:performance:1, ep:bankroll_anchor,
     ep:positions, ep:executions, ep:settle:seen, ep:config).
  2. Evaluate the four auto-halt conditions independently of the trader:
     - Daily P&L loss > override_daily_pnl_halt_pct (default 5%)
     - Balance velocity drop > override_balance_velocity_pct (default 10%)
       with zero exec + settle activity in window
     - Per-strategy 7d cumulative loss > override_strategy_loss_threshold_pct
       (default 10%)
     - Hard-halt flag file (/root/EdgePulse/.hard_halt) present
  3. If any condition trips, set ep:config:HALT_TRADING=1 and ep:halt mapping.
  4. Write ep:supervisor:heartbeat with current timestamp (trader monitors this).

The trader (ep_exec) also runs these checks in `_business_health_loop` as
defense-in-depth. The supervisor's contribution is **process isolation**: when
the trader is wedged (deadlocked event loop, OOM, infinite loop), the in-trader
checks fail with it. The supervisor's separate process keeps evaluating + can
set HALT_TRADING from outside.

The supervisor does NOT currently issue cancel-all-resting on its own (that
requires Kalshi client + auth state); the trader's own hard-halt path handles
order cancellation when it sees the flag file. If the trader is wedged badly
enough to miss the flag file, that's a Phase 2+ refinement (forced shutdown
via direct API from supervisor).

Heartbeat semantics: trader checks ep:supervisor:heartbeat in its own loop;
if stale > 60s, trader defensive-halts (safety-first when supervisor dies).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis.asyncio as redis_async


log = logging.getLogger("ep_supervisor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] supervisor: %(message)s",
)


_REDIS_URL = os.environ.get("REDIS_URL", "unix:///run/redis/redis.sock")
_HARD_HALT_FLAG = Path(os.environ.get("EP_HARD_HALT_FLAG", "/root/EdgePulse/.hard_halt"))
_HEARTBEAT_KEY = "ep:supervisor:heartbeat"
_CHECK_INTERVAL_S = 10                  # Engineering S.3 §3 default
_BALANCE_HISTORY_MAX = 12               # 1h window @ 5min effective cadence
_BALANCE_SNAPSHOT_EVERY_S = 300         # 5min between balance sampling


async def _set_halt(
    r: redis_async.Redis,
    reason: str,
    extra: Optional[dict[str, str]] = None,
) -> None:
    """Set HALT_TRADING=1 + ep:halt mapping. Idempotent (no-op if already 1)."""
    cur = await r.hget("ep:config", "HALT_TRADING")
    if cur in (b"1", "1"):
        return  # someone (in-process check, operator, or prior supervisor cycle) already set it
    try:
        await r.hset("ep:config", "HALT_TRADING", "1")
        mapping = {
            "reason":        reason,
            "tripped_at_us": str(int(time.time() * 1_000_000)),
            "source":        "supervisor",
        }
        if extra:
            mapping.update(extra)
        await r.hset("ep:halt", mapping=mapping)
        log.warning("HALT_TRADING set by supervisor — reason=%s extra=%s", reason, extra or {})
    except Exception as exc:
        log.error("Failed to set halt (reason=%s): %s", reason, exc)


async def _check_hard_halt_flag(r: redis_async.Redis) -> bool:
    """Hard halt flag-file present? If newly seen, also set HALT_TRADING."""
    try:
        present = _HARD_HALT_FLAG.exists()
    except Exception:
        return False
    if present:
        await _set_halt(r, "hard_halt_flag_file", {"flag_path": str(_HARD_HALT_FLAG)})
    return present


async def _check_daily_pnl(r: redis_async.Redis) -> None:
    """24h realized P&L loss > X% of bankroll → halt. Mirrors S.3.1 in ep_exec."""
    try:
        thresh_raw = await r.hget("ep:config", "override_daily_pnl_halt_pct")
        thresh = abs(float(thresh_raw)) if thresh_raw else 5.0
    except (ValueError, TypeError):
        thresh = 5.0
    try:
        perf_raw = await r.get("ep:performance:1")
        if not perf_raw:
            return
        perf = json.loads(perf_raw)
        day_pnl = int(float(perf.get("total_pnl_cents", 0) or 0))
        anchor_key = f"ep:bankroll_anchor:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        anchor_raw = await r.get(anchor_key)
        bankroll = int(anchor_raw) if anchor_raw else 0
        if bankroll <= 0 or day_pnl >= 0:
            return
        loss_pct = abs(day_pnl) / bankroll * 100
        if loss_pct >= thresh:
            await _set_halt(r, "daily_pnl_loss", {
                "daily_pnl_cents": str(day_pnl),
                "bankroll_cents":  str(bankroll),
                "loss_pct":        f"{loss_pct:.2f}",
                "threshold_pct":   f"{thresh:.2f}",
            })
    except Exception as exc:
        log.debug("daily_pnl check: %s", exc)


async def _check_per_strategy_loss(r: redis_async.Redis) -> None:
    """Per-strategy 7d cumulative loss > X% bankroll → auto-disable.
    Mirrors S.3.3 in ep_exec.
    """
    try:
        thresh_raw = await r.hget("ep:config", "override_strategy_loss_threshold_pct")
        thresh_pct = abs(float(thresh_raw)) if thresh_raw else 10.0
    except (ValueError, TypeError):
        thresh_pct = 10.0
    try:
        perf7_raw = await r.get("ep:performance:7")
        if not perf7_raw:
            return
        perf7 = json.loads(perf7_raw)
        by_strat = perf7.get("by_strategy", {}) or {}
        anchor_key = f"ep:bankroll_anchor:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        anchor_raw = await r.get(anchor_key)
        bankroll = int(anchor_raw) if anchor_raw else 0
        if bankroll <= 0 or not by_strat:
            return
        loss_cap_cents = bankroll * thresh_pct / 100
        disabled_raw = await r.hget("ep:config", "disabled_model_sources") or ""
        if isinstance(disabled_raw, bytes):
            disabled_raw = disabled_raw.decode()
        disabled_set = {s.strip() for s in disabled_raw.split(",") if s.strip()}
        newly = []
        for strat, stats in by_strat.items():
            if not strat or strat in disabled_set:
                continue
            pnl = int(stats.get("pnl_cents", 0) or 0)
            if pnl < -loss_cap_cents:
                newly.append((strat, pnl))
                disabled_set.add(strat)
        if newly:
            await r.hset("ep:config", "disabled_model_sources", ",".join(sorted(disabled_set)))
            now_us = int(time.time() * 1_000_000)
            for sn, spnl in newly:
                await r.hset("ep:auto_disabled", sn,
                             f"ts_us={now_us}|pnl_cents={spnl}|threshold_pct={thresh_pct:.2f}|window_days=7|source=supervisor")
                log.warning("Strategy circuit breaker (supervisor): %s pnl=$%.2f auto-disabled",
                            sn, spnl / 100)
    except Exception as exc:
        log.debug("per_strategy_loss check: %s", exc)


async def _check_balance_velocity(
    r: redis_async.Redis,
    history: list[tuple[float, int]],
) -> None:
    """Balance dropped >X% in window with zero exec + settle activity → halt.
    Mirrors S.3.2 in ep_exec. `history` is supervisor-local state.
    """
    try:
        thresh_raw = await r.hget("ep:config", "override_balance_velocity_pct")
        thresh = abs(float(thresh_raw)) if thresh_raw else 10.0
    except (ValueError, TypeError):
        thresh = 10.0
    try:
        balances = await r.hgetall("ep:balance")
        intel_bal: Optional[dict] = None
        for k, v in balances.items():
            key = k.decode() if isinstance(k, bytes) else k
            if "intel" in key.lower():
                val = v.decode() if isinstance(v, bytes) else v
                intel_bal = json.loads(val) if val else None
                break
        if intel_bal is None:
            return
        cur_bal = int(intel_bal.get("balance_cents", 0) or 0)
        now = time.time()
        history.append((now, cur_bal))
        # trim to last hour (6 entries at 10s × 60s sampling)
        while history and now - history[0][0] > 3600:
            history.pop(0)
        if len(history) < 6:
            return
        oldest_ts, oldest_bal = history[0]
        delta = cur_bal - oldest_bal
        anchor_key = f"ep:bankroll_anchor:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        anchor_raw = await r.get(anchor_key)
        bankroll = int(anchor_raw) if anchor_raw else cur_bal
        if bankroll <= 0 or delta >= 0:
            return
        drop_pct = abs(delta) / bankroll * 100
        if drop_pct < thresh:
            return
        # Activity check in window
        ws_unix = int(oldest_ts)
        ws_stream_id = f"{int(oldest_ts * 1000)}-0"
        try:
            execs = await r.xrange("ep:executions", ws_stream_id, "+", count=1)
            exec_n = len(execs)
        except Exception:
            exec_n = 1
        try:
            settle_n = await r.zcount("ep:settle:seen", ws_unix, "+inf")
        except Exception:
            settle_n = 1
        if exec_n == 0 and settle_n == 0:
            await _set_halt(r, "balance_velocity_unexplained", {
                "delta_cents":     str(delta),
                "drop_pct":        f"{drop_pct:.2f}",
                "threshold_pct":   f"{thresh:.2f}",
                "exec_count":      "0",
                "settle_count":    "0",
            })
    except Exception as exc:
        log.debug("balance_velocity check: %s", exc)


async def _write_heartbeat(r: redis_async.Redis) -> None:
    """Trader monitors this via ep_exec._business_health_loop."""
    try:
        await r.set(_HEARTBEAT_KEY, str(int(time.time() * 1_000_000)), ex=120)
    except Exception as exc:
        log.warning("heartbeat write failed: %s", exc)


async def supervisor_main() -> None:
    log.info("EdgePulse supervisor starting — check interval %ds, redis %s",
             _CHECK_INTERVAL_S, _REDIS_URL)
    r = redis_async.from_url(_REDIS_URL)
    try:
        await r.ping()
        log.info("Redis ping OK")
    except Exception as exc:
        log.error("Redis ping failed: %s — exiting (systemd will restart)", exc)
        sys.exit(1)

    balance_history: list[tuple[float, int]] = []
    last_balance_sample = 0.0
    stop_event = asyncio.Event()

    def _on_sigterm():
        log.info("SIGTERM received — supervisor shutting down")
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    loop.add_signal_handler(signal.SIGINT, _on_sigterm)

    while not stop_event.is_set():
        await _write_heartbeat(r)
        await _check_hard_halt_flag(r)
        await _check_daily_pnl(r)
        await _check_per_strategy_loss(r)
        # Balance sampling at a slower cadence than the main loop so the
        # history covers ~1h with manageable list size.
        now = time.time()
        if now - last_balance_sample >= _BALANCE_SNAPSHOT_EVERY_S:
            await _check_balance_velocity(r, balance_history)
            last_balance_sample = now
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_CHECK_INTERVAL_S)
        except asyncio.TimeoutError:
            pass

    await r.close()
    log.info("Supervisor exited cleanly")


if __name__ == "__main__":
    asyncio.run(supervisor_main())
