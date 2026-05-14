"""tests/test_slippage_decomposition.py — Engineering B.2 unit tests."""

from __future__ import annotations

from ep_slippage_decomposition import (
    spread_cost_cents,
    adverse_move_cents,
    partial_fill_cents,
    decompose_fill_slippage,
)


def test_spread_cost_basic():
    # bid=48, ask=52 → half-spread=2c × 10 = 20c
    assert spread_cost_cents(48, 52, "yes", 10) == 20


def test_spread_cost_inverted_book_returns_none():
    assert spread_cost_cents(52, 48, "yes", 10) is None


def test_spread_cost_missing_data_returns_none():
    assert spread_cost_cents(0, 52, "yes", 10) is None


def test_adverse_move_yes_buyer_adverse():
    # YES buy: placement mid 50c, fill mid 48c → paid 2c more than new mid → adverse
    assert adverse_move_cents(50, 48, "yes", 5) == 10  # 2c × 5


def test_adverse_move_yes_buyer_favorable():
    # YES buy at 50, mid moves UP to 52 — we got the lower price → favorable
    # Defined as max(0, delta) so favorable returns 0 (no adverse component)
    assert adverse_move_cents(50, 52, "yes", 5) == 0


def test_adverse_move_no_buyer():
    # NO buy: paying (1 - mid). Mid rising means NO is more expensive (adverse).
    assert adverse_move_cents(50, 53, "no", 4) == 12  # 3c × 4


def test_partial_fill_basic():
    # Wanted 20, got 15 at 48c → 5 unfilled × 48c = 240c opportunity
    assert partial_fill_cents(20, 15, 48) == 240


def test_partial_fill_full_fill_zero():
    assert partial_fill_cents(20, 20, 48) == 0


def test_partial_fill_overfill_zero():
    """Filled > requested is treated as 'no partial fill cost' (full + extra)."""
    assert partial_fill_cents(20, 25, 48) == 0


def test_decompose_all_components():
    result = decompose_fill_slippage(
        side="yes",
        contracts_requested=20,
        contracts_filled=15,
        fill_price_cents=50,
        yes_bid_at_placement_cents=48,
        yes_ask_at_placement_cents=52,
        mid_at_placement_cents=50,
        mid_at_fill_cents=49,
    )
    # spread: half=2c × 15 filled = 30c
    assert result["spread_cost_cents"] == 30
    # adverse: 50-49=1c × 15 filled = 15c
    assert result["adverse_move_cents"] == 15
    # partial: 5 unfilled × 50c = 250c
    assert result["partial_fill_cents"] == 250


def test_decompose_missing_inputs_returns_none_components():
    result = decompose_fill_slippage(
        side="yes",
        contracts_requested=10,
        contracts_filled=10,
        fill_price_cents=50,
        # No book/mid data
    )
    assert result["spread_cost_cents"] is None
    assert result["adverse_move_cents"] is None
    assert result["partial_fill_cents"] == 0  # full fill — zero opportunity cost
