"""Capital allocator — Engineering A.2.

Manages the $1,500 capital across ~30 concurrent slots with tier-1 reservation
(15 slots) for structural arbs. Enforces:

  - $50/slot baseline (sixteenth-Kelly; conservative given noisy edge estimates)
  - 30-slot total cap (Engineering A.2 §3 default)
  - 15-slot Tier-1 reservation (structural arbs get first claim)
  - 10% reserve floor — never deploy below $100 of free capital
  - Per-day allocations capped at 80% of starting capital ($1,200)

Works in concert with `ep_correlation_caps.check_candidate()` which handles
the per-event / per-prefix / per-underlying / total daily aggregation. The
allocator is more concerned with **slot accounting** + tier prioritization.

State (Redis):
  - `ep:allocator:slots_open` — int, count of currently-open allocated slots
  - `ep:allocator:tier1_open` — int, of which are Tier 1 strategies
  - `ep:allocator:today_total_cents` — int, cumulative new-position cost today
  - `ep:allocator:last_reset_date` — YYYYMMDD, daily counter rollover marker

API:
  - allocate(strategy, side, contracts, price_cents, bus_redis, balance_cents)
    → {accepted: bool, contracts_allowed, reason, slot_acquired}.
    Pre-trade gate; called BEFORE order placement.
  - release(strategy, bus_redis) — called on position close. Decrements
    slots_open and tier1_open as appropriate.
  - reset_daily(bus_redis) — UTC midnight; zeros today_total_cents.

Tier-1 classification: strategies whose StrategySpec.tier == "1" in
strategies/specs.py. Determined by model_source name pattern (current
mapping: anything matching the 6 Tier-1 keys in VERDICT_STRATEGIES).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# Engineering A.2 §3 defaults. Configurable via ep:config overrides.
_DEFAULTS = {
    "total_slots":              30,
    "tier1_reserved_slots":     15,
    "per_slot_cents":           5_000,        # $50
    "reserve_floor_cents":      10_000,       # $100
    "total_daily_cap_cents":    120_000,      # $1,200 = 80% of $1,500
    "min_position_cents":       1_000,        # $10 — reject below
}


_TIER1_STRATEGIES = {
    "h2h_sum_to_1_arb",         # H2H 2-outcome sum-to-1 (verdict §3.1)
    "spread_monot",
    "total_monot",
    "nfl_prop_yardage_monot",
    "crypto_threshold_monot",
    "a2_cross_market_arb",
    "fomc_butterfly_arb",       # legacy bot — currently disabled but tier 1 by structure
    "fomc_arb",
    "econ_monotonicity_arb",
}


def _is_tier1(model_source: str) -> bool:
    if not model_source:
        return False
    ms = model_source.lower()
    if ms in _TIER1_STRATEGIES:
        return True
    # Pattern-matching fallback for arb-suffixed strategies
    return ms.endswith("_arb") and "monot" not in ms or "monot" in ms


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def _read_overrides(bus_redis: Any) -> dict[str, int]:
    """Read ep:config overrides for allocator caps."""
    out = dict(_DEFAULTS)
    if bus_redis is None:
        return out
    try:
        raw = await bus_redis.hgetall("ep:config")
    except Exception:
        return out
    for k, v in (raw or {}).items():
        ks = k.decode() if isinstance(k, bytes) else k
        if not ks.startswith("override_allocator_"):
            continue
        field = ks[len("override_allocator_"):]
        if field in out:
            try:
                vs = v.decode() if isinstance(v, bytes) else v
                out[field] = int(vs)
            except (TypeError, ValueError):
                continue
    return out


async def _read_state(bus_redis: Any) -> dict[str, int]:
    """Read current allocator state. Auto-resets today's counter on date rollover."""
    state = {
        "slots_open":         0,
        "tier1_open":         0,
        "today_total_cents":  0,
        "last_reset_date":    _today_utc(),
    }
    if bus_redis is None:
        return state
    today = _today_utc()
    try:
        for k in ("slots_open", "tier1_open", "today_total_cents"):
            raw = await bus_redis.get(f"ep:allocator:{k}")
            if raw is not None:
                try:
                    raw_s = raw.decode() if isinstance(raw, bytes) else raw
                    state[k] = int(raw_s)
                except (TypeError, ValueError):
                    pass
        last_date_raw = await bus_redis.get("ep:allocator:last_reset_date")
        last_date = last_date_raw.decode() if isinstance(last_date_raw, bytes) else last_date_raw
        if last_date != today:
            # UTC rollover — zero today_total_cents (slots stay; they're open positions)
            state["today_total_cents"] = 0
            await bus_redis.set("ep:allocator:today_total_cents", 0)
            await bus_redis.set("ep:allocator:last_reset_date", today)
    except Exception as exc:
        log.debug("_read_state failed: %s", exc)
    return state


