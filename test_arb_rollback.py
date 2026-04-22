"""
tests/test_arb_rollback.py — End-to-end rollback path for multi-leg butterfly arb.

Verifies three scenarios:
  1. All legs succeed    → no rollback triggered
  2. Leg N fails, all cancels succeed (clean rollback) → RuntimeError, no orphans
  3. Leg N fails, one cancel also fails (ArbRollbackFailed) → unrecovered legs returned

Run with:  python -m pytest tests/test_arb_rollback.py -v
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kalshi_bot.executor import Executor, ArbRollbackFailed


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_executor(paper: bool = False) -> Executor:
    client = MagicMock()
    return Executor(
        client     = client,
        trades_csv = Path("/tmp/test_trades_arb_rollback.csv"),
        paper      = paper,
    )


def _legs(n: int) -> list:
    return [
        {"ticker": f"KXFED-26JUN-T{3.50 + i * 0.25:.2f}", "side": "yes", "price_cents": 50 + i}
        for i in range(n)
    ]


# ── Scenario 1: all legs succeed ──────────────────────────────────────────────

def test_all_legs_succeed():
    exe = _make_executor(paper=False)
    legs = _legs(3)

    # Every live-leg call returns a unique order_id
    exe._arb_live_leg = MagicMock(side_effect=[f"oid-{i}" for i in range(3)])

    order_ids = exe.execute_arb_legs(legs, contracts_per_leg=1)

    assert order_ids == ["oid-0", "oid-1", "oid-2"]
    exe._arb_live_leg.assert_called()
    # No cancel calls — nothing should have failed
    exe.client._request.assert_not_called()


# ── Scenario 2: leg 2 fails, all cancels succeed (clean RuntimeError) ─────────

def test_leg_fails_clean_rollback():
    exe = _make_executor(paper=False)
    legs = _legs(3)

    # Leg 0 and 1 succeed; leg 2 fails (returns "")
    exe._arb_live_leg = MagicMock(side_effect=["oid-0", "oid-1", ""])

    # Cancel requests succeed (no exception raised)
    exe.client._request = MagicMock(return_value={"status": "cancelled"})

    with pytest.raises(RuntimeError) as exc_info:
        exe.execute_arb_legs(legs, contracts_per_leg=1)

    # Must be a plain RuntimeError, NOT ArbRollbackFailed
    assert type(exc_info.value) is RuntimeError
    assert "clean rollback" in str(exc_info.value)

    # Both placed legs should have had cancel attempted
    assert exe.client._request.call_count == 2
    for call in exe.client._request.call_args_list:
        assert call.args[0] == "DELETE"
        assert "/portfolio/orders/" in call.args[1]


# ── Scenario 3: leg 2 fails AND cancel of leg 0 also fails → ArbRollbackFailed

def test_leg_fails_cancel_also_fails():
    exe = _make_executor(paper=False)
    legs = _legs(3)

    # Leg 0 and 1 succeed; leg 2 fails
    exe._arb_live_leg = MagicMock(side_effect=["oid-0", "oid-1", ""])

    # Cancel of oid-1 succeeds, cancel of oid-0 raises (API error)
    def _cancel_side_effect(method, path):
        if "oid-0" in path:
            raise ConnectionError("Kalshi API timeout")
        return {"status": "cancelled"}

    exe.client._request = MagicMock(side_effect=_cancel_side_effect)

    with pytest.raises(ArbRollbackFailed) as exc_info:
        exe.execute_arb_legs(legs, contracts_per_leg=1)

    exc = exc_info.value
    # Must carry exactly one unrecovered leg (oid-0)
    assert len(exc.unrecovered) == 1
    ticker, side, order_id = exc.unrecovered[0]
    assert order_id == "oid-0"
    assert side == "yes"

    # Error message must describe the partial failure
    assert "unrecovered" in str(exc).lower() or "cancel" in str(exc).lower()


# ── Scenario 4: paper mode — all legs are instant "paper" fills ───────────────

def test_paper_mode_no_cancel_needed():
    exe = _make_executor(paper=True)
    legs = _legs(2)

    # _arb_paper_leg is used in paper mode; stub it
    exe._arb_paper_leg = MagicMock(side_effect=["paper", "paper"])

    order_ids = exe.execute_arb_legs(legs, contracts_per_leg=1)
    assert order_ids == ["paper", "paper"]

    # No HTTP calls — paper mode never cancels
    exe.client._request.assert_not_called()


# ── Scenario 5: _arb_cancel_placed returns empty list when all cancels succeed ─

def test_cancel_placed_returns_empty_on_success():
    exe = _make_executor(paper=False)
    exe.client._request = MagicMock(return_value={"status": "cancelled"})

    placed = [("KXFED-26JUN-T3.50", "yes", "oid-a"),
              ("KXFED-26JUN-T3.75", "no",  "oid-b")]
    failures = exe._arb_cancel_placed(placed)

    assert failures == []
    assert exe.client._request.call_count == 2


# ── Scenario 6: _arb_cancel_placed returns failed legs, doesn't raise ─────────

def test_cancel_placed_returns_failures_without_raising():
    exe = _make_executor(paper=False)

    def _flaky(method, path):
        if "oid-bad" in path:
            raise IOError("network error")
        return {"status": "cancelled"}

    exe.client._request = MagicMock(side_effect=_flaky)

    placed = [("KXFED-26JUN-T3.50", "yes", "oid-ok"),
              ("KXFED-26JUN-T3.75", "no",  "oid-bad")]
    failures = exe._arb_cancel_placed(placed)

    assert len(failures) == 1
    assert failures[0][2] == "oid-bad"
    # Must NOT raise — caller decides what to do
