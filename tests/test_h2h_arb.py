"""tests/test_h2h_arb.py — Phase 2A #1 H2H sum-to-1 arb scanner.

Validates the scanner emits / doesn't emit signals under the conditions
specified in EdgePulse_Backtest_Verdict_2026.md §3.1. Pure unit test
against synthetic market dicts — no Kalshi API, no Redis.

Run with: python -m pytest tests/test_h2h_arb.py -v
"""

from __future__ import annotations

import pytest

from kalshi_bot.strategy import scan_h2h_sum_to_1_arb


def _mk(ticker: str, event: str, yes_ask: float) -> dict:
    return {
        "ticker":          ticker,
        "event_ticker":    event,
        "yes_ask_dollars": yes_ask,
        "close_time":      "2026-05-14T22:00:00Z",
    }


def test_sum_below_threshold_emits_arb():
    """sum_ask < 0.98 → emit a single 2-leg arb signal."""
    m1 = _mk("KXMLBGAME-26MAY14-A-PHI", "KXMLBGAME-26MAY14-A", 0.48)
    m2 = _mk("KXMLBGAME-26MAY14-A-ATL", "KXMLBGAME-26MAY14-A", 0.48)
    sigs = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=10)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.model_source == "h2h_sum_to_1_arb"
    assert s.category == "arb"
    assert s.side == "yes"
    assert s.confidence == 0.95
    assert s.arb_legs is not None and len(s.arb_legs) == 2
    assert all(leg["side"] == "yes" for leg in s.arb_legs)
    assert {leg["ticker"] for leg in s.arb_legs} == {m1["ticker"], m2["ticker"]}
    assert abs(s.edge - 0.04) < 1e-9
    assert s.contracts >= 1


def test_sum_at_or_above_threshold_no_arb():
    """sum_ask >= 0.98 → no signal (verdict §3.1 cutoff)."""
    m1 = _mk("KXATPMATCH-A-X", "KXATPMATCH-A", 0.49)
    m2 = _mk("KXATPMATCH-A-Y", "KXATPMATCH-A", 0.49)
    sigs = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=10)
    assert sigs == []


def test_three_outcome_event_skipped():
    """Events with > 2 outcomes don't satisfy the 2-team sum-to-1 invariant."""
    m1 = _mk("KXNCAAFGAME-26-EVT-A", "KXNCAAFGAME-26-EVT", 0.30)
    m2 = _mk("KXNCAAFGAME-26-EVT-B", "KXNCAAFGAME-26-EVT", 0.30)
    m3 = _mk("KXNCAAFGAME-26-EVT-C", "KXNCAAFGAME-26-EVT", 0.30)
    sigs = scan_h2h_sum_to_1_arb([m1, m2, m3], max_contracts=10)
    assert sigs == []


def test_non_h2h_prefix_ignored():
    """KXFED and other non-H2H prefixes are filtered out at the top."""
    m = _mk("KXFED-26JUN-T5.25", "KXFED-26JUN", 0.10)
    sigs = scan_h2h_sum_to_1_arb([m], max_contracts=10)
    assert sigs == []


def test_malformed_price_skips_event():
    """Any leg with yes_ask <= 0 or >= 1 disqualifies the entire event."""
    m1 = _mk("KXNHLGAME-A", "KXNHLGAME-EVT", 0.0)
    m2 = _mk("KXNHLGAME-B", "KXNHLGAME-EVT", 0.50)
    sigs = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=10)
    assert sigs == []


@pytest.mark.parametrize("prefix", [
    "KXMLBGAME",
    "KXMLSGAME",
    "KXWTAMATCH",
    "KXATPMATCH",
    "KXNCAAMBGAME",
    "KXNCAAFGAME",
    "KXNHLGAME",
])
def test_all_seven_prefixes_emit(prefix: str):
    """Every verdict-validated prefix fires on a low-sum 2-outcome event."""
    m1 = _mk(f"{prefix}-A-1", f"{prefix}-EVT", 0.45)
    m2 = _mk(f"{prefix}-B-1", f"{prefix}-EVT", 0.45)
    sigs = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=10)
    assert len(sigs) == 1, f"prefix {prefix} did not emit"


def test_contracts_scale_with_gross_edge():
    """Larger gross arb produces larger size, capped at max_contracts."""
    # sum=0.85 → gross=15¢
    m1 = _mk("KXMLSGAME-A", "KXMLSGAME-EVT", 0.40)
    m2 = _mk("KXMLSGAME-B", "KXMLSGAME-EVT", 0.45)
    sigs = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=20)
    assert len(sigs) == 1
    # int(0.15 * 50) = 7
    assert sigs[0].contracts == 7

    # Cap test: sum=0.10 → gross=90¢ → would scale to 45, capped at 5
    m3 = _mk("KXNHLGAME-A", "KXNHLGAME-EVT", 0.05)
    m4 = _mk("KXNHLGAME-B", "KXNHLGAME-EVT", 0.05)
    sigs2 = scan_h2h_sum_to_1_arb([m3, m4], max_contracts=5)
    assert sigs2[0].contracts == 5


def test_custom_threshold_argument():
    """Caller can tighten the threshold (e.g., for cost-conscious mode)."""
    m1 = _mk("KXMLBGAME-A", "KXMLBGAME-EVT", 0.48)
    m2 = _mk("KXMLBGAME-B", "KXMLBGAME-EVT", 0.48)
    # Default threshold 0.98 → would fire; tightened to 0.92 → no fire
    sigs = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=10, sum_threshold=0.92)
    assert sigs == []
    # Loosened to 0.99 → still fires (sum=0.96)
    sigs2 = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=10, sum_threshold=0.99)
    assert len(sigs2) == 1


def test_maker_price_used_when_yes_bid_available():
    """When yes_bid_dollars is present, scanner places orders at BID (maker),
    not at ASK (taker). Engineering S.1 maker-first execution."""
    # Market with explicit bid + ask spread
    m1 = {
        "ticker": "KXMLBGAME-A", "event_ticker": "KXMLBGAME-EVT",
        "yes_ask_dollars": 0.50, "yes_bid_dollars": 0.46,
        "close_time": "2026-05-14T22:00:00Z",
    }
    m2 = {
        "ticker": "KXMLBGAME-B", "event_ticker": "KXMLBGAME-EVT",
        "yes_ask_dollars": 0.45, "yes_bid_dollars": 0.42,
        "close_time": "2026-05-14T22:00:00Z",
    }
    # Sum at ASK = 0.95 (below 0.98 threshold → arb fires)
    # Sum at BID = 0.88 (gross edge = 12¢ at bid, vs 5¢ at ask)
    sigs = scan_h2h_sum_to_1_arb([m1, m2], max_contracts=20)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.arb_legs is not None
    # Each leg's price_cents should reflect the BID, not the ASK
    leg_prices = {leg["ticker"]: leg["price_cents"] for leg in s.arb_legs}
    assert leg_prices["KXMLBGAME-A"] == 46, (
        f"Expected 46¢ (yes_bid for A), got {leg_prices['KXMLBGAME-A']}"
    )
    assert leg_prices["KXMLBGAME-B"] == 42, (
        f"Expected 42¢ (yes_bid for B), got {leg_prices['KXMLBGAME-B']}"
    )
    # Signal.market_price reflects the maker price for leg 1
    assert abs(s.market_price - 0.46) < 1e-9
    # Gross edge computed at BID: 1.0 - (0.46 + 0.42) = 0.12
    assert abs(s.edge - 0.12) < 1e-9
