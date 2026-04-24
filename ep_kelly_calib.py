"""
ep_kelly_calib.py — Empirical Kelly recalibration from Postgres terminal_trades view.

Runs as a background async task every 4 hours. Queries resolved trades from the last
90 days, buckets by stated edge, computes empirical win rate + payoff ratio per bucket,
and writes the calibrated Kelly fraction to Redis (ep:kelly_calib) and to an in-memory
dict for zero-latency reads during signal sizing.

Usage in ep_exec.py:
    from ep_kelly_calib import get_calibrated_kelly, kelly_calib_loop
    _cal = get_calibrated_kelly(sig.edge)   # None if insufficient data
    if _cal is not None:
        risk_engine._kalshi.cfg.kelly_fraction = _cal

LLM-set llm_kelly_fraction still takes precedence over empirical calibration:
check llm_kelly FIRST, then fall through to empirical.
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional, Tuple

log = logging.getLogger("edgepulse.kelly_calib")

# Edge buckets: (lo_inclusive, hi_exclusive)
_BUCKETS: list[Tuple[float, float]] = [
    (0.05, 0.10),
    (0.10, 0.15),
    (0.15, 0.20),
    (0.20, 0.30),
    (0.30, 1.00),
]
_MIN_SAMPLE        = 20          # minimum terminal trades to use a bucket's calibration
_CALIB_LOOKBACK    = 90          # days
_CALIB_INTERVAL_S  = 4 * 3600   # recalibrate every 4 hours
_MAX_KELLY         = 0.50        # hard cap — never let calibration size above 50%
_MIN_KELLY         = 0.01        # floor — never fully zero out sizing from calibration alone

# In-memory cache — written by the calib loop, read by get_calibrated_kelly()
# Dict maps "lo-hi" → calibrated kelly fraction (e.g. "0.10-0.15" → 0.18)
_calib: Dict[str, float] = {}
_calib_updated_at: float = 0.0

# Per-strategy win-rate calibration: model_source → confidence multiplier
# Values > 1.0 mean the strategy historically outperforms its stated confidence.
_strategy_conf_mult: dict[str, float] = {}


def _bucket_key(edge: float) -> Optional[str]:
    for lo, hi in _BUCKETS:
        if lo <= edge < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return None


def get_strategy_conf_mult(model_source: str) -> float:
    """Return empirical confidence multiplier for model_source. Default 1.0."""
    return _strategy_conf_mult.get(model_source, 1.0)


def get_calibrated_kelly(stated_edge: float) -> Optional[float]:
    """
    Return the empirically calibrated Kelly fraction for this edge level.
    Returns None if the bucket has insufficient historical data (< _MIN_SAMPLE trades)
    or if the calibration loop hasn't run yet.
    Callers should treat None as "use the configured default."
    """
    key = _bucket_key(stated_edge)
    if key is None:
        return None
    return _calib.get(key)


def _empirical_kelly(n: int, wins: int, sum_win_r: float, sum_loss_r: float) -> float:
    """
    Kelly fraction from empirical win rate and average payoff.
    f* = p - (1-p)/b  where b = avg_win_return / avg_loss_return.
    Clamped to [_MIN_KELLY, _MAX_KELLY].
    """
    if n < _MIN_SAMPLE:
        return 0.0
    p = wins / n
    losses = n - wins
    avg_win  = (sum_win_r  / wins)   if wins   > 0 else 1.0
    avg_loss = (sum_loss_r / losses) if losses > 0 else 1.0
    if avg_loss == 0:
        return _MAX_KELLY
    b = avg_win / avg_loss
    f = p - (1.0 - p) / b
    return max(_MIN_KELLY, min(_MAX_KELLY, f))


async def _compute_calibration(dsn: str) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Query terminal_trades and return ({bucket_key: kelly_fraction}, {model_source: conf_mult})."""
    try:
        import asyncpg
    except ImportError:
        log.debug("asyncpg not available — Kelly calibration disabled")
        return {}, {}

    try:
        conn = await asyncpg.connect(dsn, timeout=10)
    except Exception as exc:
        log.warning("kelly_calib: DB connect failed: %s", exc)
        return {}, {}

    # Bucket query: samples for per-edge Kelly calibration. Run in its own
    # try/except so a failure in the strategy-multiplier query below cannot
    # knock out the primary calibration.
    try:
        rows = await conn.fetch(f"""
            SELECT stated_edge, return_frac
            FROM terminal_trades
            WHERE exited_at > now() - INTERVAL '{_CALIB_LOOKBACK} days'
              AND stated_edge  IS NOT NULL
              AND return_frac  IS NOT NULL
              AND is_terminal  = true
        """)
    except Exception as exc:
        log.warning("kelly_calib: bucket query failed: %s", exc)
        rows = []

    # Strategy-multiplier query: terminal_trades view exposes the columns as
    # `strategy` (not model_source), `realized_pnl_cents` (not pnl_cents),
    # and `exited_at` (not closed_at). The historical names here used to raise
    # "column does not exist" on every tick and silently poisoned the whole
    # calibration result. Separate try/except so bucket calibration survives.
    try:
        strat_rows = await conn.fetch("""
            SELECT
                strategy,
                COUNT(*) as n,
                SUM(CASE WHEN realized_pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
                AVG(realized_pnl_cents) as avg_pnl
            FROM terminal_trades
            WHERE exited_at > NOW() - INTERVAL '90 days'
              AND strategy IS NOT NULL
              AND strategy != ''
            GROUP BY strategy
            HAVING COUNT(*) >= 10
        """)
    except Exception as exc:
        log.warning("kelly_calib: strategy query failed: %s", exc)
        strat_rows = []
    finally:
        await conn.close()

    # Aggregate per bucket
    bucket_n:        Dict[str, int]   = {}
    bucket_wins:     Dict[str, int]   = {}
    bucket_sum_win:  Dict[str, float] = {}
    bucket_sum_loss: Dict[str, float] = {}

    for row in rows:
        key = _bucket_key(float(row["stated_edge"]))
        if key is None:
            continue
        rf = float(row["return_frac"])
        bucket_n[key]        = bucket_n.get(key, 0) + 1
        bucket_wins[key]     = bucket_wins.get(key, 0)
        bucket_sum_win[key]  = bucket_sum_win.get(key, 0.0)
        bucket_sum_loss[key] = bucket_sum_loss.get(key, 0.0)
        if rf > 0:
            bucket_wins[key]    += 1
            bucket_sum_win[key] += rf
        else:
            bucket_sum_loss[key] += abs(rf)

    result = {}
    for key in bucket_n:
        n = bucket_n[key]
        if n < _MIN_SAMPLE:
            log.debug("kelly_calib: bucket %s has only %d samples (need %d) — skipping",
                      key, n, _MIN_SAMPLE)
            continue
        kf = _empirical_kelly(
            n, bucket_wins[key], bucket_sum_win[key], bucket_sum_loss[key],
        )
        win_rate = bucket_wins[key] / n
        log.info("kelly_calib: bucket=%s n=%d win_rate=%.3f kelly=%.4f",
                 key, n, win_rate, kf)
        result[key] = kf

    strat_mult: Dict[str, float] = {}
    for row in strat_rows:
        n        = int(row["n"])
        wins     = int(row["wins"])
        win_rate = wins / n
        if win_rate > 0.65 and n >= 20:
            mult = min(1.20, 1.0 + (win_rate - 0.55) * 1.0)
        elif win_rate < 0.40 and n >= 20:
            mult = max(0.60, 1.0 - (0.55 - win_rate) * 1.5)
        else:
            mult = 1.0
        strat_mult[row["strategy"]] = mult

    return result, strat_mult


