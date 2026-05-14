"""tests/test_hot_path_h2h.py — Engineering A.3 hot-path primitive."""

from __future__ import annotations

import time

from strategies.hot_path.h2h_fast import BUDGET_MS_P95, fee_cents_at, detect_arb


def test_fee_table_endpoints():
    """Sanity on fee lookup at the parabola's edges + peak."""
    # At p=50¢ the parabola peaks: 7% × 0.5 × 0.5 = 0.0175 → ceil → 0.02
    assert fee_cents_at(50) == 0.02
    # At p=5¢: 7% × 0.05 × 0.95 = 0.003325 → ceil → 0.01
    assert fee_cents_at(5) == 0.01
    # At p=95¢: same (symmetric) → 0.01
    assert fee_cents_at(95) == 0.01
    # Out of range
    assert fee_cents_at(0) == 0.0
    assert fee_cents_at(100) == 0.0


def test_detect_arb_below_threshold():
    """sum = 96, threshold 98 → arb fires."""
    out = detect_arb(yes_ask_a_cents=48, yes_ask_b_cents=48, threshold_cents=98)
    assert out is not None
    total, gross, net, contracts = out
    assert total == 96
    assert gross == 4
    # Fee at 48¢: ceil(0.07 × 0.48 × 0.52 × 100) / 100 = ceil(1.7472)/100 = 0.02
    # 2 × $0.02 = $0.04 = 4 sub-cent units → 4¢ in display
    # net = gross - int(round((fee_a + fee_b) * 100)) = 4 - 4 = 0
    assert net == 0
    assert contracts == 2  # gross // 2


def test_detect_arb_at_threshold_returns_none():
    """sum = 98 exactly, threshold 98 → no arb (strict <)."""
    out = detect_arb(98 // 2, 98 // 2, threshold_cents=98)
    assert out is None


def test_detect_arb_above_threshold_returns_none():
    out = detect_arb(50, 50, threshold_cents=98)
    assert out is None


def test_detect_arb_invalid_prices_return_none():
    assert detect_arb(0, 50) is None
    assert detect_arb(100, 50) is None
    assert detect_arb(50, -1) is None


def test_budget_constant_set():
    """Engineering A.3 budget for decision-only logic."""
    assert BUDGET_MS_P95 == 5.0


def test_detect_arb_perf_budget():
    """detect_arb on a tight inner loop must run well within 5ms p95."""
    n = 50_000
    t0 = time.perf_counter()
    for _ in range(n):
        detect_arb(48, 48, 98)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    per_call_us = (elapsed_ms / n) * 1000
    # 50K calls in well under 5ms p95 → per-call must be ~0.1us range
    # Loose bound: per-call < 50us (very generous; typical is <1us)
    assert per_call_us < 50, f"detect_arb too slow: {per_call_us:.2f}us per call"
