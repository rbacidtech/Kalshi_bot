"""
tests/test_fees.py — Unit tests for Kalshi fee model.

Exercises:
  - _fee_adjusted_edge() in kalshi_bot/strategy.py
  - Signal.net_payout()
  - The hardcoded 7% fee rate constant

No network, no Redis, no auth.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from kalshi_bot.strategy import _fee_adjusted_edge, Signal, KALSHI_FEE_RATE


FEE = KALSHI_FEE_RATE   # 0.07


def make_signal(
    fair_value:   float = 0.60,
    market_price: float = 0.50,
    side:         str   = "yes",
    contracts:    int   = 1,
) -> Signal:
    return Signal(
        ticker            = "TEST-23DEC99",
        title             = "Test market",
        category          = "fomc",
        side              = side,
        fair_value        = fair_value,
        market_price      = market_price,
        edge              = abs(fair_value - market_price),
        fee_adjusted_edge = _fee_adjusted_edge(fair_value, market_price, side),
        contracts         = contracts,
        confidence        = 1.0,
        model_source      = "test",
    )


class TestFeeRate:

    def test_fee_rate_is_seven_percent(self):
        assert KALSHI_FEE_RATE == pytest.approx(0.07)


class TestFeeAdjustedEdge:

    def test_yes_side_positive_edge(self):
        """fair=0.60, price=0.50 YES → should be positive after fees."""
        edge = _fee_adjusted_edge(0.60, 0.50, "yes")
        assert edge > 0

    def test_yes_side_no_edge(self):
        """fair=price → raw edge 0, fee-adjusted should be negative."""
        edge = _fee_adjusted_edge(0.50, 0.50, "yes")
        assert edge < 0

    def test_no_side_positive_edge(self):
        """fair=0.30 (true prob YES), market_price=0.50 → NO has edge."""
        # NO edge: fair_value here is prob of YES, market_price=0.50
        # Buying NO at 0.50 when true prob NO is 0.70 → positive EV
        edge = _fee_adjusted_edge(0.70, 0.50, "no")
        assert edge > 0

    def test_yes_formula_correctness(self):
        """Manually verify YES formula: EV = p*(1-price)*(1-fee) - (1-p)*price."""
        fv, mp = 0.65, 0.50
        expected = fv * (1 - mp) * (1 - FEE) - (1 - fv) * mp
        assert _fee_adjusted_edge(fv, mp, "yes") == pytest.approx(expected, abs=1e-9)

    def test_no_formula_correctness(self):
        """Manually verify NO formula: EV = p*price*(1-fee) - (1-p)*(1-price)."""
        fv, mp = 0.70, 0.50
        expected = fv * mp * (1 - FEE) - (1 - fv) * (1 - mp)
        assert _fee_adjusted_edge(fv, mp, "no") == pytest.approx(expected, abs=1e-9)

    def test_high_price_yes_reduces_edge(self):
        """Higher entry price on YES reduces fee-adjusted edge for same fair value."""
        edge_low  = _fee_adjusted_edge(0.80, 0.60, "yes")
        edge_high = _fee_adjusted_edge(0.80, 0.75, "yes")
        assert edge_low > edge_high

    def test_symmetry_near_zero(self):
        """At no model edge, both sides should yield negative EV after fees.

        Convention for _fee_adjusted_edge:
          YES side: fair_value = P(YES wins), market_price = YES price
          NO  side: fair_value = P(NO wins) = 1 - P(YES), market_price = YES price

        "No model edge" means the side's winning probability == its cost:
          YES: fair_value (P(YES))  == YES cost == market_price
          NO:  fair_value (P(NO))   == NO  cost == 1 - market_price
        """
        for price in (0.30, 0.50, 0.70):
            # YES side: fair == market → only fees
            edge_yes = _fee_adjusted_edge(price, price, "yes")
            assert edge_yes < 0, f"Expected negative EV at fair==market price={price} side=yes"
            # NO side: P(NO wins) = 1 - price == NO cost = 1 - price → only fees
            edge_no = _fee_adjusted_edge(1 - price, price, "no")
            assert edge_no < 0, f"Expected negative EV at fair==market price={price} side=no"


class TestSignalNetPayout:

    def test_yes_net_payout_positive_when_edge(self):
        sig = make_signal(fair_value=0.70, market_price=0.50, side="yes")
        assert sig.net_payout() > 0

    def test_yes_net_payout_formula(self):
        sig = make_signal(fair_value=0.70, market_price=0.50, side="yes")
        expected = 0.70 * (0.50 * (1 - FEE)) - 0.30 * 0.50
        assert sig.net_payout() == pytest.approx(expected, abs=1e-9)

    def test_no_net_payout_formula(self):
        sig = make_signal(fair_value=0.30, market_price=0.50, side="no")
        # fair_value is prob of winning the NO contract
        expected = 0.30 * (0.50 * (1 - FEE)) - 0.70 * (1 - 0.50)
        assert sig.net_payout() == pytest.approx(expected, abs=1e-9)

    def test_net_payout_negative_with_no_edge(self):
        sig = make_signal(fair_value=0.50, market_price=0.50, side="yes")
        assert sig.net_payout() < 0

    def test_tax_reserve_proportional_to_win(self):
        sig = make_signal(fair_value=0.70, market_price=0.50, side="yes", contracts=10)
        # gross_win = (1 - 0.50) * 10 = 5.0
        # reserve   = 5.0 * 0.30 = 1.50
        assert sig.tax_reserve() == pytest.approx(1.50, abs=1e-9)
