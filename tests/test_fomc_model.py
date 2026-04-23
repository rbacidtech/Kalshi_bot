"""
tests/test_fomc_model.py — Unit tests for FOMC fair-value model.

Exercises:
  - _cumulative_yes_prob()  (new helper added for the cumulative-probability fix)
  - fair_value_with_confidence()  (via mock of get_meeting_probs)

Background:
  KXFED-YYMM-TX contracts pay YES when the Fed Funds rate at the meeting is AT
  OR ABOVE the strike T.  P(YES) is therefore a CUMULATIVE probability summing
  all OUTCOME_BPS entries whose implied final rate is ≥ T.  The pre-fix code
  returned only the POINT probability for the nearest outcome, which was orders
  of magnitude too low for below-current-rate strikes (e.g. T1.00 with rate 3.75%).

No network, no Redis, no auth.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

import asyncio
import pytest

from kalshi_bot.models.fomc import (
    _cumulative_yes_prob,
    fair_value_with_confidence,
    MeetingProbs,
    OUTCOME_BPS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_mp(probs: dict) -> MeetingProbs:
    """Construct a MeetingProbs with given probabilities (must sum to 1.0)."""
    return MeetingProbs(
        probs      = probs,
        fetched_at = datetime.now(timezone.utc),
        sources    = ["test"],
        confidence = 0.80,
    )


# Realistic probability distribution for a meeting ~10 months out with current
# rate at 3.75%.  Concentrates mass around 1-3 cuts.
REALISTIC_PROBS = {
    "HOLD":    0.10,
    "CUT_25":  0.25,
    "CUT_50":  0.30,
    "CUT_75":  0.20,
    "CUT_100": 0.10,
    "HIKE_25": 0.03,
    "HIKE_50": 0.02,
}

CURRENT_RATE = 3.75   # same value used by module-level _current_fed_rate in tests


# ── _cumulative_yes_prob ──────────────────────────────────────────────────────

class TestCumulativeYesProb:

    def _call(self, target_rate: float, probs: dict = None) -> float:
        if probs is None:
            probs = REALISTIC_PROBS
        mp = make_mp(probs)
        with patch("kalshi_bot.models.fomc._current_fed_rate", CURRENT_RATE):
            return _cumulative_yes_prob(target_rate, mp)

    def test_target_well_below_current_returns_near_one(self):
        """T1.00 with rate 3.75%: every model outcome keeps rate ≥ 1.00%."""
        result = self._call(1.00)
        # All 7 OUTCOME_BPS outcomes give rate ≥ 1.00% when starting at 3.75%
        # (even CUT_100 → 2.75 > 1.00), so result should be sum of all probs ≈ 1.0
        assert result >= 0.95

    def test_target_at_current_rate_excludes_cuts(self):
        """T3.75: only HOLD and HIKEs satisfy rate ≥ 3.75%."""
        result = self._call(3.75)
        expected = (
            REALISTIC_PROBS["HOLD"]
            + REALISTIC_PROBS["HIKE_25"]
            + REALISTIC_PROBS["HIKE_50"]
        )
        assert result == pytest.approx(expected, abs=1e-9)

    def test_target_one_cut_below_current(self):
        """T3.50: HOLD, CUT_25 (3.50 ≥ 3.50), and HIKEs satisfy rate ≥ 3.50%."""
        result = self._call(3.50)
        expected = (
            REALISTIC_PROBS["HOLD"]
            + REALISTIC_PROBS["CUT_25"]
            + REALISTIC_PROBS["HIKE_25"]
            + REALISTIC_PROBS["HIKE_50"]
        )
        assert result == pytest.approx(expected, abs=1e-9)

    def test_target_three_cuts_below_current(self):
        """T3.00: HOLD, CUT_25, CUT_50, CUT_75 (3.00 ≥ 3.00), and HIKEs qualify."""
        result = self._call(3.00)
        expected = (
            REALISTIC_PROBS["HOLD"]
            + REALISTIC_PROBS["CUT_25"]
            + REALISTIC_PROBS["CUT_50"]
            + REALISTIC_PROBS["CUT_75"]
            + REALISTIC_PROBS["HIKE_25"]
            + REALISTIC_PROBS["HIKE_50"]
        )
        assert result == pytest.approx(expected, abs=1e-9)

    def test_strictly_above_all_model_outcomes_clamps_to_min(self):
        """Target so high that no model outcome clears it → clamp to 0.05."""
        result = self._call(10.00)  # Fed rate at 10% — impossible
        assert result == pytest.approx(0.05)

    def test_cumulative_strictly_greater_than_point_for_cuts(self):
        """
        Core regression: cumulative P(YES for T3.50) > point P(CUT_25).

        The old code returned mp.get("CUT_25") ≈ 0.25 for T3.50.
        The new code returns P(HOLD)+P(CUT_25)+P(HIKEs) ≈ 0.40.
        """
        result   = self._call(3.50)
        old_point = REALISTIC_PROBS["CUT_25"]   # what the old code returned
        assert result > old_point

    def test_cumulative_strictly_greater_than_point_for_deep_cuts(self):
        """
        Core regression: cumulative P(YES for T3.00) > point P(CUT_75).

        Old code returned P(CUT_75) ≈ 0.20 for T3.00.
        New code returns P(HOLD)+P(CUT_25)+P(CUT_50)+P(CUT_75)+P(HIKEs) ≈ 0.90.
        """
        result    = self._call(3.00)
        old_point = REALISTIC_PROBS["CUT_75"]
        assert result > old_point

    def test_missing_outcomes_treated_as_zero(self):
        """Sparse probs dict — missing keys contribute 0, no KeyError."""
        sparse = {"HOLD": 0.80, "CUT_25": 0.20}   # no HIKE entries
        result = self._call(3.50, probs=sparse)
        # HOLD (3.75≥3.50) + CUT_25 (3.50≥3.50) = 1.0, clamped to 0.95
        expected = min(0.95, sparse["HOLD"] + sparse["CUT_25"])
        assert result == pytest.approx(expected, abs=1e-9)

    def test_monotone_decreasing_with_rising_target(self):
        """P(YES) must be non-increasing as T increases (monotonicity)."""
        targets = [1.00, 2.00, 2.75, 3.00, 3.50, 3.75, 4.00, 4.25]
        probs_list = [self._call(t) for t in targets]
        for i in range(1, len(probs_list)):
            assert probs_list[i] <= probs_list[i - 1] + 1e-9, (
                f"Monotonicity violated: P(YES@{targets[i]}) > P(YES@{targets[i-1]})"
            )


# ── fair_value_with_confidence: cumulative path ───────────────────────────────

class TestFairValueWithConfidence:

    def test_t_format_uses_cumulative_not_point(self):
        """
        For KXFED-27MAR-T3.50 the returned fair_yes must equal the cumulative
        sum, not just mp.get("CUT_25").
        """
        mp = make_mp(REALISTIC_PROBS)
        with (
            patch("kalshi_bot.models.fomc._current_fed_rate", CURRENT_RATE),
            patch(
                "kalshi_bot.models.fomc.get_meeting_probs",
                AsyncMock(return_value=mp),
            ),
        ):
            fv, conf = asyncio.run(
                fair_value_with_confidence("KXFED-27MAR-T3.50", 0.46)
            )

        # Must NOT equal the single-point P(CUT_25)
        assert fv != pytest.approx(REALISTIC_PROBS["CUT_25"], abs=1e-4)
        # Must equal HOLD + CUT_25 + HIKEs
        expected = (
            REALISTIC_PROBS["HOLD"]
            + REALISTIC_PROBS["CUT_25"]
            + REALISTIC_PROBS["HIKE_25"]
            + REALISTIC_PROBS["HIKE_50"]
        )
        assert fv == pytest.approx(expected, abs=1e-6)
        assert conf == pytest.approx(0.80)

    def test_t_format_below_current_rate_returns_high_prob(self):
        """T1.00 with rate 3.75%: fair_yes should be near 1.0."""
        mp = make_mp(REALISTIC_PROBS)
        with (
            patch("kalshi_bot.models.fomc._current_fed_rate", CURRENT_RATE),
            patch(
                "kalshi_bot.models.fomc.get_meeting_probs",
                AsyncMock(return_value=mp),
            ),
        ):
            fv, conf = asyncio.run(
                fair_value_with_confidence("KXFED-27MAR-T1.00", 0.89)
            )

        assert fv >= 0.95

    def test_no_meeting_probs_returns_none(self):
        """When get_meeting_probs returns None, returns (None, 0.30)."""
        with patch(
            "kalshi_bot.models.fomc.get_meeting_probs",
            AsyncMock(return_value=None),
        ):
            fv, conf = asyncio.run(
                fair_value_with_confidence("KXFED-27MAR-T3.50", 0.46)
            )

        assert fv is None
        assert conf == pytest.approx(0.30)

    def test_non_fomc_ticker_returns_none(self):
        """Non-FOMC tickers return (None, 0.30) immediately."""
        fv, conf = asyncio.run(
            fair_value_with_confidence("KXBTC-25DEC-B95000", 0.50)
        )
        assert fv is None
        assert conf == pytest.approx(0.30)
