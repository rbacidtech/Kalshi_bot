"""Verdict-canonical strategy specifications for the EdgePulse parity gate.

Phase 1.1 S.4 Step 1 of EdgePulse_Migration_Plan_2026.md.

Each StrategySpec captures what `EdgePulse_Backtest_Verdict_2026.md` says
about a verdict-validated strategy: side, ticker prefixes, expected annual
P&L, entry condition. These are imported by:

    - `verdict_doc_alignment_check()` (called at service startup; Step 2)
    - `scripts/parity_test.py` (deploy gate; Step 3)
    - Future scanner implementations in Phase 2 (each implementation must
      match its spec or fail the parity test)

The 12 specs below sum to $102,066/yr expected. The alignment check fails
on drift >$5,000 from that total. Stub-status specs encode only the verdict
P&L expectation; their entry_condition_summary is plain-text TODO until the
scanner is implemented in Phase 2.

The bot-side strategy register (drifts from verdict, e.g.
`fedwatch+tbill_term` vs verdict's `fedwatch+zq+wsj`) is added in Step 5.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


# Source: sum of per-strategy expected_annual_pnl_usd as enumerated in
# /root/research/EdgePulse_Backtest_Verdict_2026.md §3 (Tier 1) + §4 (Tier 2)
# + §A.3 (FOMC fusion). A change to any per-strategy value must update this
# constant in the same commit; verdict_doc_alignment_check() enforces consistency.
VERDICT_DOC_TOTAL_USD = 102_066
VERDICT_TOLERANCE_USD = 5_000


class StrategySpec(BaseModel):
    """Canonical specification for one verdict-validated Kalshi strategy.

    Frozen — specs do not mutate at runtime. To change a spec, edit this file
    and update VERDICT_DOC_TOTAL_USD if the sum drifts.
    """
    model_config = ConfigDict(frozen=True)

    name: str
    display_name: str
    tier: Literal["1", "2", "S"]
    side: Literal["yes", "no", "both"]
    expected_annual_pnl_usd: int
    ticker_prefixes: tuple[str, ...]
    verdict_section: str
    entry_condition_summary: str
    notes: str | None = None


H2H_SUM_TO_1 = StrategySpec(
    name="h2h_sum_to_1",
    display_name="H2H Sum-to-1 Arbitrage",
    tier="1",
    side="both",
    expected_annual_pnl_usd=23_054,
    ticker_prefixes=(
        "KXMLBGAME", "KXMLSGAME", "KXWTAMATCH", "KXATPMATCH",
        "KXNCAAMBGAME", "KXNCAAFGAME", "KXNHLGAME",
    ),
    verdict_section="§3.1",
    entry_condition_summary=(
        "For 2-outcome H2H events, yes_price(home) + yes_price(away) must equal "
        "1.00 at settlement. When sum < 0.98 on Kalshi, buy both sides for "
        "deterministic arb. Settlement-robust; no directional prediction."
    ),
    notes="Verdict's single biggest dollar-volume missing strategy. Phase 2A #1.",
)

SPREAD_MONOT = StrategySpec(
    name="spread_monot",
    display_name="Sports Spread Monotonicity",
    tier="1",
    side="both",
    expected_annual_pnl_usd=10_067,
    ticker_prefixes=(),
    verdict_section="§3.2",
    entry_condition_summary="TODO Step 5 / Phase 2B — point-spread monotonicity violations across 5 leagues.",
)

TOTAL_MONOT = StrategySpec(
    name="total_monot",
    display_name="Sports Total Monotonicity",
    tier="1",
    side="both",
    expected_annual_pnl_usd=8_773,
    ticker_prefixes=(),
    verdict_section="§3.3",
    entry_condition_summary="TODO Step 5 / Phase 2B — over/under total monotonicity violations across 4 leagues.",
)

NFL_PROP_YARDAGE_MONOT = StrategySpec(
    name="nfl_prop_yardage_monot",
    display_name="NFL Prop Yardage Monotonicity",
    tier="1",
    side="both",
    expected_annual_pnl_usd=1_337,
    ticker_prefixes=("KXNFLRSHYDS", "KXNFLRECYDS"),
    verdict_section="§3.4",
    entry_condition_summary="TODO Step 5 / Phase 2B — yardage-threshold monotonicity on rushing/receiving prop ladders.",
)

CRYPTO_THRESHOLD_MONOT = StrategySpec(
    name="crypto_threshold_monot",
    display_name="Crypto Threshold Monotonicity",
    tier="1",
    side="both",
    expected_annual_pnl_usd=9_270,
    ticker_prefixes=("KXBTCD", "KXETHD"),
    verdict_section="§3.5",
    entry_condition_summary="TODO Step 5 / Phase 2B — 24h crypto-price threshold monotonicity (replaces bot's `scan_crypto_price_markets`).",
    notes="Retires bot's lognormal crypto price scanner per Migration Plan §5.3.",
)

A2_CROSS_MARKET_ARB = StrategySpec(
    name="a2_cross_market_arb",
    display_name="A2 Cross-Market Sum-of-Prices Arb",
    tier="1",
    side="both",
    expected_annual_pnl_usd=2_500,
    ticker_prefixes=(),
    verdict_section="§3.6",
    entry_condition_summary="TODO Step 5 / Phase 2A — multi-bin sum-of-prices arbitrage across related markets.",
)

KXMVE_LONGSHOT = StrategySpec(
    name="kxmve_longshot",
    display_name="KXMVE Parlay Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=17_198,
    ticker_prefixes=("KXMVENFLSINGLEGAME", "KXMVENFLMULTIGAMEEXTENDED"),
    verdict_section="§4.1",
    entry_condition_summary=(
        "Longshot bias on parlay markets: when YES is ≤40%, market overprices the "
        "improbable parlay outcome. Buy NO at T-12h+ (mandatory entry-time gate). "
        "Single highest-EV strategy in the verdict portfolio."
    ),
    notes=(
        "Phase 2C priority #1. T-12h entry gate is hard requirement. "
        "STEP 5 / PHASE 2C: split into two specs per Migration Plan §5.1 — "
        "KXMVENFLSINGLEGAME (~$8.6K, ≤40% yes, T-12h) and "
        "KXMVENFLMULTIGAMEEXTENDED (~$7K, ≤24h timing). Combined here as a "
        "placeholder pending verdict-doc §4.1 re-read."
    ),
)

WEATHER_LONGSHOT = StrategySpec(
    name="weather_longshot",
    display_name="Weather City Highs Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=9_107,
    ticker_prefixes=("KXHIGH",),
    verdict_section="§4.2",
    entry_condition_summary=(
        "Longshot bias on weather city-high markets: when YES is ≤25%, market "
        "overprices the high-temperature outcome. Buy NO at T-12h+. Replaces "
        "bot's weather directional strategies."
    ),
    notes="Phase 2C #9. Retires `gfs+noaa_hourly` / `noaa_nws+open_meteo` directional scanners.",
)

A1_MENTION_NO = StrategySpec(
    name="a1_mention_no",
    display_name="A1 Mention NO Bet",
    tier="2",
    side="no",
    expected_annual_pnl_usd=2_500,
    ticker_prefixes=("KXTRUMP", "SECPRESS", "VANCEMENTION"),
    verdict_section="§4.3",
    entry_condition_summary=(
        "Mention-frequency markets are systematically overpriced YES. Buy NO. "
        "Corrected from earlier verdict-inverted YES position per research §S.4.A1."
    ),
)

CRYPTO_DAILY_LONGSHOT = StrategySpec(
    name="crypto_daily_longshot",
    display_name="Crypto Daily Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=1_848,
    ticker_prefixes=("KXBTCD", "KXETHD"),
    verdict_section="§4.4",
    entry_condition_summary="TODO Step 5 / Phase 2C — daily crypto longshot NO bet at yes ≤25%.",
)

POLITICAL_LONGSHOT = StrategySpec(
    name="political_longshot",
    display_name="Political Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=1_412,
    ticker_prefixes=("KXTRUMPMENTION", "APRPOTUS", "538APPROVE"),
    verdict_section="§4.5",
    entry_condition_summary="TODO Step 5 / Phase 2C — political longshot NO bet at extreme YES underpricing.",
)

FOMC_FUSION = StrategySpec(
    name="fomc_fedwatch_fusion",
    display_name="FOMC FedWatch + ZQ + WSJ Fusion",
    tier="S",
    side="both",
    expected_annual_pnl_usd=15_000,
    ticker_prefixes=("KXFED-",),
    verdict_section="§A.3",
    entry_condition_summary=(
        "Weighted fair-value fusion: 0.60×FedWatch + 0.30×ZQ + 0.10×WSJ. "
        "Per-meeting cumulative YES probability vs market price; trade when "
        "|fair - market| > 0.10 edge threshold. Confidence model: all 3 sources "
        "agree on HOLD within 4¢ → 0.90; all 3 disagree > 4¢ → 0.75; only "
        "FedWatch available → 0.70."
    ),
    notes=(
        "Bot currently runs `fedwatch+tbill_term` (ZQ replaced by FRED T-bill "
        "term structure since CME 403'd ZQ in 2024). The verdict spec is the "
        "ZQ-original. Drift documented in Step 5 + resolved in Phase 2.4."
    ),
)


VERDICT_STRATEGIES: dict[str, StrategySpec] = {
    s.name: s for s in (
        H2H_SUM_TO_1,
        SPREAD_MONOT,
        TOTAL_MONOT,
        NFL_PROP_YARDAGE_MONOT,
        CRYPTO_THRESHOLD_MONOT,
        A2_CROSS_MARKET_ARB,
        KXMVE_LONGSHOT,
        WEATHER_LONGSHOT,
        A1_MENTION_NO,
        CRYPTO_DAILY_LONGSHOT,
        POLITICAL_LONGSHOT,
        FOMC_FUSION,
    )
}


def verdict_doc_alignment_check() -> None:
    """Assert the spec module's per-strategy P&L sums match the verdict doc total.

    Phase 1.1 S.4: called at intel + exec service startup. RuntimeError aborts
    the service on drift > VERDICT_TOLERANCE_USD ($5,000) from
    VERDICT_DOC_TOTAL_USD ($102,066). A deliberate spec change requires both
    the per-strategy update AND a matching update to VERDICT_DOC_TOTAL_USD.
    """
    total = sum(s.expected_annual_pnl_usd for s in VERDICT_STRATEGIES.values())
    drift = total - VERDICT_DOC_TOTAL_USD
    if abs(drift) > VERDICT_TOLERANCE_USD:
        raise RuntimeError(
            f"Strategy spec totals ${total:,} diverge from verdict doc "
            f"${VERDICT_DOC_TOTAL_USD:,} by ${drift:+,} "
            f"(tolerance ±${VERDICT_TOLERANCE_USD:,}). "
            f"A strategy was added/removed or expected_annual_pnl_usd changed "
            f"without updating VERDICT_DOC_TOTAL_USD. Verify against "
            f"EdgePulse_Backtest_Verdict_2026.md before adjusting the constant."
        )


if __name__ == "__main__":
    verdict_doc_alignment_check()
    total = sum(s.expected_annual_pnl_usd for s in VERDICT_STRATEGIES.values())
    print(
        f"Verdict portfolio: {len(VERDICT_STRATEGIES)} strategies, "
        f"${total:,}/yr expected (target ${VERDICT_DOC_TOTAL_USD:,} "
        f"±${VERDICT_TOLERANCE_USD:,})\n"
    )
    for tier_label, tier_code in (("1 — Structural arbs", "1"),
                                   ("2 — Behavioral biases", "2"),
                                   ("S — Directional fundamental", "S")):
        tier_specs = [s for s in VERDICT_STRATEGIES.values() if s.tier == tier_code]
        if not tier_specs:
            continue
        tier_total = sum(s.expected_annual_pnl_usd for s in tier_specs)
        print(f"Tier {tier_label}  ({len(tier_specs)} strategies, ${tier_total:,}/yr)")
        for s in tier_specs:
            prefixes = ",".join(s.ticker_prefixes) if s.ticker_prefixes else "<TBD>"
            print(
                f"  {s.name:28s}  ${s.expected_annual_pnl_usd:>6,}/yr  "
                f"side={s.side:4s}  {s.verdict_section:6s}  prefixes={prefixes}"
            )
        print()
