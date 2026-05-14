"""Hot-path helpers for H2H sum-to-1 arb — Engineering A.3.

Bare-bones primitives used by the H2H detection loop when latency matters
(WebSocket trade event → POST order in <100ms p95). The full scanner in
`kalshi_bot/strategy.py:scan_h2h_sum_to_1_arb` is the COLD-PATH variant
used for batch evaluation of pre-fetched market lists. This module is
the FAST variant for per-event evaluation off the WebSocket stream.

BUDGET_MS_P95 = 5.0 — local decision logic alone (not including I/O).

Pre-computed fee lookup table avoids per-call float math:
  fee_cents_at(p_int)  where p_int = round(yes_price * 100)
"""

from __future__ import annotations

from typing import Optional


BUDGET_MS_P95 = 5.0   # Engineering A.3 — decision logic only, not POST round-trip


# Pre-computed Kalshi taker fee in cents per contract.
# Formula: ceil(0.07 × P × (1-P) × 100) / 100   (P in dollars)
# We pre-compute for p ∈ [1, 99] cents (Kalshi's valid range) to skip the
# multiply + ceil in the hot loop.
#
# fee_cents_table[p_int] = fee in dollars (as float, hundredths of cent).
# Multiply by 100 to get cents-as-int, or by 10000 to get sub-cents.
import math as _m

_FEE_TABLE: list[float] = [0.0] * 100   # index 0..99
for _p in range(1, 100):
    _p_dollars = _p / 100.0
    _raw_fee = 0.07 * _p_dollars * (1.0 - _p_dollars)   # in dollars
    _FEE_TABLE[_p] = _m.ceil(_raw_fee * 100) / 100.0    # round up to nearest cent


def fee_cents_at(price_cents: int) -> float:
    """O(1) lookup. price_cents ∈ [1, 99]; returns fee in dollars (0.0 - 0.0175)."""
    if 1 <= price_cents <= 99:
        return _FEE_TABLE[price_cents]
    return 0.0


def detect_arb(
    yes_ask_a_cents: int,
    yes_ask_b_cents: int,
    threshold_cents: int = 98,
) -> Optional[tuple[int, int, int, int]]:
    """Hot-path arb detection — minimal branches, integer-only math.

    Inputs are cents (ints), not dollar floats. Returns
    (sum_cents, gross_cents, net_cents, contracts) when arb opportunity
    exists, else None.

    threshold_cents default 98 corresponds to verdict §3.1's "sum < 0.98".
    """
    if yes_ask_a_cents < 1 or yes_ask_a_cents > 99:
        return None
    if yes_ask_b_cents < 1 or yes_ask_b_cents > 99:
        return None
    total = yes_ask_a_cents + yes_ask_b_cents
    if total >= threshold_cents:
        return None
    gross = 100 - total
    # Fee lookup; fees are already in dollars, convert to sub-cents
    fee_a = _FEE_TABLE[yes_ask_a_cents]
    fee_b = _FEE_TABLE[yes_ask_b_cents]
    fees_cents = int(round((fee_a + fee_b) * 100))
    net = gross - fees_cents
    # Size scaling — gross cents × 50/100 = gross/2
    contracts = gross // 2 if gross > 0 else 1
    if contracts < 1:
        contracts = 1
    return (total, gross, net, contracts)