async def allocate(
    bus_redis: Any,
    strategy: str,
    contracts: int,
    price_cents: int,
    balance_cents: int,
) -> dict[str, Any]:
    """Pre-trade slot + capital reservation.

    Returns dict with:
      - accepted        bool
      - contracts_allowed  int (may be < contracts if downsized to fit per-slot cap)
      - reason          None or one of: NO_SLOTS / RESERVE_FLOOR / TIER1_RESERVED /
                                       DAILY_CAP_TOTAL / BELOW_MIN
      - tier1           bool — was this allocated as Tier 1
      - notes           str — operator-readable
    """
    if contracts <= 0 or price_cents <= 0:
        return {"accepted": False, "contracts_allowed": 0,
                "reason": "INVALID_INPUT", "tier1": False, "notes": ""}

    caps = await _read_overrides(bus_redis)
    state = await _read_state(bus_redis)
    is_t1 = _is_tier1(strategy)

    # Slot accounting
    slots_open = state["slots_open"]
    tier1_open = state["tier1_open"]
    total_slots = caps["total_slots"]
    tier1_reserved = caps["tier1_reserved_slots"]

    if slots_open >= total_slots:
        return {"accepted": False, "contracts_allowed": 0,
                "reason": "NO_SLOTS", "tier1": False,
                "notes": f"slots_open={slots_open}/{total_slots}"}

    # Tier 2 strategies cannot claim the Tier-1 reservation:
    # tier-2 available = total_slots - tier1_reserved + (tier1_reserved - tier1_open)?
    # Simpler: tier 2 can occupy at most (total_slots - tier1_reserved) slots.
    if not is_t1:
        tier2_open = slots_open - tier1_open
        tier2_cap = total_slots - tier1_reserved
        if tier2_open >= tier2_cap:
            return {"accepted": False, "contracts_allowed": 0,
                    "reason": "TIER1_RESERVED", "tier1": False,
                    "notes": f"tier2_open={tier2_open}/{tier2_cap} (tier-1 slots reserved)"}

    # Capital math
    requested_cost = contracts * price_cents
    free_cents = balance_cents - caps["reserve_floor_cents"]
    if free_cents < caps["min_position_cents"]:
        return {"accepted": False, "contracts_allowed": 0,
                "reason": "RESERVE_FLOOR", "tier1": is_t1,
                "notes": f"balance={balance_cents}¢ reserve_floor={caps['reserve_floor_cents']}¢"}

    daily_remaining = caps["total_daily_cap_cents"] - state["today_total_cents"]
    if daily_remaining < caps["min_position_cents"]:
        return {"accepted": False, "contracts_allowed": 0,
                "reason": "DAILY_CAP_TOTAL", "tier1": is_t1,
                "notes": f"today_total={state['today_total_cents']}¢ cap={caps['total_daily_cap_cents']}¢"}

    # Per-slot cap binds: at most per_slot_cents per signal
    binding = min(caps["per_slot_cents"], free_cents, daily_remaining)
    allowed_cost = min(requested_cost, binding)
    if allowed_cost < caps["min_position_cents"]:
        return {"accepted": False, "contracts_allowed": 0,
                "reason": "BELOW_MIN", "tier1": is_t1,
                "notes": f"allowed_cost={allowed_cost}¢ min={caps['min_position_cents']}¢"}

    allowed_contracts = max(1, int(allowed_cost // price_cents))
    return {
        "accepted":           True,
        "contracts_allowed":  allowed_contracts,
        "reason":             None if allowed_contracts == contracts else "DOWNSIZED_PER_SLOT",
        "tier1":              is_t1,
        "notes":              (f"allocated tier{'1' if is_t1 else '2'} "
                               f"{allowed_contracts}×{price_cents}¢ "
                               f"slots={slots_open + 1}/{total_slots}"),
    }


async def reserve_slot(bus_redis: Any, strategy: str, cost_cents: int) -> None:
    """Commit a slot reservation after `allocate` returned accepted=True and
    the order was actually placed. Increments slots_open + today_total_cents.

    Sets last_reset_date too so a subsequent _read_state doesn't trigger
    a spurious rollover that would zero out today_total_cents.
    """
    if bus_redis is None:
        return
    is_t1 = _is_tier1(strategy)
    try:
        await bus_redis.incrby("ep:allocator:slots_open", 1)
        if is_t1:
            await bus_redis.incrby("ep:allocator:tier1_open", 1)
        await bus_redis.incrby("ep:allocator:today_total_cents", int(cost_cents))
        # Ensure the daily-rollover marker is current so _read_state doesn't
        # auto-zero today_total_cents on the next read.
        await bus_redis.set("ep:allocator:last_reset_date", _today_utc())
    except Exception as exc:
        log.warning("reserve_slot(%s) failed: %s", strategy, exc)


async def release_slot(bus_redis: Any, strategy: str) -> None:
    """Decrement slot counters on position close.  Idempotent floor at 0."""
    if bus_redis is None:
        return
    is_t1 = _is_tier1(strategy)
    try:
        cur = await bus_redis.get("ep:allocator:slots_open")
        cur_int = int(cur) if cur else 0
        if cur_int > 0:
            await bus_redis.decrby("ep:allocator:slots_open", 1)
        if is_t1:
            cur_t1 = await bus_redis.get("ep:allocator:tier1_open")
            cur_t1_int = int(cur_t1) if cur_t1 else 0
            if cur_t1_int > 0:
                await bus_redis.decrby("ep:allocator:tier1_open", 1)
    except Exception as exc:
        log.warning("release_slot(%s) failed: %s", strategy, exc)


async def get_state(bus_redis: Any) -> dict[str, Any]:
    """Operator helper — full allocator state snapshot."""
    state = await _read_state(bus_redis)
    caps = await _read_overrides(bus_redis)
    return {**state, **caps,
            "tier2_open": state["slots_open"] - state["tier1_open"],
            "tier2_cap":  caps["total_slots"] - caps["tier1_reserved_slots"]}
