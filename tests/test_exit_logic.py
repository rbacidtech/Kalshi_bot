"""
tests/test_exit_logic.py — Unit tests for take-profit / stop-loss exit logic.

Exercises the move_cents calculation and trigger conditions that live in
ep_exec.py _exit_checker(), extracted into a testable pure function.
No network, no Redis, no asyncio needed.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import time


# ── Pure exit logic extracted from ep_exec._exit_checker ─────────────────────

TAKE_PROFIT_CENTS = 15   # mirrors cfg default
STOP_LOSS_CENTS   = 10   # mirrors cfg default


def compute_move(current_cents: int, entry_cents: int, side: str) -> int:
    """
    Replicate the move_cents formula from _exit_checker.

    YES: profit when price rises  → move = current - entry
    NO:  profit when price falls  → move = entry   - current
    """
    if side == "yes":
        return current_cents - entry_cents
    return entry_cents - current_cents


def should_exit(
    current_cents:      int,
    entry_cents:        int,
    side:               str,
    take_profit_cents:  int = TAKE_PROFIT_CENTS,
    stop_loss_cents:    int = STOP_LOSS_CENTS,
) -> str | None:
    """Return exit reason string or None if no exit triggered."""
    move = compute_move(current_cents, entry_cents, side)
    if move >= take_profit_cents:
        return f"take_profit (+{move}¢)"
    if move <= -stop_loss_cents:
        return f"stop_loss ({move}¢)"
    return None


def is_stale(ts_us: int, stale_age_s: int = 300) -> bool:
    """Return True if the price timestamp is older than stale_age_s seconds."""
    cutoff_us = int(time.time() * 1_000_000) - stale_age_s * 1_000_000
    return ts_us < cutoff_us


# ── Move calculation ──────────────────────────────────────────────────────────

class TestComputeMove:

    def test_yes_profit(self):
        assert compute_move(65, 50, "yes") == 15

    def test_yes_loss(self):
        assert compute_move(40, 50, "yes") == -10

    def test_no_profit(self):
        """NO is profitable when price drops."""
        assert compute_move(35, 50, "no") == 15

    def test_no_loss(self):
        assert compute_move(60, 50, "no") == -10

    def test_no_change(self):
        assert compute_move(50, 50, "yes") == 0
        assert compute_move(50, 50, "no")  == 0


# ── Take-profit trigger ───────────────────────────────────────────────────────

class TestTakeProfit:

    def test_yes_take_profit_triggers(self):
        reason = should_exit(current_cents=65, entry_cents=50, side="yes")
        assert reason is not None
        assert "take_profit" in reason

    def test_yes_take_profit_exact_boundary(self):
        """Move == take_profit_cents should trigger."""
        reason = should_exit(
            current_cents=65, entry_cents=50, side="yes",
            take_profit_cents=15,
        )
        assert reason is not None

    def test_yes_take_profit_one_below_boundary(self):
        """Move == take_profit_cents - 1 should NOT trigger."""
        reason = should_exit(
            current_cents=64, entry_cents=50, side="yes",
            take_profit_cents=15,
        )
        assert reason is None

    def test_no_take_profit_triggers(self):
        """NO is profitable when price drops."""
        reason = should_exit(current_cents=35, entry_cents=50, side="no")
        assert reason is not None
        assert "take_profit" in reason

    def test_no_take_profit_rising_price_no_trigger(self):
        """NO loses when price rises — should not trigger take-profit."""
        reason = should_exit(
            current_cents=65, entry_cents=50, side="no",
            take_profit_cents=15,
        )
        assert reason is None or "stop" in reason


# ── Stop-loss trigger ─────────────────────────────────────────────────────────

class TestStopLoss:

    def test_yes_stop_loss_triggers(self):
        reason = should_exit(current_cents=40, entry_cents=50, side="yes")
        assert reason is not None
        assert "stop_loss" in reason

    def test_yes_stop_loss_exact_boundary(self):
        """Move == -stop_loss_cents should trigger."""
        reason = should_exit(
            current_cents=40, entry_cents=50, side="yes",
            stop_loss_cents=10,
        )
        assert reason is not None

    def test_yes_stop_loss_one_above_boundary(self):
        """Move == -(stop_loss_cents - 1) should NOT trigger."""
        reason = should_exit(
            current_cents=41, entry_cents=50, side="yes",
            stop_loss_cents=10,
        )
        assert reason is None

    def test_no_stop_loss_triggers(self):
        """NO loses when price rises."""
        reason = should_exit(current_cents=60, entry_cents=50, side="no")
        assert reason is not None
        assert "stop_loss" in reason

    def test_no_movement_no_exit(self):
        reason = should_exit(current_cents=50, entry_cents=50, side="yes")
        assert reason is None


# ── Stale price guard ─────────────────────────────────────────────────────────

class TestStalePriceGuard:

    def test_fresh_price_not_stale(self):
        fresh_ts = int(time.time() * 1_000_000)
        assert is_stale(fresh_ts) is False

    def test_old_price_is_stale(self):
        old_ts = int((time.time() - 400) * 1_000_000)   # 400 s ago > 300 s threshold
        assert is_stale(old_ts) is True

    def test_exactly_at_cutoff_is_stale(self):
        """ts == cutoff means the price is right at the boundary → stale."""
        cutoff_ts = int((time.time() - 300) * 1_000_000)
        assert is_stale(cutoff_ts, stale_age_s=300) is True

    def test_zero_ts_is_stale(self):
        assert is_stale(0) is True


# ── Integration: combined take-profit + stop-loss scenarios ──────────────────

class TestExitScenarios:

    @pytest.mark.parametrize("side,entry,current,expected", [
        ("yes", 50, 66, "take_profit"),   # YES position gains 16¢
        ("yes", 50, 39, "stop_loss"),     # YES position loses 11¢
        ("yes", 50, 55, None),            # YES moves 5¢ — hold
        ("no",  50, 34, "take_profit"),   # NO position gains 16¢ (price dropped)
        ("no",  50, 61, "stop_loss"),     # NO position loses 11¢ (price rose)
        ("no",  50, 46, None),            # NO moves 4¢ — hold
    ])
    def test_exit_scenarios(self, side, entry, current, expected):
        reason = should_exit(current, entry, side)
        if expected is None:
            assert reason is None
        else:
            assert reason is not None and expected in reason
