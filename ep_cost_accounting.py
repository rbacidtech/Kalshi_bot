"""Per-strategy cost accounting — Engineering B.5.

Allocates infrastructure costs ($924/yr default = $77/mo VPS+backup+probe)
across strategies by activity weight, computes net contribution, and
answers the annual kill-or-continue question.

Activity-based weight (Engineering B.5 §):
    w = 0.40 × (signals_strategy / signals_total)
      + 0.40 × (fills_strategy   / fills_total)
      + 0.20 × (position_dollar_days_strategy / position_dollar_days_total)

Three cost categories:
  1. Direct trading — already in fees/slippage (Phase 1.4 S.1)
  2. Allocated infrastructure — this module
  3. Opportunity cost — T-bill yield ($67/yr on $1,500 at 4.5%) +
     operator time (10 hrs/mo × $40/hr = $4,800/yr); often ignored but
     dominates the economic case at retail scale

90-day payback check + annual kill-or-continue threshold:
  Bot must generate > $5,792/yr to break even on FULL economics
  (= $924 infra + $67 T-bill + $4,800 operator time + ~$1 buffer).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_SQLITE_PATH = Path(os.environ.get("EP_COSTS_SQLITE", "/var/lib/edgepulse/costs.sqlite"))

# Engineering B.5 defaults (cents)
_DEFAULT_INFRA_MONTHLY_CENTS = 7_700        # $77/mo
_DEFAULT_TBILL_ANNUAL_CENTS = 6_750         # $67.50/yr at 4.5% on $1,500
_DEFAULT_OPERATOR_ANNUAL_CENTS = 480_000    # $4,800/yr
_DEFAULT_BREAKEVEN_CENTS = 579_200          # $5,792/yr full economics


def _ensure_db() -> sqlite3.Connection:
    _SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema() -> None:
    conn = _ensure_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cost_ledger (
                month         TEXT PRIMARY KEY,    -- YYYY-MM
                infra_cents   INTEGER NOT NULL,
                notes         TEXT
            );
            CREATE TABLE IF NOT EXISTS monthly_attribution (
                month       TEXT NOT NULL,
                strategy    TEXT NOT NULL,
                signals     INTEGER NOT NULL DEFAULT 0,
                fills       INTEGER NOT NULL DEFAULT 0,
                pos_dollar_days REAL NOT NULL DEFAULT 0,
                cost_share_cents INTEGER NOT NULL DEFAULT 0,
                gross_pnl_cents  INTEGER NOT NULL DEFAULT 0,
                net_pnl_cents    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (month, strategy)
            );
            CREATE INDEX IF NOT EXISTS idx_monthly_attribution_strategy
                ON monthly_attribution (strategy, month DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_infra_cost(month: str, infra_cents: int, notes: str = "") -> None:
    """Operator enters monthly infra cost (`YYYY-MM`, cents)."""
    conn = _ensure_db()
    try:
        conn.execute(
            """
            INSERT INTO cost_ledger (month, infra_cents, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
              infra_cents = excluded.infra_cents,
              notes       = excluded.notes
            """,
            (month, int(infra_cents), notes),
        )
        conn.commit()
    finally:
        conn.close()


def compute_attribution(
    month: str,
    by_strategy: dict[str, dict],
    infra_cents: Optional[int] = None,
) -> dict[str, dict]:
    """Allocate `infra_cents` across strategies using the B.5 weight formula.

    `by_strategy`: {strategy: {signals, fills, pos_dollar_days, gross_pnl_cents}}.
    `infra_cents` falls back to default $77/mo when None.

    Returns dict {strategy: {**input, cost_share_cents, net_pnl_cents}}.
    Idempotent — re-run with the same inputs produces identical output.
    """
    if infra_cents is None:
        infra_cents = _DEFAULT_INFRA_MONTHLY_CENTS
    totals = {
        "signals":         sum(int(d.get("signals", 0)) for d in by_strategy.values()),
        "fills":           sum(int(d.get("fills", 0)) for d in by_strategy.values()),
        "pos_dollar_days": sum(float(d.get("pos_dollar_days", 0)) for d in by_strategy.values()),
    }
    result: dict[str, dict] = {}
    for strat, stats in by_strategy.items():
        sig = int(stats.get("signals", 0))
        fil = int(stats.get("fills", 0))
        pdd = float(stats.get("pos_dollar_days", 0))
        gross = int(stats.get("gross_pnl_cents", 0))

        w = 0.0
        if totals["signals"] > 0:         w += 0.40 * (sig / totals["signals"])
        if totals["fills"] > 0:           w += 0.40 * (fil / totals["fills"])
        if totals["pos_dollar_days"] > 0: w += 0.20 * (pdd / totals["pos_dollar_days"])

        cost_share = int(round(infra_cents * w))
        net_pnl = gross - cost_share
        result[strat] = {
            **stats,
            "cost_share_cents": cost_share,
            "net_pnl_cents":    net_pnl,
            "weight":           round(w, 4),
        }
    return result


def persist_attribution(month: str, attribution: dict[str, dict]) -> None:
    """Write computed monthly attribution to SQLite."""
    conn = _ensure_db()
    try:
        cur = conn.cursor()
        for strat, row in attribution.items():
            cur.execute(
                """
                INSERT INTO monthly_attribution
                  (month, strategy, signals, fills, pos_dollar_days,
                   cost_share_cents, gross_pnl_cents, net_pnl_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(month, strategy) DO UPDATE SET
                  signals          = excluded.signals,
                  fills            = excluded.fills,
                  pos_dollar_days  = excluded.pos_dollar_days,
                  cost_share_cents = excluded.cost_share_cents,
                  gross_pnl_cents  = excluded.gross_pnl_cents,
                  net_pnl_cents    = excluded.net_pnl_cents
                """,
                (
                    month, strat,
                    int(row.get("signals", 0)),
                    int(row.get("fills", 0)),
                    float(row.get("pos_dollar_days", 0)),
                    int(row.get("cost_share_cents", 0)),
                    int(row.get("gross_pnl_cents", 0)),
                    int(row.get("net_pnl_cents", 0)),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def payback_status(start_date: date) -> dict[str, Any]:
    """90-day payback projection. Engineering B.5 §:
      ON_TRACK / CONVERGING / BELOW_TRAJECTORY / POST_90D_NOT_PAID_BACK
    """
    today = datetime.now(timezone.utc).date()
    days_elapsed = (today - start_date).days
    conn = _ensure_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT SUM(gross_pnl_cents), SUM(cost_share_cents) FROM monthly_attribution")
        row = cur.fetchone() or (0, 0)
        gross = int(row[0] or 0)
        cost = int(row[1] or 0)
    finally:
        conn.close()

    # Linear-required trajectory: must reach $924 by day 90 = $10.27/day
    daily_required = _DEFAULT_INFRA_MONTHLY_CENTS * 12 / 365
    cumulative_required = int(daily_required * max(0, days_elapsed))

    if days_elapsed >= 90:
        status = "POST_90D_PAID_BACK" if gross >= _DEFAULT_INFRA_MONTHLY_CENTS * 3 else "POST_90D_NOT_PAID_BACK"
    elif gross >= cumulative_required:
        status = "ON_TRACK"
    elif gross >= 0.5 * cumulative_required:
        status = "CONVERGING"
    else:
        status = "BELOW_TRAJECTORY"

    return {
        "days_elapsed":         days_elapsed,
        "gross_pnl_cents":      gross,
        "cost_share_cents":     cost,
        "net_pnl_cents":        gross - cost,
        "cumulative_required_cents": cumulative_required,
        "status":               status,
    }


def annual_kill_or_continue() -> dict[str, Any]:
    """Engineering B.5 annual review — three metrics:
      1. Net over T-bill
      2. Net over full opportunity costs (incl. operator time)
      3. YoY trend (if 2+ full years of data)
    """
    conn = _ensure_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT SUM(gross_pnl_cents), SUM(cost_share_cents) FROM monthly_attribution")
        row = cur.fetchone() or (0, 0)
        gross = int(row[0] or 0)
        cost = int(row[1] or 0)
    finally:
        conn.close()
    net_after_infra = gross - cost
    net_after_tbill = net_after_infra - _DEFAULT_TBILL_ANNUAL_CENTS
    net_after_full = net_after_tbill - _DEFAULT_OPERATOR_ANNUAL_CENTS
    return {
        "gross_pnl_cents":            gross,
        "infra_cost_cents":           cost,
        "net_after_infra_cents":      net_after_infra,
        "net_after_tbill_cents":      net_after_tbill,
        "net_after_full_cents":       net_after_full,
        "breakeven_threshold_cents":  _DEFAULT_BREAKEVEN_CENTS,
        "beats_tbill":                net_after_tbill > 0,
        "beats_full_opportunity":     net_after_full > 0,
    }
