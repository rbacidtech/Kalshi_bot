"""tests/test_capital_allocator.py — Engineering A.2 allocator unit tests."""

from __future__ import annotations

import asyncio

from ep_capital_allocator import (
    _is_tier1,
    _DEFAULTS,
    allocate,
    reserve_slot,
    release_slot,
    get_state,
)


class _FakeRedis:
    def __init__(self, state: dict[str, str] | None = None, config: dict | None = None):
        self._kv: dict[str, str] = state or {}
        self._config = config or {}
    async def get(self, k):
        v = self._kv.get(k)
        return v.encode() if isinstance(v, str) else v
    async def set(self, k, v):
        self._kv[k] = str(v)
    async def incrby(self, k, n):
        cur = int(self._kv.get(k, 0) or 0)
        self._kv[k] = str(cur + n)
    async def decrby(self, k, n):
        cur = int(self._kv.get(k, 0) or 0)
        self._kv[k] = str(cur - n)
    async def hgetall(self, k):
        if k == "ep:config":
            return {kk.encode(): str(vv).encode() for kk, vv in self._config.items()}
        return {}


def test_is_tier1_for_known_arbs():
    assert _is_tier1("h2h_sum_to_1_arb")
    assert _is_tier1("spread_monot")
    assert _is_tier1("fomc_butterfly_arb")
    # Tier 2
    assert not _is_tier1("kxmve_nfl_singlegame_longshot")
    assert not _is_tier1("weather_longshot")
    assert not _is_tier1("fedwatch+tbill_term")


def test_allocate_happy_path():
    r = _FakeRedis()
    # Tier 1 arb, 30 × 48¢ = $14.40 (above $10 hard floor, below $50 per-slot)
    res = asyncio.run(allocate(r, "h2h_sum_to_1_arb", contracts=30, price_cents=48,
                               balance_cents=150_000))
    assert res["accepted"]
    assert res["tier1"]
    assert res["contracts_allowed"] == 30


def test_allocate_rejects_tiny_position():
    """Below the $10 hard minimum — reject (not downsize)."""
    r = _FakeRedis()
    # 10 × 48¢ = $4.80 — below $10 floor
    res = asyncio.run(allocate(r, "h2h_sum_to_1_arb", contracts=10, price_cents=48,
                               balance_cents=150_000))
    assert not res["accepted"]
    assert res["reason"] == "BELOW_MIN"


def test_allocate_downsizes_at_per_slot_cap():
    r = _FakeRedis()
    # 200 × 48¢ = $96; per_slot cap $50 → downsize
    res = asyncio.run(allocate(r, "h2h_sum_to_1_arb", contracts=200, price_cents=48,
                               balance_cents=150_000))
    assert res["accepted"]
    # 5000 / 48 = 104 contracts
    assert res["contracts_allowed"] == 104
    assert res["reason"] == "DOWNSIZED_PER_SLOT"


def test_allocate_rejects_when_slots_full():
    r = _FakeRedis(state={"ep:allocator:slots_open": "30",
                          "ep:allocator:tier1_open": "15"})
    res = asyncio.run(allocate(r, "h2h_sum_to_1_arb", contracts=30, price_cents=50,
                               balance_cents=150_000))
    assert not res["accepted"]
    assert res["reason"] == "NO_SLOTS"


def test_allocate_tier2_blocked_when_at_tier2_cap():
    # 15 Tier-1 slots reserved; Tier-2 cap = 30 - 15 = 15.
    # Pre-state: 20 open, 5 of which Tier 1 → tier2_open = 15 (at cap).
    r = _FakeRedis(state={"ep:allocator:slots_open": "20",
                          "ep:allocator:tier1_open": "5"})
    res = asyncio.run(allocate(r, "weather_longshot", contracts=100, price_cents=20,
                               balance_cents=150_000))
    assert not res["accepted"]
    assert res["reason"] == "TIER1_RESERVED"


def test_allocate_tier1_can_use_tier2_slots():
    """Tier 1 can occupy any slot — no Tier-2-reservation constraint."""
    r = _FakeRedis(state={"ep:allocator:slots_open": "20",
                          "ep:allocator:tier1_open": "5"})
    res = asyncio.run(allocate(r, "h2h_sum_to_1_arb", contracts=30, price_cents=50,
                               balance_cents=150_000))
    assert res["accepted"]
    assert res["tier1"]


def test_allocate_rejects_below_reserve_floor():
    r = _FakeRedis()
    # Balance = $50; reserve floor $100 → free = -$50 → reject
    res = asyncio.run(allocate(r, "h2h_sum_to_1_arb", contracts=30, price_cents=48,
                               balance_cents=5_000))
    assert not res["accepted"]
    assert res["reason"] == "RESERVE_FLOOR"


def test_allocate_rejects_when_daily_cap_exhausted():
    from datetime import datetime, timezone
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    # last_reset_date must match today, else _read_state auto-rolls to 0
    r = _FakeRedis(state={
        "ep:allocator:today_total_cents": "120000",
        "ep:allocator:last_reset_date":   today_str,
    })
    res = asyncio.run(allocate(r, "h2h_sum_to_1_arb", contracts=30, price_cents=50,
                               balance_cents=150_000))
    assert not res["accepted"]
    assert res["reason"] == "DAILY_CAP_TOTAL"


def test_reserve_and_release_increment_counters():
    r = _FakeRedis()
    asyncio.run(reserve_slot(r, "h2h_sum_to_1_arb", cost_cents=4_800))
    state = asyncio.run(get_state(r))
    assert state["slots_open"] == 1
    assert state["tier1_open"] == 1
    assert state["today_total_cents"] == 4_800
    asyncio.run(release_slot(r, "h2h_sum_to_1_arb"))
    state2 = asyncio.run(get_state(r))
    assert state2["slots_open"] == 0
    assert state2["tier1_open"] == 0


def test_tier2_reserve_does_not_inc_tier1_open():
    r = _FakeRedis()
    asyncio.run(reserve_slot(r, "weather_longshot", cost_cents=2_000))
    state = asyncio.run(get_state(r))
    assert state["slots_open"] == 1
    assert state["tier1_open"] == 0


def test_defaults_match_engineering_spec():
    assert _DEFAULTS["total_slots"] == 30
    assert _DEFAULTS["tier1_reserved_slots"] == 15
    assert _DEFAULTS["per_slot_cents"] == 5_000          # $50
    assert _DEFAULTS["reserve_floor_cents"] == 10_000    # $100
    assert _DEFAULTS["total_daily_cap_cents"] == 120_000 # $1,200
