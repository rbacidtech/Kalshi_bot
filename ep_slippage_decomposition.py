"""Slippage 4-component decomposition — Engineering B.2.

Phase 1.4 S.1.1 shipped only the TOTAL `slippage_cents` per fill. B.2
spec calls for breaking that down into independently-actionable parts:

  1. spread_cost      — half bid-ask premium paid per round trip
  2. adverse_move     — price drift during unfilled order lifetime
                        (mid_at_fill - mid_at_placement)
  3. partial_fill     — opportunity cost when requested size wasn't fully
                        filled (requested - filled, at filled price)
  4. cancel_replace   — rebooked at worse price after cancel
                        (DEFERRED — requires order lifecycle event capture
                         not yet logged)

Each component drives a different operational decision per Engineering B.2:
  - Wide spread → bid-improve or skip the market
  - Adverse move dominant → tighten time-in-force
  - Partial fill dominant → market making with smaller orders
  - Cancel-replace → strategy is over-aggressive on cancel/replace decisions

This module exposes pure-function computation helpers; persistence is to
the new columns added by alembic c8d2e5f1b943.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def spread_cost_cents(
    yes_bid_at_placement_cents: int,
    yes_ask_at_placement_cents: int,
    side: str,
    contracts: int,
) -> Optional[int]:
    """Half the bid-ask spread × contracts, attributed as taker cost.

    For YES buy at ask: spread_cost = (ask - mid) × contracts = half_spread × contracts
    For NO buy at ask:  same.

    Returns None when either bid or ask is unavailable / invalid.
    """
    if yes_bid_at_placement_cents <= 0 or yes_ask_at_placement_cents <= 0:
        return None
    if yes_ask_at_placement_cents < yes_bid_at_placement_cents:
        return None  # inverted book — bad data
    half_spread_cents = (yes_ask_at_placement_cents - yes_bid_at_placement_cents) // 2
    return half_spread_cents * max(1, contracts)


def adverse_move_cents(
    mid_at_placement_cents: int,
    mid_at_fill_cents: int,
    side: str,
    contracts: int,
) -> Optional[int]:
    """Adverse price drift during the unfilled-order window.

    For YES buy: adverse if mid drops between placement and fill (we paid
    more than the new mid — we'd have gotten a better fill if patient).
    Defined as positive when adverse.

    For NO buy: inverse — adverse if mid rises.
    """
    if mid_at_placement_cents <= 0 or mid_at_fill_cents <= 0:
        return None
    s = side.lower()
    if s == "yes":
        # YES buyer: paying more than current mid is adverse
        delta = mid_at_placement_cents - mid_at_fill_cents
    elif s == "no":
        # NO buyer: paying more (1 - mid) when mid is HIGHER is adverse
        delta = mid_at_fill_cents - mid_at_placement_cents
    else:
        return None
    return max(0, delta) * max(1, contracts)


def partial_fill_cents(
    requested_contracts: int,
    filled_contracts: int,
    fill_price_cents: int,
) -> Optional[int]:
    """Opportunity cost of unfilled remainder, conservatively priced
    at the realized fill_price (we'd have gotten ~the same price).

    Returns None on invalid input. Returns 0 when filled in full.
    """
    if requested_contracts <= 0 or filled_contracts < 0:
        return None
    if filled_contracts >= requested_contracts:
        return 0
    if fill_price_cents <= 0:
        return None
    unfilled = requested_contracts - filled_contracts
    # Cost = unfilled × fill_price — represents the position we wanted
    # but didn't get. (Edge-on-unfilled is the true cost; this is a
    # conservative bound proxied by fill_price.)
    return unfilled * fill_price_cents


def decompose_fill_slippage(
    *,
    side: str,
    contracts_requested: int,
    contracts_filled: int,
    fill_price_cents: int,
    yes_bid_at_placement_cents: int = 0,
    yes_ask_at_placement_cents: int = 0,
    mid_at_placement_cents: int = 0,
    mid_at_fill_cents: int = 0,
) -> dict[str, Optional[int]]:
    """Compute all 3 decomposition components for one fill. Returns dict
    with `spread_cost_cents`, `adverse_move_cents`, `partial_fill_cents`.
    Components that can't be computed (missing inputs) come back as None.

    Caller writes these into the executions row via ep_pg_audit on insert.
    cancel_replace_cents is NOT computed here (out of scope per B.2 §);
    that column stays NULL until lifecycle logging lands.
    """
    return {
        "spread_cost_cents":  spread_cost_cents(
            yes_bid_at_placement_cents, yes_ask_at_placement_cents, side, contracts_filled,
        ),
        "adverse_move_cents": adverse_move_cents(
            mid_at_placement_cents, mid_at_fill_cents, side, contracts_filled,
        ),
        "partial_fill_cents": partial_fill_cents(
            contracts_requested, contracts_filled, fill_price_cents,
        ),
    }
