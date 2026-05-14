"""Per-strategy maker fill-rate tracking — Engineering S.1.

Engineering S.1 §9 mandate: track maker fill rate ≥70% per strategy. If a
strategy's maker rate drops below the threshold, the maker fee advantage
(+2.5% premium per Becker) evaporates and the strategy may flip to net-
negative. This module collects the data; B.4's quarterly tuning loop
acts on it (auto-switch to taker, threshold adjustment, etc.).

Storage:
  - ep:maker_outcome_count Redis hash, keys "{strategy}:{outcome}" where
    outcome ∈ {maker_filled, taker_filled, unfilled_expired, unfilled_cancelled}.
    Counts are cumulative; B.4 reads + diff vs prior-period snapshot.
  - Periodic rate report computed on demand: total/maker_filled.

Caller contract:
  - On limit-order placement at-bid (maker attempt): no immediate record
    — outcome is unknown until fill or expire.
  - On fill confirmation: record_outcome(strategy, "maker_filled" or "taker_filled")
    based on whether the fill price matched our bid (maker) or crossed (taker).
  - On expiry/cancel without fill: record_outcome(strategy, "unfilled_expired"
    or "unfilled_cancelled").

Threshold: 70% maker fill rate per Engineering S.1. Below that triggers
B.4 review (not auto-action — operator decision).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


_HASH_KEY = "ep:maker_outcome_count"
_MAKER_RATE_TARGET = 0.70
_MIN_SAMPLE_FOR_RATE = 50


async def record_outcome(
    bus_redis: Any,
    strategy: str,
    outcome: str,
) -> None:
    """Increment the (strategy, outcome) counter.

    `outcome` ∈ {"maker_filled", "taker_filled", "unfilled_expired",
    "unfilled_cancelled"}.
    """
    if not strategy:
        return
    if outcome not in ("maker_filled", "taker_filled",
                       "unfilled_expired", "unfilled_cancelled"):
        log.warning("record_outcome: unknown outcome %r for %s", outcome, strategy)
        return
    try:
        await bus_redis.hincrby(_HASH_KEY, f"{strategy}:{outcome}", 1)
    except Exception as exc:
        log.debug("record_outcome(%s, %s) failed: %s", strategy, outcome, exc)


async def get_fill_rate(
    bus_redis: Any,
    strategy: str,
) -> Optional[dict[str, Any]]:
    """Compute maker fill rate + supporting counts for a strategy.

    Returns dict with: {strategy, maker_filled, taker_filled,
    unfilled_expired, unfilled_cancelled, total_attempts,
    maker_rate, taker_rate, below_target}.  None if no data.
    """
    if not strategy:
        return None
    counters = {
        "maker_filled": 0,
        "taker_filled": 0,
        "unfilled_expired": 0,
        "unfilled_cancelled": 0,
    }
    try:
        for outcome in counters:
            raw = await bus_redis.hget(_HASH_KEY, f"{strategy}:{outcome}")
            if raw is not None:
                try:
                    counters[outcome] = int(raw)
                except (ValueError, TypeError):
                    pass
    except Exception as exc:
        log.debug("get_fill_rate(%s) failed: %s", strategy, exc)
        return None

    total = sum(counters.values())
    if total == 0:
        return None
    maker_rate = counters["maker_filled"] / total
    taker_rate = counters["taker_filled"] / total
    return {
        "strategy":             strategy,
        **counters,
        "total_attempts":       total,
        "maker_rate":           round(maker_rate, 4),
        "taker_rate":           round(taker_rate, 4),
        "below_target":         total >= _MIN_SAMPLE_FOR_RATE and maker_rate < _MAKER_RATE_TARGET,
        "target":               _MAKER_RATE_TARGET,
        "min_sample":           _MIN_SAMPLE_FOR_RATE,
    }


async def get_all_fill_rates(bus_redis: Any) -> dict[str, dict]:
    """Return fill-rate report for every strategy with recorded outcomes."""
    out: dict[str, dict] = {}
    try:
        all_keys = await bus_redis.hkeys(_HASH_KEY)
    except Exception as exc:
        log.debug("get_all_fill_rates failed: %s", exc)
        return out
    strategies = set()
    for k in all_keys or []:
        key = k.decode() if isinstance(k, bytes) else k
        if ":" in key:
            strategies.add(key.split(":", 1)[0])
    for strat in strategies:
        report = await get_fill_rate(bus_redis, strat)
        if report:
            out[strat] = report
    return out


# ── Maker-price helper for scanners ──────────────────────────────────────────

def maker_price_for_yes_buy(market: dict, fallback_price: float) -> float:
    """Return the limit price to place a maker YES buy at.

    Strategy: use yes_bid_dollars + 0¢ (post AT the bid; rests as maker until
    a taker hits). Falls back to `fallback_price` (typically the strategy's
    intended price) when yes_bid is unavailable. Caller should use this for
    Signal.market_price when constructing maker-first signals so the
    downstream executor places a resting limit instead of crossing the spread.

    Engineering S.1 §3 explicitly recommends "best bid + 0¢" over "-1¢" —
    on Kalshi sports markets the edge is 3-15% so losing 1¢ to bid-improve
    is 2% of edge given up.
    """
    try:
        yb = float(market.get("yes_bid_dollars") or 0)
    except (TypeError, ValueError):
        yb = 0.0
    if 0.01 <= yb <= 0.99:
        return yb
    return fallback_price


def maker_price_for_no_buy(market: dict, fallback_price: float) -> float:
    """Return the limit price (in YES-equivalent) for a maker NO buy.

    NO buy at no_bid is the analog of YES buy at yes_bid. We return the
    YES-equivalent (1 - no_bid_dollars) so Signal.market_price stays on
    the YES axis (Signal convention).
    """
    try:
        nb = float(market.get("no_bid_dollars") or 0)
    except (TypeError, ValueError):
        nb = 0.0
    if 0.01 <= nb <= 0.99:
        return 1.0 - nb
    return fallback_price
