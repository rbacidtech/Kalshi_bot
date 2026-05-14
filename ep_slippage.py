"""Per-strategy slippage aggregation — Phase 1.4 S.1.3 of EdgePulse_Migration_Plan_2026.md.

Reads the slippage_cents column on executions (added by S.1.1 migration
7a3b8f9c2e6d) and computes per-strategy mean / median / p95 over a configurable
window. Joined to the signals table on signal_id to pick up the strategy
label (executions don't store strategy directly).

Used by:
  - Future B.2 slippage measurement dashboard
  - Future Phase 4 quarterly review for tuning-loop decisions
  - Operator on-demand: `python -m ep_slippage [days]`
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

import asyncpg


_DEFAULT_DSN = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")


async def get_slippage_by_strategy(days: int = 7, dsn: Optional[str] = None) -> dict[str, dict]:
    """Return per-strategy slippage statistics over the last `days`.

    Returns a dict keyed by strategy name; each value:
        {
            "n":          int,    # number of filled fills with slippage
            "mean_cents": float,  # mean slippage (positive = adverse)
            "p50_cents":  int,    # median
            "p95_cents":  int,    # 95th-percentile (tail)
            "sum_cents":  int,    # total slippage in window
        }

    Empty dict when there are no qualifying rows in window.
    """
    actual_dsn = dsn or _DEFAULT_DSN
    if not actual_dsn:
        return {}

    pool: Optional[asyncpg.Pool] = None
    try:
        pool = await asyncpg.create_pool(actual_dsn, min_size=1, max_size=2)
    except Exception:
        return {}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(s.strategy, '<unknown>')               AS strategy,
                    COUNT(*)                                        AS n,
                    AVG(e.slippage_cents)                           AS mean_cents,
                    PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY e.slippage_cents) AS p50_cents,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY e.slippage_cents) AS p95_cents,
                    SUM(e.slippage_cents)                           AS sum_cents
                FROM executions e
                LEFT JOIN signals s ON s.signal_id = e.signal_id
                WHERE e.status = 'filled'
                  AND e.slippage_cents IS NOT NULL
                  AND e.reported_at >= NOW() - make_interval(days => $1)
                GROUP BY s.strategy
                ORDER BY n DESC
                """,
                days,
            )
    finally:
        await pool.close()

    return {
        r["strategy"]: {
            "n":          int(r["n"]),
            "mean_cents": float(r["mean_cents"]) if r["mean_cents"] is not None else 0.0,
            "p50_cents":  int(r["p50_cents"])    if r["p50_cents"]  is not None else 0,
            "p95_cents":  int(r["p95_cents"])    if r["p95_cents"]  is not None else 0,
            "sum_cents":  int(r["sum_cents"])    if r["sum_cents"]  is not None else 0,
        }
        for r in rows
    }


def _format_report(by_strategy: dict[str, dict], days: int) -> str:
    if not by_strategy:
        return (
            f"No filled executions with slippage_cents IS NOT NULL "
            f"in the last {days} day(s).\n"
            "Either the bot hasn't traded, or all fills predate the Phase 1.4 "
            "S.1.1 migration (slippage_cents column added 2026-05-14)."
        )
    lines = [
        f"Per-strategy slippage over last {days} day(s)  "
        f"(positive = adverse, paid more than quoted; negative = favorable)",
        "",
        f"{'Strategy':32s}  {'n':>5s}  {'mean':>10s}  {'p50':>8s}  {'p95':>8s}  {'sum':>10s}",
        "-" * 86,
    ]
    for strat in sorted(by_strategy, key=lambda s: -by_strategy[s]["n"]):
        st = by_strategy[strat]
        lines.append(
            f"{strat:32s}  {st['n']:>5d}  "
            f"{st['mean_cents']:>+10.2f}  "
            f"{st['p50_cents']:>+8d}  "
            f"{st['p95_cents']:>+8d}  "
            f"{st['sum_cents']:>+10d}"
        )
    return "\n".join(lines)


async def _cli_main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    by_strategy = await get_slippage_by_strategy(days=days)
    print(_format_report(by_strategy, days))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli_main()))