async def kelly_calib_loop(bus, interval_s: int = _CALIB_INTERVAL_S) -> None:
    """
    Background task: recompute Kelly calibration every interval_s seconds.
    Writes to in-memory _calib dict (zero-latency reads) and Redis ep:kelly_calib
    (for dashboard observability).

    bus: RedisBus instance (for Redis writes)
    """
    global _calib, _calib_updated_at, _strategy_conf_mult

    dsn = os.environ.get("DATABASE_URL", "").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    if not dsn:
        log.info("DATABASE_URL not set — Kelly calibration loop disabled")
        return

    # First run after a short delay so exec is fully initialised
    await asyncio.sleep(30)

    while True:
        try:
            result, strat_mult = await _compute_calibration(dsn)
            if result:
                _calib = result
                _calib_updated_at = time.time()
                # Persist to Redis for observability (dashboard can read ep:kelly_calib)
                try:
                    calib_payload = {
                        "buckets":    {k: round(v, 4) for k, v in result.items()},
                        "updated_at": int(_calib_updated_at),
                        "lookback_days": _CALIB_LOOKBACK,
                    }
                    await bus._r.set(
                        "ep:kelly_calib",
                        json.dumps(calib_payload),
                        ex=86400 * 2,
                    )
                except Exception as exc:
                    log.debug("kelly_calib: Redis write failed: %s", exc)
            else:
                log.info("kelly_calib: no buckets with sufficient data — using configured defaults")
            if strat_mult:
                _strategy_conf_mult = strat_mult
                try:
                    await bus._r.hset("ep:kelly_calib:strategy", mapping={k: str(v) for k, v in strat_mult.items()})
                except Exception as exc:
                    log.debug("kelly_calib: strategy Redis write failed: %s", exc)
        except Exception as exc:
            log.warning("kelly_calib_loop error: %s", exc)

        await asyncio.sleep(interval_s)
