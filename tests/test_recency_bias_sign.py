"""tests/test_recency_bias_sign.py — Regression guard for ep_intel.py:2010.

The recency-bias edge recompute in ep_intel.py applies after
`recency_bias_adj` shifts `_sig.fair_value`. Prior to the 2026-05-22
fix, the recompute used the YES-only formula `fair_value - market_price`
unconditionally — inverting the sign for NO signals and triggering
`SignalMessage.edge < 0` rejection at the publish boundary.

This test pins the corrected behavior. The actual recompute lives
inline in ep_intel.py (not a separate function), so the test reimplements
the same branching logic and asserts the invariants we expect:

  1. YES signal + nonzero bias  → edge = fair_value - market_price
  2. NO  signal + nonzero bias  → edge = market_price - fair_value
  3. ANY signal + bias == 0.0   → edge is NOT recomputed (scanner value preserved)
  4. YES signal where bias drives fair_value < market_price → edge is
     correctly negative; SignalMessage validation then rejects it,
     which is the intentional kill path.

When the recompute logic in ep_intel.py changes, mirror the change here.
"""

from __future__ import annotations

import pytest


def _recompute_edge(sig_side: str, fair_value: float, market_price: float) -> float:
    """Mirrors the branching logic at ep_intel.py:2014-2024.

    Kept as a standalone helper so the test doesn't need a live Redis
    connection or an event loop. The shape must match the production
    branch exactly — if you change one, change the other.
    """
    if sig_side == "yes":
        return fair_value - market_price
    elif sig_side == "no":
        return market_price - fair_value
    else:
        raise ValueError(f"unsupported side {sig_side!r}")


def _apply_bias(side: str, fair_value: float, market_price: float, bias: float):
    """Replicate the if-bias-nonzero gated recompute."""
    if bias == 0.0:
        return fair_value, None    # edge NOT recomputed
    new_fair = max(0.01, min(0.99, fair_value + bias))
    new_edge = _recompute_edge(side, new_fair, market_price)
    return new_fair, new_edge


def test_yes_signal_positive_bias_keeps_positive_edge():
    """YES bet with bias = +0.02 (confirmation) lifts fair_value, edge stays positive."""
    fair, edge = _apply_bias(side="yes", fair_value=0.55, market_price=0.40, bias=0.02)
    assert fair == pytest.approx(0.57)
    assert edge == pytest.approx(0.17)   # 0.57 - 0.40, positive — YES underpriced


def test_no_signal_negative_bias_keeps_positive_edge():
    """NO bet with bias = -0.04 (surprise) drops fair_value, edge stays positive.

    This is the bug case: pre-fix this returned a negative edge because
    the YES formula was applied. Now: market_price - fair_value gives
    the correct sign.
    """
    fair, edge = _apply_bias(side="no", fair_value=0.14, market_price=0.20, bias=-0.04)
    assert fair == pytest.approx(0.10)
    assert edge == pytest.approx(0.10)   # 0.20 - 0.10, positive — YES overpriced (good for NO)


def test_zero_bias_does_not_recompute_edge():
    """When bias is exactly 0.0 the gate is False — edge stays at the scanner-emitted value.

    This pins the silent path the bug used to hide behind: for any signal
    whose series has no resolutions in ep:resolutions (returns 0.0), the
    recompute is skipped entirely.
    """
    fair, edge = _apply_bias(side="no", fair_value=0.14, market_price=0.96, bias=0.0)
    assert fair == 0.14
    assert edge is None    # sentinel: recompute did not run


def test_yes_signal_bias_drives_fair_below_market_produces_negative_edge():
    """When recency bias correctly inverts the signal direction, edge goes negative.

    A YES signal that started with fair > market (edge positive) but
    where a surprise bias drives fair below market should produce a
    negative edge. SignalMessage.__post_init__ then rejects it at the
    bus boundary — which is the intentional kill. This is NOT a bug;
    the test pins that we don't silently rescue the signal.
    """
    fair, edge = _apply_bias(side="yes", fair_value=0.30, market_price=0.40, bias=-0.05)
    assert fair == pytest.approx(0.25)
    assert edge == pytest.approx(-0.15)  # 0.25 - 0.40, negative — bus must reject


def test_unsupported_side_raises():
    """BTC/CME 'buy'/'sell' signals shouldn't reach this code path; if one ever does,
    fail loudly rather than silently apply a wrong formula."""
    with pytest.raises(ValueError, match="unsupported side"):
        _recompute_edge("buy", 0.5, 0.5)
