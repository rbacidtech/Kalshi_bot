"""
models/arb.py — Multi-meeting FOMC arbitrage detector.

The core insight:
  Fed rate expectations are cumulative. If the market prices a 40%
  chance of a cut at the June meeting and a 65% chance of a cut by
  July, that implies a ~25% chance the Fed cuts at July but *not* June.

  Mathematically:
    P(cut by July) = P(cut at June) + P(no cut at June) × P(cut at July | no cut at June)

  Rearranging:
    P(cut at July | no cut at June) = (P(by July) - P(at June)) / (1 - P(at June))

  If Kalshi is pricing the July contract inconsistently with the June
  contract given this relationship, there's a structural arbitrage
  independent of which way rates actually move.

Three types of edges this catches:

  1. CALENDAR SPREAD — June and July contracts price inconsistent
     cumulative probabilities. Buy the cheap one, short the expensive one.
     (Note: Kalshi doesn't support shorts, so we buy the underpriced leg only.)

  2. OUTCOME SUM — For a single meeting, YES prices of all outcomes
     (HOLD + CUT_25 + CUT_50 + ...) should sum to ~100¢ minus fees.
     If they sum to 90¢, the market is collectively underpricing all
     outcomes — buy the most likely one. If they sum to 110¢, overpriced.

  3. COMPLEMENTARY PAIRS — HOLD and CUT_25 for the same meeting are
     not perfectly complementary if there are other outcomes. But if
     only two outcomes are liquid, their YES prices should sum to ~93¢
     (100¢ minus round-trip fee). Divergence from this is tradeable.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Fee per contract per side (cents)
FEE_CENTS = 7

# Minimum edge to flag an arb (cents)
MIN_ARB_EDGE_CENTS = 8


@dataclass
class ArbSignal:
    """
    An arbitrage signal between two or more FOMC contracts.

    arb_type:    "calendar_spread", "outcome_sum", or "complementary"
    ticker:      The contract to BUY
    edge_cents:  Expected profit in cents if the arb closes
    description: Human-readable explanation
    confidence:  0-1 (lower for multi-leg arbs, higher for simple pairs)
    """
    arb_type:    str
    ticker:      str
    side:        str        # "yes" or "no"
    edge_cents:  float
    description: str
    confidence:  float
    meeting:     str
    related_ticker: str = ""


def detect_arb_signals(
    markets: list[dict],
    fomc_probs: dict,        # meeting_key → MeetingProbs from fomc.py
    min_edge_cents: float = MIN_ARB_EDGE_CENTS,
) -> list[ArbSignal]:
    """
    Scan all open FOMC markets for structural arbitrage opportunities.

    Args:
        markets:      List of market dicts from Kalshi /markets endpoint
        fomc_probs:   Current meeting probabilities from fomc.get_meeting_probs()
        min_edge_cents: Minimum edge to return a signal

    Returns:
        List of ArbSignal objects sorted by edge descending
    """
    signals = []

    # Group markets by meeting month
    by_meeting: dict[str, list[dict]] = {}
    for m in markets:
        ticker = m.get("ticker", "")
        from .fomc import parse_fomc_ticker
        parsed = parse_fomc_ticker(ticker)
        if parsed:
            key = parsed["meeting"]
            by_meeting.setdefault(key, []).append({**m, "_parsed": parsed})

    meetings_sorted = sorted(by_meeting.keys())

    # ── 1. Outcome sum check (per meeting) ───────────────────────────────────
    for meeting_key, mtks in by_meeting.items():
        sum_signals = _check_outcome_sum(mtks, min_edge_cents)
        signals.extend(sum_signals)

    # ── 2. Complementary pair check (per meeting) ────────────────────────────
    for meeting_key, mtks in by_meeting.items():
        pair_signals = _check_complementary_pairs(mtks, min_edge_cents)
        signals.extend(pair_signals)

    # ── 3. Calendar spread (across meetings) ─────────────────────────────────
    if len(meetings_sorted) >= 2:
        for i in range(len(meetings_sorted) - 1):
            m1 = meetings_sorted[i]
            m2 = meetings_sorted[i + 1]
            cal_signals = _check_calendar_spread(
                by_meeting[m1], by_meeting[m2],
                fomc_probs, min_edge_cents,
            )
            signals.extend(cal_signals)

    signals.sort(key=lambda s: s.edge_cents, reverse=True)

    if signals:
        log.info("Arb detector found %d signal(s):", len(signals))
        for s in signals:
            log.info("  [%s] %s  side=%s  edge=%.1f¢  %s",
                     s.arb_type, s.ticker, s.side, s.edge_cents, s.description)

    return signals


def _check_outcome_sum(
    markets: list[dict],
    min_edge: float,
) -> list[ArbSignal]:
    """
    For a single meeting, YES prices of all outcomes should sum to
    approximately 100¢ (the probability simplex).

    If sum < 93¢ (100 - fee buffer), the market is collectively underpricing
    outcomes. Buy the most underpriced outcome relative to FedWatch.
    If sum > 107¢, the market is overpricing. Buy NO on the overpriced outcome.
    """
    signals = []
    if len(markets) < 2:
        return signals

    total_yes = sum(m.get("last_price", 50) for m in markets)
    n         = len(markets)

    # Expected sum accounting for fees
    expected_min = 100 - FEE_CENTS * 2      # ~86
    expected_max = 100 + FEE_CENTS          # ~107

    if total_yes < expected_min:
        # Collectively underpriced — find cheapest market relative to 100/n baseline
        fair_per  = 100 / n
        cheapest  = min(markets, key=lambda m: m.get("last_price", 50))
        edge      = fair_per - cheapest.get("last_price", 50)

        if edge >= min_edge:
            signals.append(ArbSignal(
                arb_type    = "outcome_sum",
                ticker      = cheapest["ticker"],
                side        = "yes",
                edge_cents  = round(edge, 1),
                description = (
                    f"Meeting outcomes sum to {total_yes}¢ "
                    f"(expected ~{expected_min}-{expected_max}¢). "
                    f"Buying cheapest outcome."
                ),
                confidence  = 0.65,
                meeting     = cheapest.get("_parsed", {}).get("meeting", ""),
            ))

    elif total_yes > expected_max:
        # Collectively overpriced — buy NO on most expensive outcome
        priciest = max(markets, key=lambda m: m.get("last_price", 50))
        edge     = priciest.get("last_price", 50) - 100 / n

        if edge >= min_edge:
            signals.append(ArbSignal(
                arb_type    = "outcome_sum",
                ticker      = priciest["ticker"],
                side        = "no",
                edge_cents  = round(edge, 1),
                description = (
                    f"Meeting outcomes sum to {total_yes}¢ "
                    f"(overpriced vs expected ~100¢). "
                    f"Buying NO on most expensive outcome."
                ),
                confidence  = 0.65,
                meeting     = priciest.get("_parsed", {}).get("meeting", ""),
            ))

    return signals


def _check_complementary_pairs(
    markets: list[dict],
    min_edge: float,
) -> list[ArbSignal]:
    """
    For a meeting with exactly two liquid outcomes (e.g. HOLD and CUT_25),
    their YES prices should sum to approximately 93¢ (100¢ - round-trip fee).

    If they sum to 85¢, both are underpriced — buy the one with the higher
    FedWatch probability.
    If they sum to 102¢, both are overpriced — pass (can't short on Kalshi).
    """
    signals = []

    # Find pairs of complementary outcomes (HOLD + CUT_25, etc.)
    outcomes = {
        m["_parsed"]["outcome"]: m
        for m in markets
        if "_parsed" in m
    }

    complementary_pairs = [
        ("HOLD", "CUT_25"),
        ("HOLD", "HIKE_25"),
        ("CUT_25", "CUT_50"),
    ]

    target_sum = 100 - FEE_CENTS    # ~93¢

    for a, b in complementary_pairs:
        if a not in outcomes or b not in outcomes:
            continue

        ma      = outcomes[a]
        mb      = outcomes[b]
        price_a = ma.get("last_price", 50)
        price_b = mb.get("last_price", 50)
        pair_sum = price_a + price_b

        gap = target_sum - pair_sum

        if gap >= min_edge:
            # Both underpriced — buy the cheaper one
            cheaper = ma if price_a <= price_b else mb
            signals.append(ArbSignal(
                arb_type       = "complementary",
                ticker         = cheaper["ticker"],
                side           = "yes",
                edge_cents     = round(gap / 2, 1),
                description    = (
                    f"{a}({price_a}¢) + {b}({price_b}¢) = {pair_sum}¢ "
                    f"(expected ~{target_sum}¢). Pair underpriced."
                ),
                confidence     = 0.70,
                meeting        = cheaper.get("_parsed", {}).get("meeting", ""),
                related_ticker = (mb if cheaper is ma else ma)["ticker"],
            ))

    return signals


def _check_calendar_spread(
    near_markets: list[dict],
    far_markets:  list[dict],
    fomc_probs:   dict,
    min_edge:     float,
) -> list[ArbSignal]:
    """
    Check that the cumulative cut probability across two meetings is
    internally consistent.

    P(cut by M2) >= P(cut at M1) always (if you cut at M1, you've cut by M2).
    If P(cut by M2) < P(cut at M1), there's a contradiction — and one of
    them is mispriced.

    This is the most profitable arb type when it appears, but also the
    rarest. It requires both meetings to be actively traded.
    """
    signals = []

    # Find CUT contracts in each meeting
    def find_cut(markets):
        for m in markets:
            parsed = m.get("_parsed", {})
            if parsed.get("outcome") in ("CUT_25", "CUT_50"):
                return m
        return None

    near_cut = find_cut(near_markets)
    far_cut  = find_cut(far_markets)

    if not near_cut or not far_cut:
        return signals

    near_price = near_cut.get("last_price", 50) / 100
    far_price  = far_cut.get("last_price", 50) / 100

    # P(cut by far) should be >= P(cut at near)
    # If not, far meeting cut is underpriced
    if far_price < near_price - (min_edge / 100):
        edge_cents = (near_price - far_price) * 100

        signals.append(ArbSignal(
            arb_type    = "calendar_spread",
            ticker      = far_cut["ticker"],
            side        = "yes",
            edge_cents  = round(edge_cents, 1),
            description = (
                f"Near meeting cut={near_price:.2%} > "
                f"far meeting cut={far_price:.2%}. "
                f"Cumulative probability violated — far meeting underpriced."
            ),
            confidence     = 0.75,
            meeting        = far_cut.get("_parsed", {}).get("meeting", ""),
            related_ticker = near_cut["ticker"],
        ))

    return signals
