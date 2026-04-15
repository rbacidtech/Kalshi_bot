"""
tests/test_signal_filter.py — Unit tests for signal dedup and filtering logic.

Tests the Intel-side dedup check (skip tickers already held in positions)
and the edge/confidence gate that drops signals before they reach the bus.

No network, no Redis, no auth — all pure logic exercised directly.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from kalshi_bot.strategy import _fee_adjusted_edge, Signal, KALSHI_FEE_RATE, MIN_EDGE_GROSS


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_signal(
    ticker:       str   = "KXFED-23DEC99-T5.25",
    fair_value:   float = 0.65,
    market_price: float = 0.50,
    side:         str   = "yes",
    confidence:   float = 0.80,
    spread_cents: int   = 5,
) -> Signal:
    return Signal(
        ticker            = ticker,
        title             = f"Test {ticker}",
        category          = "fomc",
        side              = side,
        fair_value        = fair_value,
        market_price      = market_price,
        edge              = abs(fair_value - market_price),
        fee_adjusted_edge = _fee_adjusted_edge(fair_value, market_price, side),
        contracts         = 1,
        confidence        = confidence,
        model_source      = "test",
        spread_cents      = spread_cents,
    )


# ── Dedup filter (mirrors Intel loop logic) ───────────────────────────────────

def apply_dedup(signals: list, positions: dict) -> list:
    """Replicate the Intel dedup check: filter signals for tickers already held."""
    return [s for s in signals if s.ticker not in positions]


class TestDedup:

    def test_no_positions_passes_all(self):
        sigs = [make_signal("TICKER-A"), make_signal("TICKER-B")]
        assert apply_dedup(sigs, {}) == sigs

    def test_held_ticker_filtered(self):
        sigs = [make_signal("TICKER-A"), make_signal("TICKER-B")]
        result = apply_dedup(sigs, {"TICKER-A": {"side": "yes"}})
        assert len(result) == 1
        assert result[0].ticker == "TICKER-B"

    def test_all_held_empty_result(self):
        sigs = [make_signal("TICKER-A"), make_signal("TICKER-B")]
        positions = {"TICKER-A": {}, "TICKER-B": {}}
        assert apply_dedup(sigs, positions) == []

    def test_empty_signals_empty_result(self):
        assert apply_dedup([], {"TICKER-A": {}}) == []

    def test_unknown_ticker_passes(self):
        """A signal for a ticker NOT in positions should pass through."""
        sigs = [make_signal("TICKER-NEW")]
        result = apply_dedup(sigs, {"TICKER-OLD": {}})
        assert len(result) == 1


# ── Edge/confidence gate ──────────────────────────────────────────────────────

def apply_edge_filter(
    signals:        list,
    edge_threshold: float = 0.12,
    min_confidence: float = 0.70,
) -> list:
    """
    Replicate the edge + confidence gate from fetch_signals_async.
    Signals below either threshold are dropped.
    """
    return [
        s for s in signals
        if s.edge >= edge_threshold and s.confidence >= min_confidence
    ]


class TestEdgeFilter:

    def test_above_threshold_passes(self):
        sig = make_signal(fair_value=0.70, market_price=0.50, confidence=0.85)
        # edge = 0.20 ≥ 0.12 and confidence = 0.85 ≥ 0.70
        result = apply_edge_filter([sig])
        assert len(result) == 1

    def test_below_edge_threshold_filtered(self):
        sig = make_signal(fair_value=0.60, market_price=0.52, confidence=0.85)
        # edge = 0.08 < 0.12
        result = apply_edge_filter([sig])
        assert len(result) == 0

    def test_below_confidence_threshold_filtered(self):
        sig = make_signal(fair_value=0.70, market_price=0.50, confidence=0.60)
        # confidence = 0.60 < 0.70
        result = apply_edge_filter([sig])
        assert len(result) == 0

    def test_both_thresholds_must_pass(self):
        sig_both   = make_signal(fair_value=0.70, market_price=0.50, confidence=0.85)
        sig_edge   = make_signal(fair_value=0.70, market_price=0.50, confidence=0.50)
        sig_conf   = make_signal(fair_value=0.60, market_price=0.52, confidence=0.85)
        sig_neither = make_signal(fair_value=0.55, market_price=0.52, confidence=0.50)

        result = apply_edge_filter([sig_both, sig_edge, sig_conf, sig_neither])
        assert len(result) == 1
        assert result[0] is sig_both

    def test_exact_threshold_passes(self):
        """Edge exactly at threshold should not be filtered."""
        sig = make_signal(fair_value=0.62, market_price=0.50, confidence=0.70)
        # edge = 0.12, confidence = 0.70
        result = apply_edge_filter([sig], edge_threshold=0.12, min_confidence=0.70)
        assert len(result) == 1

    def test_sorting_by_fee_adjusted_edge(self):
        """Higher fee-adjusted edge signals should sort first."""
        low  = make_signal("LOW",  fair_value=0.62, market_price=0.50, confidence=0.80)
        high = make_signal("HIGH", fair_value=0.80, market_price=0.50, confidence=0.80)
        signals = [low, high]
        signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
        assert signals[0].ticker == "HIGH"


# ── MIN_EDGE_GROSS constant ───────────────────────────────────────────────────

class TestMinEdgeGross:

    def test_constant_is_twelve_cents(self):
        assert MIN_EDGE_GROSS == pytest.approx(0.12)

    def test_fee_adjusted_edge_below_gross_at_threshold(self):
        """At MIN_EDGE_GROSS, fee-adjusted edge should be positive but small."""
        edge = _fee_adjusted_edge(0.62, 0.50, "yes")
        assert edge > 0
