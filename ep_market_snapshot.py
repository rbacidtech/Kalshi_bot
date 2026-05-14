"""In-memory market trade history + snapshot helpers — Engineering A.1.

Per-market deque of recent trades (26h ring buffer per Engineering A.1).
Used by Phase 2 strategies that need median yes_price over a time window
(KXMVE longshot, weather longshot, A1 mention markets — all of §4.1-§4.5).

Snapshot semantics (Engineering A.1 §):
  - `statistics.median()` exactly — backtest parity verified vs DuckDB
    MEDIAN aggregate (Becker 5M-row sample, 0.001 precision).
  - Minimum 3 trades in window to compute a snapshot. Below that, returns
    None and the calling strategy SKIPs the market (don't fabricate from
    insufficient data).
  - Frozen-view caching is the strategy's responsibility — call this
    module's `snapshot_median_yes_price(ticker, window_hours)` once per
    cycle per market and cache the result for that cycle.

Storage:
  - In-memory dict: `{ticker: deque[(timestamp_unix, yes_price)]}`.
    Max length 2000 per market (hard cap on retained trades; matches
    Engineering A.1 §).
  - SQLite checkpoint every 60s (deferred — defaults to in-memory only
    in MVP; restart loses history, cold-start REST refresh repopulates).

Staleness halt:
  - `is_websocket_stale(threshold_s=60)` returns True if no append in
    threshold seconds. Caller (e.g. `_business_health_loop`) translates
    that to HALT_TRADING per Engineering A.1 §3 (60s no-message → soft halt).
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)


_RING_MAX = 2000                # Engineering A.1: deque(maxlen=2000) per market
_RING_HOURS = 26.0              # Engineering A.1: covers all strategy windows


# Module-level state — per-market trade history.
# {ticker: deque[(unix_ts: float, yes_price: float)]}
_history: dict[str, deque] = {}
# Last-append timestamp (for staleness detection)
_last_append_ts: float = 0.0


def append_trade(ticker: str, yes_price: float, ts_unix: Optional[float] = None) -> None:
    """Append a trade to the per-market ring buffer.

    Called from the WebSocket trade-event handler (per-trade) or from the
    REST cold-start backfill. `yes_price` in [0.0, 1.0] fraction. Out-of-
    range prices are silently dropped (defensive — the data pipeline §1.4
    invariant is yes_price ∈ (0, 1)).
    """
    if not ticker:
        return
    if not (0.0 < yes_price < 1.0):
        return
    global _last_append_ts
    now = ts_unix if ts_unix is not None else time.time()
    buf = _history.get(ticker)
    if buf is None:
        buf = deque(maxlen=_RING_MAX)
        _history[ticker] = buf
    buf.append((float(now), float(yes_price)))
    if now > _last_append_ts:
        _last_append_ts = now

    # Trim by age — anything older than _RING_HOURS hours.
    cutoff = now - (_RING_HOURS * 3600)
    while buf and buf[0][0] < cutoff:
        buf.popleft()


def snapshot_median_yes_price(
    ticker: str,
    window_hours: float = 1.0,
    min_trades: int = 3,
    now_unix: Optional[float] = None,
) -> Optional[float]:
    """Median yes_price over the last `window_hours` hours for `ticker`.

    Returns None when:
      - ticker has no recorded trades
      - fewer than `min_trades` trades within window

    Per Engineering A.1: `statistics.median()` (not `np.median`) for
    backtest parity. min_trades default 3.
    """
    if not ticker or window_hours <= 0:
        return None
    buf = _history.get(ticker)
    if buf is None or not buf:
        return None
    now = now_unix if now_unix is not None else time.time()
    cutoff = now - (window_hours * 3600)
    in_window = [price for ts, price in buf if ts >= cutoff]
    if len(in_window) < min_trades:
        return None
    return float(statistics.median(in_window))


def snapshot_count(ticker: str, window_hours: float = 1.0,
                   now_unix: Optional[float] = None) -> int:
    """Number of trades in window for `ticker`. Useful for thin-trading filters."""
    if not ticker or window_hours <= 0:
        return 0
    buf = _history.get(ticker)
    if buf is None or not buf:
        return 0
    now = now_unix if now_unix is not None else time.time()
    cutoff = now - (window_hours * 3600)
    return sum(1 for ts, _ in buf if ts >= cutoff)


def is_websocket_stale(threshold_s: float = 60.0, now_unix: Optional[float] = None) -> bool:
    """True if no trade has been appended in `threshold_s` seconds.

    Engineering A.1: 60s no-message → soft halt. Caller invokes from
    `_business_health_loop` every cycle; trips HALT_TRADING when this
    returns True (and HALT_TRADING isn't already 1).

    Returns False when no trades have ever been recorded (cold-start
    grace — don't halt before the first trade arrives).
    """
    if _last_append_ts <= 0:
        return False
    now = now_unix if now_unix is not None else time.time()
    return (now - _last_append_ts) > threshold_s


def get_tracked_tickers() -> list[str]:
    """List of tickers with at least one trade in history. For operator inspection."""
    return list(_history.keys())


def reset_for_test() -> None:
    """Clear all history. ONLY for tests — never call in production."""
    global _last_append_ts
    _history.clear()
    _last_append_ts = 0.0
