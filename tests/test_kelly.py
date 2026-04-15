"""
tests/test_kelly.py — Unit tests for RiskManager Kelly sizing.

Exercises kalshi_bot/risk.py RiskManager.size() in isolation —
no network, no Redis, no auth.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from kalshi_bot.risk import RiskManager, RiskConfig


def make_risk(
    kelly_fraction:       float = 0.25,
    max_contracts:        int   = 10,
    max_market_exposure:  float = 0.05,
    max_total_exposure:   float = 0.30,
    daily_drawdown_limit: float = 0.10,
    max_spread_cents:     int   = 10,
    fee_cents:            int   = 7,
) -> RiskManager:
    return RiskManager(RiskConfig(
        kelly_fraction       = kelly_fraction,
        max_contracts        = max_contracts,
        max_market_exposure  = max_market_exposure,
        max_total_exposure   = max_total_exposure,
        daily_drawdown_limit = daily_drawdown_limit,
        max_spread_cents     = max_spread_cents,
        fee_cents            = fee_cents,
    ))


class TestKellySizing:

    def test_zero_balance_returns_zero(self):
        rm = make_risk()
        assert rm.size(edge=0.20, market_price=0.50, balance_cents=0) == 0

    def test_negative_balance_returns_zero(self):
        rm = make_risk()
        assert rm.size(edge=0.20, market_price=0.50, balance_cents=-1000) == 0

    def test_edge_below_fee_returns_zero(self):
        """Net edge = 0.05 - 0.07 = -0.02 → no trade."""
        rm = make_risk(fee_cents=7)
        assert rm.size(edge=0.05, market_price=0.50, balance_cents=100_000) == 0

    def test_edge_equal_to_fee_returns_zero(self):
        """Net edge = 0.07 - 0.07 = 0 → no trade."""
        rm = make_risk(fee_cents=7)
        assert rm.size(edge=0.07, market_price=0.50, balance_cents=100_000) == 0

    def test_positive_edge_yes_side(self):
        """With known values, verify formula gives a positive integer result."""
        rm = make_risk(kelly_fraction=0.25, fee_cents=7, max_contracts=100,
                       max_market_exposure=1.0)
        # net_edge = 0.20 - 0.07 = 0.13
        # kelly_f  = 0.13 / (1 - 0.50) = 0.26
        # bet_frac = 0.26 * 0.25 * 1.0 = 0.065
        # max_kelly = int(100_000 * 0.065 / 50) = 130
        contracts = rm.size(
            edge=0.20, market_price=0.50, balance_cents=100_000,
            confidence=1.0, side="yes",
        )
        assert contracts == 130

    def test_positive_edge_no_side(self):
        """NO side uses market_price as denominator (win amount = price)."""
        rm = make_risk(kelly_fraction=0.25, fee_cents=7, max_contracts=500,
                       max_market_exposure=1.0)
        # net_edge = 0.20 - 0.07 = 0.13
        # kelly_f  = 0.13 / 0.30  (market_price = 0.30)
        # bet_frac = kelly_f * 0.25 * 1.0
        # max_kelly = int(100_000 * bet_frac / 30)
        net_edge = 0.20 - 0.07
        kelly_f  = net_edge / 0.30
        bet_frac = kelly_f * 0.25
        expected = int(100_000 * bet_frac / 30)
        contracts = rm.size(
            edge=0.20, market_price=0.30, balance_cents=100_000,
            confidence=1.0, side="no",
        )
        assert contracts == expected

    def test_confidence_scales_position(self):
        """Higher confidence → more contracts for same edge."""
        rm = make_risk(max_contracts=1000, max_market_exposure=1.0)
        low  = rm.size(edge=0.20, market_price=0.50, balance_cents=100_000, confidence=0.50)
        high = rm.size(edge=0.20, market_price=0.50, balance_cents=100_000, confidence=0.90)
        assert high > low

    def test_capped_at_max_contracts(self):
        """Result never exceeds max_contracts regardless of Kelly output."""
        rm = make_risk(max_contracts=5, max_market_exposure=1.0)
        contracts = rm.size(
            edge=0.50, market_price=0.50, balance_cents=10_000_000,
            confidence=1.0, side="yes",
        )
        assert contracts == 5

    def test_capped_at_market_exposure(self):
        """Result limited by max_market_exposure fraction of balance."""
        rm = make_risk(max_contracts=10_000, max_market_exposure=0.05, kelly_fraction=1.0)
        # max_by_cap = int(100_000 * 0.05 / 50) = 100
        contracts = rm.size(
            edge=0.40, market_price=0.50, balance_cents=100_000,
            confidence=1.0, side="yes",
        )
        # Should be capped at 100, not the (much larger) Kelly amount
        assert contracts <= 100

    def test_very_high_price_yes_side(self):
        """Market price near 1.0 → win amount near 0 → few contracts."""
        rm = make_risk(max_contracts=1000, max_market_exposure=1.0)
        contracts = rm.size(
            edge=0.05, market_price=0.97, balance_cents=100_000,
            confidence=1.0, side="yes",
        )
        # net_edge = 0.05 - 0.07 = -0.02 → no trade
        assert contracts == 0

    def test_drawdown_halt_blocks_trading(self):
        """After daily drawdown limit hit, approve() returns False."""
        rm = make_risk(daily_drawdown_limit=0.10)
        rm.set_balance(100_000)
        rm.set_balance(89_000)   # 11% drawdown → halted
        approved = rm.approve(
            ticker="TEST-A", contracts=1,
            market_price=0.50, balance_cents=89_000,
            open_exposure_cents=0,
        )
        assert approved is False


class TestKellyApprove:

    def test_approve_basic(self):
        rm = make_risk()
        assert rm.approve(
            ticker="TEST-A", contracts=2, market_price=0.50,
            balance_cents=100_000, open_exposure_cents=0,
        ) is True

    def test_approve_rejects_zero_contracts(self):
        rm = make_risk()
        assert rm.approve(
            ticker="TEST-A", contracts=0, market_price=0.50,
            balance_cents=100_000, open_exposure_cents=0,
        ) is False

    def test_approve_rejects_wide_spread(self):
        rm = make_risk(max_spread_cents=5)
        assert rm.approve(
            ticker="TEST-A", contracts=2, market_price=0.50,
            balance_cents=100_000, open_exposure_cents=0,
            spread_cents=10,
        ) is False

    def test_approve_rejects_excess_total_exposure(self):
        rm = make_risk(max_total_exposure=0.30)
        # open_exposure already at 30% of balance
        assert rm.approve(
            ticker="TEST-A", contracts=5, market_price=0.50,
            balance_cents=100_000, open_exposure_cents=30_000,
        ) is False
