"""tests/test_market_snapshot.py — Engineering A.1 market data primitives."""

from __future__ import annotations

import time

import pytest

from ep_market_snapshot import (
    append_trade,
    snapshot_median_yes_price,
    snapshot_count,
    is_websocket_stale,
    reset_for_test,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_for_test()
    yield
    reset_for_test()


def test_append_and_median():
    now = time.time()
    for p in [0.40, 0.42, 0.44, 0.46, 0.48]:
        append_trade("KXMVE-TEST", p, ts_unix=now)
    m = snapshot_median_yes_price("KXMVE-TEST", window_hours=1.0, now_unix=now + 60)
    assert abs(m - 0.44) < 1e-9


def test_median_requires_min_trades():
    now = time.time()
    append_trade("KXMVE-TEST", 0.40, ts_unix=now)
    append_trade("KXMVE-TEST", 0.42, ts_unix=now)
    # Only 2 trades, min default 3 → None
    m = snapshot_median_yes_price("KXMVE-TEST", window_hours=1.0, now_unix=now + 1)
    assert m is None


def test_window_excludes_old_trades():
    now = time.time()
    # 5 trades 2 hours ago (out of 1h window), 3 trades within last 30min
    for p in [0.20, 0.22, 0.24, 0.26, 0.28]:
        append_trade("KXMVE-TEST", p, ts_unix=now - 7200)
    for p in [0.60, 0.62, 0.64]:
        append_trade("KXMVE-TEST", p, ts_unix=now - 1800)
    # Median over last 1h should reflect ONLY the recent 3 trades
    m = snapshot_median_yes_price("KXMVE-TEST", window_hours=1.0, now_unix=now)
    assert abs(m - 0.62) < 1e-9


def test_invalid_price_dropped():
    now = time.time()
    for p in [0.0, 1.0, -0.5, 1.5]:
        append_trade("KXMVE-TEST", p, ts_unix=now)
    assert snapshot_count("KXMVE-TEST", window_hours=1.0, now_unix=now + 1) == 0


def test_count_in_window():
    now = time.time()
    for p in [0.30, 0.32, 0.34, 0.36, 0.38, 0.40]:
        append_trade("KXMVE-TEST", p, ts_unix=now)
    assert snapshot_count("KXMVE-TEST", window_hours=1.0, now_unix=now + 60) == 6


def test_ring_buffer_age_cutoff():
    """Trades older than _RING_HOURS (26h) are dropped on append."""
    now = time.time()
    # Insert an old trade
    append_trade("KXMVE-TEST", 0.30, ts_unix=now - (27 * 3600))
    # Now insert recent ones; the old one should be popleft'd
    for p in [0.50, 0.52, 0.54]:
        append_trade("KXMVE-TEST", p, ts_unix=now)
    # Counting full 26h window should NOT include the 27h-old trade
    assert snapshot_count("KXMVE-TEST", window_hours=26.0, now_unix=now + 1) == 3


def test_websocket_stale_detection():
    now = time.time()
    # Before any trade, stale check returns False (cold-start grace).
    assert is_websocket_stale(threshold_s=60, now_unix=now) is False
    append_trade("KXMVE-TEST", 0.50, ts_unix=now)
    # Right after trade — not stale.
    assert is_websocket_stale(threshold_s=60, now_unix=now + 30) is False
    # 90s later, threshold 60s → stale.
    assert is_websocket_stale(threshold_s=60, now_unix=now + 90) is True


def test_unknown_ticker_returns_none():
    assert snapshot_median_yes_price("KXNEVER-SEEN", window_hours=1.0) is None
    assert snapshot_count("KXNEVER-SEEN", window_hours=1.0) == 0
