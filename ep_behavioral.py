"""
ep_behavioral.py — Late-money and recency-bias detectors.

Both detectors are lightweight, in-process, and require no external APIs.

late_money_spike(ticker, current_volume)
    Returns True when per-cycle volume gain for a market is > 3× its own
    rolling average — indicating retail pile-in just before resolution.
    Caller should *reduce* signal confidence when this fires.

recency_bias_adj(series, bus) -> float
    Checks ep:resolutions Redis hash for the series' last outcome.
    Returns -0.04 if the previous resolution was a surprise (market was
    >70% confident but wrong, or <30% confident but right).
    Returns 0.0 when no history is available (fail-safe).

Volume history is kept in an in-memory defaultdict of deques — no Redis
reads needed per cycle.  Max 240 entries per ticker (~4 h at 60 s/cycle).
"""

import json
import time
from collections import defaultdict, deque
from typing import Optional

from ep_config import log

# ── Volume history ─────────────────────────────────────────────────────────────

_LATE_MONEY_MULTIPLIER = 3.0   # spike threshold vs rolling avg delta
_MIN_HISTORY_SAMPLES   = 10    # need at least this many cycles before firing

# ticker → deque of (timestamp_s, total_volume_float)
_vol_history: dict = defaultdict(lambda: deque(maxlen=240))


def record_volume(ticker: str, volume: float) -> None:
    """
    Call once per cycle for every market in the scan.
    Appends (now, volume) to the ticker's history ring.
    """
    _vol_history[ticker].append((time.time(), float(volume or 0)))


def is_late_money_spike(ticker: str, current_volume: float) -> bool:
    """
    Returns True if the volume increase this cycle is > LATE_MONEY_MULTIPLIER
    times the average per-cycle volume increase over recent history.

    A spike ≠ high volume — it means volume is *accelerating* faster than
    usual, which often signals momentum-chasing retail flow just before
    resolution.  Fade this by reducing signal confidence.
    """
    hist = _vol_history[ticker]
    if len(hist) < _MIN_HISTORY_SAMPLES:
        return False

    vols = [v for _, v in hist]

    # Per-cycle volume deltas (negative clipped to 0 — resets happen)
    deltas = [max(0.0, vols[i] - vols[i - 1]) for i in range(1, len(vols))]
    if not deltas:
        return False

    avg_delta = sum(deltas) / len(deltas)
    if avg_delta < 1.0:
        # Market has essentially zero volume history — don't trigger
        return False

    current_delta = max(0.0, current_volume - vols[-1])
    return current_delta > _LATE_MONEY_MULTIPLIER * avg_delta


# ── Recency bias ───────────────────────────────────────────────────────────────

async def recency_bias_adj(series: str, bus) -> float:
    """
    Pull the last resolution for this series from ep:resolutions Redis hash.
    Returns a fair-value probability adjustment:
      -0.04  if last outcome was a surprise (market was confident but wrong)
      +0.02  if last outcome was confirmed (market was right at high confidence)
       0.00  otherwise (default / no data)

    ep:resolutions format per series key:
      {"outcomes": [{"price_before": int, "resolved_yes": bool, "ts": int}, ...]}
    Written by ep_resolution_db.py when a market resolves.
    """
    try:
        raw = await bus._r.hget("ep:resolutions", series)
        if not raw:
            return 0.0

        data     = json.loads(raw)
        outcomes = data.get("outcomes", [])
        if not outcomes:
            return 0.0

        last          = outcomes[-1]
        price_before  = int(last.get("price_before", 50))
        resolved_yes  = last.get("resolved_yes")

        if resolved_yes is None:
            return 0.0

        # Surprise: market was >70% confident but resolved opposite way
        high_conf_wrong = (price_before > 70 and not resolved_yes) or \
                          (price_before < 30 and resolved_yes)

        # Confirmed: market was >70% confident and resolved correctly
        high_conf_right = (price_before > 70 and resolved_yes) or \
                          (price_before < 30 and not resolved_yes)

        if high_conf_wrong:
            log.debug(
                "Recency bias: %s last outcome was a surprise "
                "(price_before=%d¢, resolved_yes=%s) → -0.04 adj",
                series, price_before, resolved_yes,
            )
            return -0.04

        if high_conf_right:
            return +0.02   # mild positive reinforcement

        return 0.0

    except Exception as exc:
        log.debug("recency_bias_adj error for %s: %s", series, exc)
        return 0.0
