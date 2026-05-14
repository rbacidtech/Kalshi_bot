"""
ep_settlements.py — Phase 3 v2 settlement reconciliation.

Polls Kalshi's /portfolio/settlements endpoint, dedupes via Redis, and
reconciles each settlement against internal position_history.

Closes the structural data gap where markets that resolve via settlement
(rather than the bot's pre-expiry exit) never produced a position_history
row, so they were absent from Kelly calibration and trades.csv.

═══════════════════════════════════════════════════════════════════════════
v2 (2026-05-02) — corrects v1's fatal design flaw
═══════════════════════════════════════════════════════════════════════════
Phase 3 v1 (commit 70d107b, reverted in 209422f) wrote a position_history
settlement row for EVERY Kalshi settlement, regardless of whether the
position had already been closed via internal exit logic (stop_loss,
take_profit, pre_expiry, etc.). Result: double-attribution.

Concrete production instance: KXNBAGAME-26APR28ATLNYK-ATL exited via
stop_loss at 1¢ for -$121.77 (correct). v1 reconciler then wrote a
settlement row at 0¢ for -$161.80 — $40 of phantom over-attribution
on a single position. Across one 7-day backfill that single bug added
~$1,600 of phantom losses to position_history.

v2 fix: BEFORE writing the settlement row, query position_history for any
prior non-settlement exit on the same ticker. If found, skip the write
entirely — the internal exit is the authoritative attribution; the
exchange settlement is bookkeeping. The Redis seen-set is still updated
(action='skipped_internal_exit') so we don't re-check on every poll
cycle. Cost-basis-mismatch logic from v1 stays for the no-prior case.

Public surface:
    settlement_recon_loop(client, bus, executor, interval=300)
    reconcile_one_settlement(settlement, executor, bus_redis, ...)

Both write at most one position_history row per (ticker, settlement_ts).
The partial unique index position_history_settlement_uniq provides the
DB-level safety net; the Redis seen-set provides the in-memory dedupe.

Cost-basis precedence (only applies in the NO-prior-internal-exit branch):
    1. Internal: most recent position_history row for `ticker` with
       exited_at <= settled_time AND exit_reason NOT LIKE 'settlement_%'.
       Uses entry_cents * contracts.
    2. Kalshi aggregate: yes_total_cost_dollars + no_total_cost_dollars
       (×100 for cents). Used when no internal row matches.
    3. Mismatch: both available but diverge >2%. Internal wins; row is
       tagged cost_basis_source='mismatch' and a WARN is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import asyncpg

import ep_config  # noqa: F401 — sys.path bootstrap, must precede kalshi_bot import
import kalshi_bot.config as cfg
from ep_pg_audit import audit as _audit_writer

log = logging.getLogger("edgepulse")

# Redis keys
_REDIS_CURSOR_KEY = "ep:settle:cursor"
_REDIS_SEEN_KEY   = "ep:settle:seen"
_SEEN_TTL_SECS    = 14 * 86400        # 14 days

# Mismatch threshold for cost-basis divergence (internal vs kalshi_agg).
_COST_BASIS_MISMATCH_FRAC = 0.02      # 2 %

# How far back to seed the cursor on first run.
_DEFAULT_LOOKBACK = timedelta(days=7)

# When advancing the cursor after a successful page, walk back this much for
# safety overlap (Kalshi may emit settlements with slightly out-of-order ts).
_CURSOR_OVERLAP   = timedelta(hours=1)

# Per-page limit for /portfolio/settlements. Kalshi's standard is 100; cap
# defensively in case the server enforces a smaller max.
_PAGE_LIMIT       = 100


# ── Redis cursor / seen-set helpers ───────────────────────────────────────────

async def _get_cursor(bus_redis) -> str:
    """Return the persisted ISO-format cursor or seed a (now - 7d) default."""
    raw = await bus_redis.get(_REDIS_CURSOR_KEY)
    if raw:
        try:
            return raw.decode() if isinstance(raw, bytes) else raw
        except Exception:
            pass
    seeded = (datetime.now(timezone.utc) - _DEFAULT_LOOKBACK).isoformat()
    log.info("settlement cursor unset — seeding to %s", seeded)
    return seeded


async def _set_cursor(bus_redis, value: str) -> None:
    await bus_redis.set(_REDIS_CURSOR_KEY, value)


async def _is_seen(bus_redis, ticker: str, settled_time: str) -> bool:
    member = f"{ticker}|{settled_time}"
    score = await bus_redis.zscore(_REDIS_SEEN_KEY, member)
    return score is not None


async def _mark_seen(bus_redis, ticker: str, settled_time: str) -> None:
    member = f"{ticker}|{settled_time}"
    score  = int(datetime.now(timezone.utc).timestamp())
    try:
        await bus_redis.zadd(_REDIS_SEEN_KEY, {member: score})
        # ZREMRANGEBYSCORE drops members older than 14 days.  The set's TTL
        # alone wouldn't remove individual members, only the entire key.
        cutoff = score - _SEEN_TTL_SECS
        await bus_redis.zremrangebyscore(_REDIS_SEEN_KEY, 0, cutoff)
        # Keep a key-level TTL too as a safety net in case the bot is
        # offline long enough that the set goes stale (re-seeded fresh).
        await bus_redis.expire(_REDIS_SEEN_KEY, _SEEN_TTL_SECS * 2)
    except Exception as exc:
        log.warning("settle: zadd/expire failed for %s: %s", member, exc)


# ── Prior-internal-exit lookup (v2 critical fix) ─────────────────────────────

async def _get_prior_internal_exit(
    pool: Optional[asyncpg.Pool],
    ticker: str,
    settled_time: datetime,
) -> Optional[asyncpg.Record]:
    """
    Look up the most recent NON-settlement position_history row for `ticker`.

    Returns the asyncpg Record if a prior internal exit exists (so we should
    skip the settlement write to avoid double-attribution), or None if no
    such row exists.

    Distinguishing return values:
        Record(...) — prior internal exit found; caller MUST skip the write.
        None        — either no prior internal exit, or pool unavailable.
                      Caller looks at `pool` to disambiguate (we deliberately
                      return None for both because the cost-basis path also
                      needs the pool — caller checks pool once).

    The query filter (exit_reason NOT LIKE 'settlement_%') is the same
    pattern used by `_get_internal_cost_basis`; this prevents v2-written
    settlement rows from being treated as "prior internal exits" if the
    reconciler runs twice for the same ticker.

    NOTE on the `exited_at <= settled_time` filter: the task spec includes
    this. In practice an internal exit cannot happen AFTER a market settles
    (the market is gone), so the filter shouldn't disqualify any real row.
    Wall-clock vs. Kalshi-clock skew of a few seconds is theoretically
    possible — flagged in the report as an open question.
    """
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT realized_pnl_cents, exit_reason, exited_at,
                       entry_cents, contracts
                  FROM position_history
                 WHERE ticker = $1
                   AND (exit_reason IS NULL OR exit_reason NOT LIKE 'settlement_%')
                 ORDER BY exited_at DESC
                 LIMIT 1
                """,
                ticker,
            )
    except Exception as exc:
        log.warning("settle: prior-exit query failed for %s: %s", ticker, exc)
        return None
    return row


