"""
tests/test_unrealized_staleness.py — Unit tests for the price-staleness gate
on unrealized P&L (api/routers/positions.py:_compute_pnl).

The gate prevents unrealized P&L from silently freezing at the last-published
value when the upstream price scanner halts: if a price record's `ts_us` is
older than `UNREALIZED_PRICE_MAX_AGE_S` (default 600s), `_compute_pnl` returns
`(None, "stale")` so the caller can exclude it from the total and surface a
stale-price counter in the portfolio response.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.routers.positions import _compute_pnl, _MAX_PRICE_AGE_S


def _pos(ticker: str = "KXFED-26JUN-T4.25", side: str = "yes",
         contracts: int = 10, entry_cents: int = 50) -> dict:
    return {
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "entry_cents": entry_cents,
    }


def test_fresh_price_returns_pnl():
    """A price record with a recent ts_us yields a real pnl + 'ok' status."""
    now_us = int(time.time() * 1_000_000)
    prices = {
        "KXFED-26JUN-T4.25": {
            "yes_price": 60,
            "ts_us":     now_us,
        },
    }
    pnl, status = _compute_pnl(_pos(), prices)
    assert status == "ok"
    # YES, entry=50, current=60, contracts=10 → +100
    assert pnl == 100


def test_stale_price_returns_none():
    """A price record older than _MAX_PRICE_AGE_S returns (None, 'stale')."""
    stale_age_s = _MAX_PRICE_AGE_S + 100  # comfortably past the gate
    stale_ts_us = int((time.time() - stale_age_s) * 1_000_000)
    prices = {
        "KXFED-26JUN-T4.25": {
            "yes_price": 60,
            "ts_us":     stale_ts_us,
        },
    }
    pnl, status = _compute_pnl(_pos(), prices)
    assert status == "stale"
    assert pnl is None


def test_missing_price_returns_none():
    """A position with no price record at all returns (None, 'missing')."""
    pnl, status = _compute_pnl(_pos(), prices={})
    assert status == "missing"
    assert pnl is None


def test_no_ts_us_treated_as_fresh():
    """
    Back-compat: price records without a ts_us field (older publishers) are
    treated as fresh — staleness only fires when ts_us is present and old.
    """
    prices = {
        "KXFED-26JUN-T4.25": {"yes_price": 60},  # no ts_us key
    }
    pnl, status = _compute_pnl(_pos(), prices)
    assert status == "ok"
    assert pnl == 100


def test_no_side_position_uses_inverse_pnl():
    """Sanity: NO-side positions use (entry - current) * contracts."""
    now_us = int(time.time() * 1_000_000)
    prices = {
        "KXFED-26JUN-T4.25": {"yes_price": 40, "ts_us": now_us},
    }
    pnl, status = _compute_pnl(_pos(side="no"), prices)
    assert status == "ok"
    # NO, entry=50, current=40, contracts=10 → +100
    assert pnl == 100
