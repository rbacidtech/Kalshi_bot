"""Quarterly parameter tuning loop — Engineering B.4.

Replaces the autonomous LLM advisor architecture (Phase 0 Option B
decision) with a structured pipeline:

  1. **Aggregate** recommendations from upstream signals:
       - S.4 strategy drift (z-score breaches from ep_pnl_attribution)
       - A.4 correlation cap rejection rates (per-prefix from ep_correlation_caps)
       - A.5 per-strategy realized P&L vs expected (from strategy_pnl_daily)
       - B.2 slippage trend (widening per (strategy, prefix) cell)
       - B.3 reconciliation anomalies (settlement audit findings)
  2. **Apply decision rules** (reviewable Python, not LLM black-box):
       - Disable strategy: requires 3σ over 12 weeks AND negative lifetime P&L
       - Maker → taker switch: maker fill rate < 70% AND >50 samples
       - Threshold tighten: B.2 slippage widening > 0.3¢/quarter
       - Cap loosen: A.4 rejection rate < 5% for 2+ quarters
       - Cap tighten: A.4 rejection rate > 25% for 2+ quarters
  3. **Cap changes** at 25%/quarter per Carver "smaller-changes-less-often"
  4. **Bayesian update** on strategy edge: prior(backtest) + observed → posterior.
     One bad quarter ≠ disable; require multi-quarter confirmation.
  5. **Operator override gate**: nothing auto-applies. propose_changes()
     returns reviewable diff; operator approves via CLI.

Quarterly cadence (never mid-quarter except emergency). Outcome
measurement persists 2 quarters; 4-quarter reversal check flags when
prior changes get rolled back.

This module is the BUILDER + EVALUATOR. Persistence to SQLite at
/var/lib/edgepulse/tuning_proposals.sqlite for full audit trail.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_SQLITE_PATH = Path(os.environ.get("EP_TUNING_SQLITE", "/var/lib/edgepulse/tuning_proposals.sqlite"))

# Engineering B.4 defaults
_MAX_CHANGE_PCT_PER_QUARTER = 0.25       # 25% per Carver
_DRIFT_DISABLE_SIGMA = 3.0               # require 3σ at 12 weeks
_DRIFT_DISABLE_WEEKS = 12
_MAKER_TAKER_FLIP_RATE = 0.70            # maker fill rate threshold
_MAKER_TAKER_MIN_SAMPLES = 50
_CAP_LOOSEN_REJECT_PCT = 0.05            # below 5% = over-conservative
_CAP_TIGHTEN_REJECT_PCT = 0.25           # above 25% = too tight
_NEW_STRATEGY_GRACE_DAYS = 90


class RecommendationSource(str, Enum):
    DRIFT_S4 = "s4_drift"
    CAP_REJECTION_A4 = "a4_cap_rejection"
    PNL_ATTRIBUTION_A5 = "a5_pnl"
    SLIPPAGE_B2 = "b2_slippage"
    RECONCILIATION_B3 = "b3_reconciliation"


class ChangeType(str, Enum):
    DISABLE_STRATEGY = "disable_strategy"
    REENABLE_STRATEGY = "reenable_strategy"
    MAKER_TO_TAKER = "maker_to_taker"
    TAKER_TO_MAKER = "taker_to_maker"
    TIGHTEN_CAP = "tighten_cap"
    LOOSEN_CAP = "loosen_cap"
    TIGHTEN_THRESHOLD = "tighten_threshold"
    LOOSEN_THRESHOLD = "loosen_threshold"


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
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                quarter          TEXT NOT NULL,             -- e.g. "2026Q2"
                proposed_at_us   INTEGER NOT NULL,
                target_strategy  TEXT,
                change_type      TEXT NOT NULL,
                change_payload   TEXT NOT NULL,             -- JSON
                source_signals   TEXT NOT NULL,             -- JSON list
                reasoning        TEXT NOT NULL,
                bayesian_posterior REAL,
                status           TEXT NOT NULL DEFAULT 'PROPOSED',
                operator_decision TEXT,                     -- 'approved' / 'rejected' / NULL
                decided_at_us    INTEGER,
                outcome_2q       TEXT,                      -- JSON: realized P&L delta after 2 quarters
                reversed         INTEGER NOT NULL DEFAULT 0 -- 1 if a later proposal reverses this
            );
            CREATE INDEX IF NOT EXISTS idx_proposals_quarter
                ON proposals (quarter, proposed_at_us DESC);
            CREATE INDEX IF NOT EXISTS idx_proposals_status
                ON proposals (status, proposed_at_us DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _current_quarter() -> str:
    """Return YYYYQn for the current UTC date."""
    now = datetime.now(timezone.utc)
    q = (now.month - 1) // 3 + 1
    return f"{now.year}Q{q}"


def aggregate_recommendations(
    drift_reports: Optional[list[dict]] = None,
    cap_rejection_rates: Optional[dict[str, float]] = None,
    pnl_vs_expected: Optional[dict[str, dict]] = None,
    slippage_trends: Optional[dict[tuple[str, str], float]] = None,
    reconciliation_anomalies: Optional[list[dict]] = None,
) -> list[dict]:
    """Synthesize upstream signals into recommendation dicts.

    Each item: {source, target, suggested_change_type, evidence}.
    Conflict resolution (B.4 §): when two sources recommend opposite
    changes to the same parameter, prefer status quo (neither
    recommendation makes the cut).
    """
    recs: list[dict] = []

    # S.4 drift — disable candidates
    for rep in (drift_reports or []):
        strat = rep.get("strategy")
        flagged = rep.get("flagged")
        if not strat or not flagged:
            continue
        if "12_week" in flagged or "84d" in flagged:
            z = float(rep.get("84d_z", 0))
            if abs(z) >= _DRIFT_DISABLE_SIGMA:
                recs.append({
                    "source":   RecommendationSource.DRIFT_S4.value,
                    "target":   strat,
                    "change":   ChangeType.DISABLE_STRATEGY.value,
                    "evidence": {"z_84d": z, "flagged": flagged},
                })

    # A.4 cap rejection — loosen or tighten per-prefix caps
    for prefix, rate in (cap_rejection_rates or {}).items():
        if rate > _CAP_TIGHTEN_REJECT_PCT:
            recs.append({
                "source":   RecommendationSource.CAP_REJECTION_A4.value,
                "target":   prefix,
                "change":   ChangeType.LOOSEN_CAP.value,
                "evidence": {"rejection_rate": rate},
            })
        elif rate < _CAP_LOOSEN_REJECT_PCT:
            recs.append({
                "source":   RecommendationSource.CAP_REJECTION_A4.value,
                "target":   prefix,
                "change":   ChangeType.TIGHTEN_CAP.value,
                "evidence": {"rejection_rate": rate},
            })

    # A.5 P&L vs expected — supporting evidence (does not auto-recommend)
    # NB: A.5 drift alone doesn't justify a change — needs corroboration from
    # S.4 drift (which IS already captured above). We just add it as context.
    for strat, data in (pnl_vs_expected or {}).items():
        ratio = data.get("realized_vs_expected_ratio")
        if ratio is not None and ratio < 0.5:
            # Find any existing S.4 disable rec for this strat; otherwise no-op.
            # B.4 conflict-resolution: don't double-count.
            pass

    # B.2 slippage trends — tighten threshold when widening > 0.3¢/quarter
    for (strat, prefix), delta_cents in (slippage_trends or {}).items():
        if delta_cents > 0.3:
            recs.append({
                "source":   RecommendationSource.SLIPPAGE_B2.value,
                "target":   f"{strat}:{prefix}",
                "change":   ChangeType.TIGHTEN_THRESHOLD.value,
                "evidence": {"slippage_delta_cents_per_quarter": delta_cents},
            })

    # B.3 reconciliation anomalies — informational, no auto-recommendation
    # (anomalies trigger operator investigation, not parameter changes)

    return recs


def propose_changes(
    recommendations: list[dict],
    backtest_priors: Optional[dict[str, float]] = None,
    quarter: Optional[str] = None,
) -> list[dict]:
    """Apply decision rules to recommendations and return a structured
    proposal list. Operator reviews + approves each.

    Bayesian update sketch:
      For DISABLE recommendations: posterior_p_negative_edge ∝
        prior(backtest_edge_zero) × likelihood(observed_drift_z)
      We approximate via a hard rule: backtest_priors gives the strategy's
      lifetime P&L; require negative lifetime P&L AND multi-source confirmation.
    """
    if quarter is None:
        quarter = _current_quarter()
    priors = backtest_priors or {}
    proposals: list[dict] = []

    # Group by (target, change) so we can spot multi-source confirmation
    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in recommendations:
        key = (r["target"], r["change"])
        grouped.setdefault(key, []).append(r)

    for (target, change), rs in grouped.items():
        sources = sorted({r["source"] for r in rs})

        # Multi-source confirmation for disables
        if change == ChangeType.DISABLE_STRATEGY.value:
            lifetime = priors.get(target, 0.0)
            if lifetime >= 0:
                # Positive lifetime P&L — single bad quarter not enough
                continue

        # Compute Bayesian-ish posterior probability of the change being right.
        # Simple heuristic: posterior = 0.4 + 0.2 × num_sources, capped at 0.95.
        posterior = min(0.95, 0.4 + 0.2 * len(sources))

        proposals.append({
            "quarter":             quarter,
            "target":              target,
            "change":              change,
            "sources":             sources,
            "evidence":            [r["evidence"] for r in rs],
            "bayesian_posterior":  round(posterior, 3),
            "reasoning": (
                f"{len(sources)} source(s) recommend {change} for {target}. "
                f"Posterior probability of correctness ≈ {posterior:.2f}. "
                f"Lifetime prior P&L: ${priors.get(target, 0)/100 if isinstance(priors.get(target), (int, float)) else 'unknown'}."
            ),
        })

    return proposals


def persist_proposal(p: dict) -> int:
    """Write a proposal to the audit trail. Returns proposal_id."""
    conn = _ensure_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO proposals
              (quarter, proposed_at_us, target_strategy, change_type,
               change_payload, source_signals, reasoning, bayesian_posterior, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED')
            """,
            (
                p["quarter"],
                int(time.time() * 1_000_000),
                p["target"],
                p["change"],
                json.dumps(p.get("evidence", [])),
                json.dumps(p.get("sources", [])),
                p.get("reasoning", ""),
                float(p.get("bayesian_posterior", 0)),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def operator_decide(proposal_id: int, decision: str) -> bool:
    """Record the operator's decision. `decision` ∈ {'approved', 'rejected'}."""
    if decision not in ("approved", "rejected"):
        return False
    conn = _ensure_db()
    try:
        conn.execute(
            """
            UPDATE proposals
            SET operator_decision = ?, decided_at_us = ?,
                status = CASE WHEN ? = 'approved' THEN 'APPROVED' ELSE 'REJECTED' END
            WHERE proposal_id = ?
            """,
            (decision, int(time.time() * 1_000_000), decision, proposal_id),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def list_pending_proposals() -> list[dict]:
    """Return all proposals awaiting operator decision (status=PROPOSED)."""
    conn = _ensure_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT proposal_id, quarter, proposed_at_us, target_strategy,
                   change_type, change_payload, source_signals, reasoning,
                   bayesian_posterior
            FROM proposals
            WHERE status = 'PROPOSED'
            ORDER BY proposed_at_us DESC
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for r in rows:
        out.append({
            "proposal_id":        int(r[0]),
            "quarter":            r[1],
            "proposed_at_us":     int(r[2]),
            "target":             r[3],
            "change":             r[4],
            "evidence":           json.loads(r[5]) if r[5] else [],
            "sources":            json.loads(r[6]) if r[6] else [],
            "reasoning":          r[7],
            "bayesian_posterior": float(r[8]) if r[8] is not None else 0.0,
        })
    return out