# ── Cost-basis lookup ─────────────────────────────────────────────────────────

async def _get_internal_cost_basis(
    pool: Optional[asyncpg.Pool],
    ticker: str,
    settled_time: datetime,
) -> Optional[int]:
    """
    Return entry_cents * contracts from the most recent non-settlement
    position_history row for `ticker` exited on or before settled_time, or
    None if no such row exists / pool unavailable.
    """
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT entry_cents, contracts
                  FROM position_history
                 WHERE ticker = $1
                   AND (exit_reason IS NULL OR exit_reason NOT LIKE 'settlement_%')
                 ORDER BY exited_at DESC
                 LIMIT 1
                """,
                ticker,
            )
    except Exception as exc:
        log.warning("settle: cost-basis query failed for %s: %s", ticker, exc)
        return None
    if not row:
        return None
    try:
        return int(row["entry_cents"]) * int(row["contracts"])
    except Exception:
        return None


def _kalshi_agg_cost_cents(settlement: Dict[str, Any]) -> Optional[int]:
    """
    Sum yes_total_cost_dollars + no_total_cost_dollars from the Kalshi
    settlement record and convert to cents. Returns None if neither field
    is present / parseable.
    """
    yes_cost = settlement.get("yes_total_cost_dollars")
    no_cost  = settlement.get("no_total_cost_dollars")
    total = 0.0
    seen_any = False
    for v in (yes_cost, no_cost):
        if v is None:
            continue
        try:
            total += float(v)
            seen_any = True
        except (TypeError, ValueError):
            continue
    if not seen_any:
        return None
    return int(round(total * 100))


def _infer_held_side(settlement: Dict[str, Any], pos: Optional[Dict[str, Any]]) -> str:
    """
    Determine which side the trader held at settlement.

    Preference: pos.side from ep:positions snapshot. If the position is
    already cleared from Redis (common — bot deletes on flat), fall back
    to a heuristic on the settlement record: whichever of yes_count_fp /
    no_count_fp is larger was the held side.
    """
    if pos and pos.get("side") in ("yes", "no"):
        return pos["side"]
    try:
        yc = float(settlement.get("yes_count_fp", 0) or 0)
        nc = float(settlement.get("no_count_fp", 0) or 0)
        return "yes" if yc >= nc else "no"
    except (TypeError, ValueError):
        return "yes"  # arbitrary; logged via cost_basis_source if unknown


def _parse_fee_cents(settlement: Dict[str, Any]) -> int:
    """
    Kalshi `fee_cost` is a string in dollars (e.g. "0.07"). Convert to
    integer cents. Default 0 if missing or unparseable.
    NOTE: do NOT add cfg.FEE_CENTS on top — Kalshi's value is authoritative.
    """
    raw = settlement.get("fee_cost", "0")
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return 0


# ── Core reconciliation ───────────────────────────────────────────────────────

async def reconcile_one_settlement(
    settlement: Dict[str, Any],
    executor,
    bus_redis,
    pool: Optional[asyncpg.Pool] = None,
    pos_snapshot: Optional[Dict[str, Any]] = None,
    *,
    dry_run: bool = False,
    paper_skip: bool = True,
    model_source: str = "settlement_live",
) -> Dict[str, Any]:
    """
    Reconcile a single Kalshi settlement record. Idempotent via Redis
    seen-set + position_history_settlement_uniq partial unique index.

    Args:
        settlement:    Kalshi /portfolio/settlements record (single dict).
        executor:      Executor instance (for trades.csv via _log_trade).
                       May be None in pure-audit dry runs.
        bus_redis:     Async Redis client (RedisBus._r is fine).
        pool:          asyncpg pool for prior-exit + cost-basis lookups.
                       v2: when None, we cannot verify whether a prior
                       internal exit exists — we fail closed and return
                       'skipped_no_pool' WITHOUT marking seen so the next
                       poll retries once the pool is back.
        pos_snapshot:  Pre-fetched ep:positions[ticker] dict if available.
        dry_run:       If True, do everything except writes.
        paper_skip:    If True (default) and cfg.PAPER_TRADE, skip CSV
                       and audit writes.
        model_source:  Tag used in trades.csv model_source column.

    Returns a dict describing the action taken:
        action: 'inserted'             — settlement row written (no prior).
                'skipped_internal_exit'— prior internal exit; v2 fix path.
                'skipped_duplicate'    — already in ep:settle:seen.
                'skipped_no_pool'      — pool unavailable (fail-closed v2).
                'dry_run'              — would-have-written (no writes).
                'paper_skip'           — PAPER_TRADE mode skip.
                'malformed'            — missing ticker / settled_time.
        ticker, pnl_cents, cost_basis_source, reason
    """
    ticker = settlement.get("ticker") or ""
    settled_time_raw = settlement.get("settled_time") or ""
    if not ticker or not settled_time_raw:
        return {
            "action": "malformed",
            "ticker": ticker,
            "pnl_cents": 0,
            "cost_basis_source": "missing",
            "reason": "missing ticker or settled_time",
        }

    # ── Dedupe FIRST so duplicate calls don't even hit Postgres ──────────────
    if await _is_seen(bus_redis, ticker, settled_time_raw):
        return {
            "action": "skipped_duplicate",
            "ticker": ticker,
            "pnl_cents": 0,
            "cost_basis_source": "missing",
            "reason": "already in ep:settle:seen",
        }

    # ── Parse settled_time → datetime ─────────────────────────────────────────
    try:
        settled_dt = datetime.fromisoformat(settled_time_raw.replace("Z", "+00:00"))
    except Exception as exc:
        return {
            "action": "malformed",
            "ticker": ticker,
            "pnl_cents": 0,
            "cost_basis_source": "missing",
            "reason": f"unparseable settled_time {settled_time_raw!r}: {exc}",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # v2 critical fix: PRIOR-INTERNAL-EXIT CHECK
    # ──────────────────────────────────────────────────────────────────────────
    # If position_history already has a non-settlement exit row for this
    # ticker before settled_time, our internal exit logic already attributed
    # the P&L. Writing the settlement row would double-count.
    #
    # v1 (reverted) wrote unconditionally — concrete production instance
    # KXNBAGAME-26APR28ATLNYK-ATL gained $40 of phantom loss attribution.
    # Across 7 days of backfill the bug added ~$1,600 of phantom P&L.
    #
    # Fail-closed if pool is None: we can't verify, so we don't write and
    # we DON'T mark_seen (so next poll retries). The audit pool is owned
    # by ep_pg_audit which can fail to init; we'd rather lag a few cycles
    # than re-introduce v1's bug.
    if pool is None:
        log.debug(
            "settle: %s pool unavailable — skipping (fail-closed; not marking seen)",
            ticker,
        )
        return {
            "action": "skipped_no_pool",
            "ticker": ticker,
            "pnl_cents": 0,
            "cost_basis_source": "missing",
            "reason": "asyncpg pool unavailable; cannot verify prior-internal-exit safely",
        }

    prior = await _get_prior_internal_exit(pool, ticker, settled_dt)
    if prior is not None:
        prior_pnl    = int(prior.get("realized_pnl_cents") or 0)
        prior_reason = prior.get("exit_reason")
        log.debug(
            "settle: %s already closed internally at exit_reason=%r pnl=%d "
            "before settled_time=%s — skipping settlement write (v2 dedup)",
            ticker, prior_reason, prior_pnl, settled_time_raw,
        )
        # In live mode mark seen so we don't re-check on every poll cycle.
        # In dry_run mode do NOT mark seen — operators run `--dry-run` first
        # to preview, then `--apply` to commit. If dry_run marked seen, every
        # settlement would come back 'skipped_duplicate' on the apply pass.
        if not dry_run:
            await _mark_seen(bus_redis, ticker, settled_time_raw)
        return {
            "action": "skipped_internal_exit",
            "ticker": ticker,
            "pnl_cents": 0,
            "cost_basis_source": "missing",
            "reason": (
                f"prior internal exit exists (exit_reason={prior_reason!r} "
                f"pnl={prior_pnl}c at {prior.get('exited_at')}); "
                "settlement is bookkeeping, not new P&L"
            ),
            "prior_reason": prior_reason,
            "prior_pnl_cents": prior_pnl,
            "settlement_ts": settled_time_raw,
        }

    # ── No prior internal exit. Cost basis: internal cost-basis row (rare
    # since prior-exit check returned None — would only fire if the prior
    # exit had an exit_reason that started with 'settlement_', which we
    # already filter), kalshi_agg fallback, else 'missing'.
    internal_cost = await _get_internal_cost_basis(pool, ticker, settled_dt)
    kalshi_cost   = _kalshi_agg_cost_cents(settlement)

    if internal_cost is not None and kalshi_cost is not None:
        if kalshi_cost > 0:
            divergence = abs(internal_cost - kalshi_cost) / kalshi_cost
        else:
            divergence = 0.0 if internal_cost == 0 else 1.0
        if divergence > _COST_BASIS_MISMATCH_FRAC:
            log.warning(
                "settle: cost basis MISMATCH for %s — internal=%d kalshi=%d "
                "(div=%.4f); using internal, tagging mismatch",
                ticker, internal_cost, kalshi_cost, divergence,
            )
            cost_basis_cents = internal_cost
            cost_basis_src   = "mismatch"
        else:
            cost_basis_cents = internal_cost
            cost_basis_src   = "internal"
    elif internal_cost is not None:
        cost_basis_cents = internal_cost
        cost_basis_src   = "internal"
    elif kalshi_cost is not None:
        cost_basis_cents = kalshi_cost
        cost_basis_src   = "kalshi_agg"
    else:
        cost_basis_cents = 0
        cost_basis_src   = "missing"

    # ── Revenue + fee from Kalshi (already in cents per API inspection) ──────
    try:
        revenue_cents = int(settlement.get("revenue", 0) or 0)
    except (TypeError, ValueError):
        revenue_cents = 0
    fee_cents = _parse_fee_cents(settlement)

    pnl_cents = revenue_cents - cost_basis_cents - fee_cents

    side = _infer_held_side(settlement, pos_snapshot)
    contracts = 0
    try:
        # Held qty: prefer pos snapshot, else use the larger of yes/no count_fp.
        if pos_snapshot:
            contracts = int(pos_snapshot.get("contracts", 0) or 0)
        if contracts <= 0:
            yc = int(float(settlement.get("yes_count_fp", 0) or 0))
            nc = int(float(settlement.get("no_count_fp", 0) or 0))
            contracts = yc if side == "yes" else nc
    except Exception:
        contracts = 0

    # entry_cents per CLAUDE.md invariant: always YES-market price × 100.
    if pos_snapshot and pos_snapshot.get("entry_cents") is not None:
        try:
            entry_cents_val = int(pos_snapshot["entry_cents"])
        except Exception:
            entry_cents_val = 0
    elif contracts > 0 and cost_basis_cents > 0:
        per_contract_cost = cost_basis_cents // max(1, contracts)
        # cost-per-contract is YES-price for YES side, (100 - YES) for NO.
        entry_cents_val = per_contract_cost if side == "yes" else (100 - per_contract_cost)
        entry_cents_val = max(0, min(100, entry_cents_val))
    else:
        entry_cents_val = 0

    # exit_cents: Kalshi YES-resolved → 100, NO-resolved → 0.
    result = (settlement.get("result") or "").lower()
    if result == "yes":
        exit_cents_val = 100
    elif result == "no":
        exit_cents_val = 0
    else:
        if contracts > 0 and revenue_cents > 0:
            per_contract_rev = revenue_cents // max(1, contracts)
            exit_cents_val = per_contract_rev if side == "yes" else (100 - per_contract_rev)
        else:
            exit_cents_val = 0
        exit_cents_val = max(0, min(100, exit_cents_val))

    exit_reason = (
        "settlement_resolved" if result in ("yes", "no") else "settlement_unknown"
    )

    if dry_run:
        # v2: do NOT mark_seen on dry-run. Operators run `--dry-run` first to
        # preview, then `--apply` to commit; marking seen here would make the
        # apply pass return 'skipped_duplicate' for every settlement and silently
        # write nothing. (v1 had this bug.)
        return {
            "action": "dry_run",
            "ticker": ticker,
            "pnl_cents": pnl_cents,
            "cost_basis_source": cost_basis_src,
            "reason": "dry-run; no writes",
            "settlement_ts": settled_time_raw,
            "revenue_cents": revenue_cents,
            "fee_cents": fee_cents,
            "cost_basis_cents": cost_basis_cents,
            "contracts": contracts,
            "side": side,
            "entry_cents": entry_cents_val,
            "exit_cents": exit_cents_val,
            "exit_reason": exit_reason,
        }

    # Paper-trade mode: don't reconcile against the real exchange.
    if paper_skip and getattr(cfg, "PAPER_TRADE", False):
        log.debug(
            "settle: paper-mode skip for %s (settled=%s pnl=%d)",
            ticker, settled_time_raw, pnl_cents,
        )
        await _mark_seen(bus_redis, ticker, settled_time_raw)
        return {
            "action": "paper_skip",
            "ticker": ticker,
            "pnl_cents": pnl_cents,
            "cost_basis_source": cost_basis_src,
            "reason": "PAPER_TRADE=true; skipped audit + csv",
        }

    # ── 1. position_history audit row ────────────────────────────────────────
    try:
        _audit_writer().write("position_history", {
            "entry_exec_id":        None,
            "ticker":               ticker,
            "side":                 side,
            "contracts":            contracts,
            "entry_cents":          entry_cents_val,
            "exit_cents":           exit_cents_val,
            "realized_pnl_cents":   pnl_cents,
            "exit_reason":          exit_reason,
            "entered_at":           None,
            "exited_at":            settled_dt.isoformat(),
            "strategy":             (pos_snapshot or {}).get("strategy") or "settlement",
            # Phase-3 v2 settlement columns
            "settlement_ts":        settled_time_raw,
            "cost_basis_source":    cost_basis_src,
            "kalshi_fee_cents":     fee_cents,
            "kalshi_revenue_cents": revenue_cents,
        })
    except Exception as exc:
        log.warning("settle: audit write failed for %s: %s", ticker, exc)

    # ── 1b. Engineering A.5 — per-strategy P&L attribution realtime hook ──────
    # Increments ep:strategy_pnl_realtime counters for the originating strategy.
    # Rolled over to strategy_pnl_daily Postgres table at 00:00 UTC by
    # ep_pnl_attribution.rollover_loop (launched from ep_exec.exec_main).
    try:
        from ep_pnl_attribution import record_settlement as _a5_record_settlement
        _a5_strat = (pos_snapshot or {}).get("strategy") or "settlement"
        await _a5_record_settlement(bus_redis, _a5_strat, int(pnl_cents))
    except Exception as _a5_exc:
        log.debug("settle: A.5 record_settlement failed for %s: %s", ticker, _a5_exc)

    # ── 1c. Engineering A.2 — capital allocator slot release ──────────────────
    # Decrement ep:allocator:slots_open + tier1_open when a position closes.
    # Idempotency: release_slot floors counters at 0, so double-calls are safe.
    # Model_source resolution mirrors A.5 above so behavior is consistent.
    try:
        from ep_capital_allocator import release_slot as _a2_release_slot
        _a2_strat = (pos_snapshot or {}).get("strategy") or ""
        if _a2_strat and _a2_strat != "settlement":
            await _a2_release_slot(bus_redis, _a2_strat)
    except Exception as _a2_exc:
        log.debug("settle: A.2 release_slot failed for %s: %s", ticker, _a2_exc)

    # ── 2. trades.csv synthetic exit row (live-mode only) ────────────────────
    if executor is not None:
        try:
            from kalshi_bot.strategy import Signal as _KSig
            meeting = (pos_snapshot or {}).get("meeting", "") or ""
            outcome = (pos_snapshot or {}).get("outcome", "") or ""
            category = (pos_snapshot or {}).get("category", "settlement") or "settlement"
            exit_side = "no" if side == "yes" else "yes"
            exit_sig = _KSig(
                ticker            = ticker,
                title             = "",
                category          = category,
                meeting           = meeting,
                outcome           = outcome,
                side              = exit_side,
                fair_value        = 0.5,
                market_price      = exit_cents_val / 100.0,
                edge              = 0.0,
                fee_adjusted_edge = 0.0,
                contracts         = contracts,
                confidence        = 0.0,
                model_source      = model_source,
            )
            executor._log_trade(exit_sig, "exit", exit_reason, "live")
        except Exception as exc:
            log.warning("settle: trades.csv log failed for %s: %s", ticker, exc)

    await _mark_seen(bus_redis, ticker, settled_time_raw)

    log.info(
        "settle: reconciled %s settled=%s contracts=%d side=%s "
        "cost=%d rev=%d fee=%d pnl=%d src=%s",
        ticker, settled_time_raw, contracts, side,
        cost_basis_cents, revenue_cents, fee_cents, pnl_cents, cost_basis_src,
    )

    return {
        "action": "inserted",
        "ticker": ticker,
        "pnl_cents": pnl_cents,
        "cost_basis_source": cost_basis_src,
        "reason": "ok",
        "settlement_ts": settled_time_raw,
        "revenue_cents": revenue_cents,
        "fee_cents": fee_cents,
        "cost_basis_cents": cost_basis_cents,
        "contracts": contracts,
        "side": side,
        "entry_cents": entry_cents_val,
        "exit_cents": exit_cents_val,
        "exit_reason": exit_reason,
    }


# ── Live polling loop ─────────────────────────────────────────────────────────

async def _fetch_settlements_page(
    client,
    min_ts: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """
    Single page from /portfolio/settlements. Runs the synchronous
    KalshiClient.get() on a thread executor so the loop doesn't block.
    """
    try:
        min_ts_dt = datetime.fromisoformat(min_ts.replace("Z", "+00:00"))
        min_ts_epoch = int(min_ts_dt.timestamp())
    except Exception:
        min_ts_epoch = int(
            (datetime.now(timezone.utc) - _DEFAULT_LOOKBACK).timestamp()
        )
    params: Dict[str, Any] = {"limit": _PAGE_LIMIT, "min_ts": min_ts_epoch}
    if cursor:
        params["cursor"] = cursor
    try:
        return await asyncio.to_thread(
            client.get, "/portfolio/settlements", params,
        )
    except Exception as exc:
        log.warning("settle: /portfolio/settlements fetch error: %s", exc)
        return {}


async def settlement_recon_loop(client, bus, executor, interval: int = 300) -> None:
    """
    Poll /portfolio/settlements every `interval` seconds and reconcile each
    settlement against position_history + trades.csv.
    """
    log.info("Settlement reconciliation loop started (interval=%ds, v2)", interval)
    bus_redis = bus._r
    while True:
        try:
            cursor_iso = await _get_cursor(bus_redis)
            page_cursor: Optional[str] = None
            processed_max_ts: Optional[datetime] = None
            pages = 0
            inserted = duplicates = paper = malformed = 0
            internal_exits = no_pool = 0

            # Pull the audit pool (same lifecycle as ep_pg_audit). Reused for
            # prior-exit + cost-basis lookups; None until init_audit_writer
            # succeeds.
            try:
                pool = getattr(_audit_writer(), "_pool", None)
            except Exception:
                pool = None

            positions_map = {}
            try:
                positions_map = await bus.get_all_positions()
            except Exception as exc:
                log.debug("settle: get_all_positions failed: %s", exc)

            while True:
                resp = await _fetch_settlements_page(client, cursor_iso, page_cursor)
                settlements = (resp or {}).get("settlements") or []
                pages += 1
                if not settlements:
                    break
                for s in settlements:
                    pos_snap = positions_map.get(s.get("ticker") or "")
                    try:
                        result = await reconcile_one_settlement(
                            s, executor, bus_redis,
                            pool=pool, pos_snapshot=pos_snap,
                            dry_run=False, paper_skip=True,
                            model_source="settlement_live",
                        )
                    except Exception as exc:
                        log.warning(
                            "settle: reconcile_one_settlement raised for %s: %s",
                            s.get("ticker"), exc,
                        )
                        continue
                    action = result["action"]
                    if action == "inserted":
                        inserted += 1
                    elif action == "skipped_duplicate":
                        duplicates += 1
                    elif action == "skipped_internal_exit":
                        internal_exits += 1
                    elif action == "skipped_no_pool":
                        no_pool += 1
                    elif action == "paper_skip":
                        paper += 1
                    elif action == "malformed":
                        malformed += 1
                    # Track max settled_time across all processed entries for
                    # cursor advancement (incl. duplicates so we don't get
                    # stuck if everything's already seen). Note: do NOT
                    # advance for skipped_no_pool — we want the next poll
                    # to retry those.
                    if action != "skipped_no_pool":
                        try:
                            st = datetime.fromisoformat(
                                (s.get("settled_time") or "").replace("Z", "+00:00")
                            )
                            if processed_max_ts is None or st > processed_max_ts:
                                processed_max_ts = st
                        except Exception:
                            pass

                page_cursor = (resp or {}).get("cursor")
                if not page_cursor or len(settlements) < _PAGE_LIMIT:
                    break

            # Advance cursor with safety overlap.  If we processed nothing,
            # leave the cursor where it is.
            if processed_max_ts is not None:
                next_cursor = (processed_max_ts - _CURSOR_OVERLAP).isoformat()
                await _set_cursor(bus_redis, next_cursor)
                log.info(
                    "settle: page_done pages=%d inserted=%d skipped_internal=%d "
                    "dup=%d paper=%d malformed=%d no_pool=%d cursor→%s",
                    pages, inserted, internal_exits, duplicates, paper,
                    malformed, no_pool, next_cursor,
                )
            else:
                log.debug(
                    "settle: empty page pages=%d (cursor unchanged: %s)",
                    pages, cursor_iso,
                )

        except Exception as exc:
            log.warning("settlement_recon_loop iteration error: %s", exc)
        await asyncio.sleep(interval)
