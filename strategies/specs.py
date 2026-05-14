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

KXMVE_NFL_SINGLEGAME = StrategySpec(
    name="kxmve_nfl_singlegame_longshot",
    display_name="KXMVE NFL Single-Game Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=8_635,
    ticker_prefixes=("KXMVENFLSINGLEGAME",),
    verdict_section="§4.1",
    entry_condition_summary=(
        "Bet NO when median yes_price ≤ 0.40 in the T-12h to T-6h pre-close window. "
        "25,490 trades, +1.35% mean. Largest single KXMVE prefix; Phase 2C priority #1."
    ),
    notes="T-12h entry gate is hard requirement (T-6h adds settlement contamination).",
)

KXMVE_NFL_MULTIGAME = StrategySpec(
    name="kxmve_nfl_multigame_longshot",
    display_name="KXMVE NFL Multi-Game Extended Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=7_084,
    ticker_prefixes=("KXMVENFLMULTIGAMEEXTENDED",),
    verdict_section="§4.1",
    entry_condition_summary=(
        "Bet NO when median yes_price ≤ 0.40 in the T-24h pre-close window. "
        "31,295 trades, +0.91% mean. Wider entry window than single-game variant "
        "because multi-game parlays have slower price discovery."
    ),
)

KXMVE_NBA_SINGLEGAME = StrategySpec(
    name="kxmve_nba_singlegame_longshot",
    display_name="KXMVE NBA Single-Game Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=616,
    ticker_prefixes=("KXMVENBASINGLEGAME",),
    verdict_section="§4.1",
    entry_condition_summary=(
        "Bet NO when median yes_price ≤ 0.40 in the T-12h window. 747 trades, "
        "+3.30% mean. Small sample but highest per-trade edge in KXMVE family — "
        "low-volume market structurally."
    ),
)

KXMVE_SPORTS_MULTIGAME = StrategySpec(
    name="kxmve_sports_multigame_longshot",
    display_name="KXMVE Sports Multi-Game Extended Longshot NO",
    tier="2",
    side="no",
    expected_annual_pnl_usd=863,
    ticker_prefixes=("KXMVESPORTSMULTIGAMEEXTENDED",),
    verdict_section="§4.1",
    entry_condition_summary=(
        "Bet NO when median yes_price ≤ 0.40 in the T-12h window. 2,668 trades, "
        "+1.29% mean. Generalized multi-sport parlay variant."
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
        KXMVE_NFL_SINGLEGAME,
        KXMVE_NFL_MULTIGAME,
        KXMVE_NBA_SINGLEGAME,
        KXMVE_SPORTS_MULTIGAME,
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


# ─── Bot-side scanner registry — current implementations + drift from verdict ──
#
# Step 5 of Migration Plan §4.1. These document what the bot actually runs
# today (some active, some in disabled_model_sources) vs the verdict-canonical
# specs above. Each entry's `verdict_match` points to a VERDICT_STRATEGIES key
# when the scanner is a (possibly-drifted) implementation of that strategy, or
# is None when the scanner isn't in the verdict portfolio (most weather/GDP
# scanners — flagged for retirement in Phase 2).
#
# This registry does NOT participate in verdict_doc_alignment_check — bot
# scanners have no canonical expected_annual_pnl_usd. Their retirement schedule
# is in Migration Plan §5.3.

class BotStrategyImpl(BaseModel):
    """A scanner currently implemented (or recently disabled) in the bot.

    Documents drift from the verdict canon. Frozen — to change a bot scanner,
    update the live code AND this entry in the same commit.
    """
    model_config = ConfigDict(frozen=True)

    bot_name: str  # model_source / strategy_tag as emitted by the scanner
    display_name: str
    file_path: str  # primary location of the scanner code, e.g. kalshi_bot/models/fomc.py
    is_active: bool  # True if currently emitting; False if in disabled_model_sources
    disabled_reason: str | None = None  # populated iff is_active is False
    verdict_match: str | None  # key in VERDICT_STRATEGIES, or None if no match
    drift_summary: str  # plain text: how this differs from the verdict canon
    retirement_target: str  # Migration Plan §5.3 reference for replacement


FEDWATCH_TBILL_TERM = BotStrategyImpl(
    bot_name="fedwatch+tbill_term",
    display_name="FOMC FedWatch + T-bill Term Structure",
    file_path="kalshi_bot/models/fomc.py",
    is_active=True,
    verdict_match="fomc_fedwatch_fusion",
    drift_summary=(
        "Verdict spec is fedwatch+zq+wsj fusion (60/30/10 weights). This implementation "
        "replaced ZQ with FRED T-bill term structure (DTB3/DTB6/DTB1YR) after CME WAF "
        "403'd the ZQ endpoint in 2024 — see fomc.py:1798. WSJ source is also not "
        "integrated. Effective weights: ~50/50 fedwatch/tbill_term, confidence 0.72-0.90 "
        "depending on agreement."
    ),
    retirement_target=(
        "Migration Plan §5.4: Option I (restore ZQ if endpoint workable) or "
        "Option II (validate fedwatch+tbill_term against verdict framework as a "
        "documented variant). Decision required before Phase 2C deploys behavioral "
        "biases that depend on FOMC capital allocation."
    ),
)

FEDWATCH_BARE = BotStrategyImpl(
    bot_name="fedwatch",
    display_name="FOMC FedWatch (single-source fallback)",
    file_path="kalshi_bot/models/fomc.py",
    is_active=True,
    verdict_match="fomc_fedwatch_fusion",
    drift_summary=(
        "Single-source FedWatch (no fusion). Lower confidence (~0.70) than the "
        "tbill_term variant. Fires when T-bill source is stale or unavailable."
    ),
    retirement_target="Subsumed by fedwatch+zq+wsj fusion once ZQ is restored or fedwatch+tbill_term is validated.",
)

GFS_NOAA_HOURLY = BotStrategyImpl(
    bot_name="gfs+noaa_hourly",
    display_name="Weather Directional (GFS + NOAA blend)",
    file_path="kalshi_bot/strategy.py:scan_weather_markets",
    is_active=True,
    verdict_match=None,
    drift_summary=(
        "Directional forecasting model — predicts high temperature, bets YES/NO based "
        "on probability of crossing strike. NOT in verdict portfolio. Verdict §4.2 "
        "validates only the longshot bias variant (yes ≤25%, NO bet, T-12h), not "
        "directional prediction."
    ),
    retirement_target="Migration Plan §5.3: retire when Phase 2C #9 (weather_longshot) deploys.",
)

NOAA_NWS_OPEN_METEO = BotStrategyImpl(
    bot_name="noaa_nws+open_meteo",
    display_name="Weather Directional (NOAA NWS + Open-Meteo blend)",
    file_path="kalshi_bot/strategy.py:scan_weather_markets",
    is_active=True,
    verdict_match=None,
    drift_summary="Same class as gfs+noaa_hourly — directional weather forecasting, not in verdict portfolio.",
    retirement_target="Migration Plan §5.3: retire when Phase 2C #9 (weather_longshot) deploys.",
)

ECMWF_BLEND = BotStrategyImpl(
    bot_name="ecmwf+gfs+noaa_hourly",
    display_name="Weather Directional (ECMWF + GFS + NOAA blend)",
    file_path="kalshi_bot/strategy.py:scan_weather_markets",
    is_active=False,
    disabled_reason=(
        "Disabled 2026-05-04 via ep:config:disabled_model_sources after 7d -$55 from "
        "the ecmwf blend; backtest showed 0% accuracy when confident YES. See memory "
        "project_weather_disabled_2026_05_04 + commit 1f41341 ECMWF kill-switch."
    ),
    verdict_match=None,
    drift_summary="Same class as other weather directional scanners — not in verdict portfolio.",
    retirement_target="Permanently retired; do not re-enable. The verdict's weather_longshot replaces this entire class.",
)

GDPNOW_3_5_PCT = BotStrategyImpl(
    bot_name="gdpnow_3.5pct",
    display_name="GDPNow 3.5% Threshold Directional",
    file_path="kalshi_bot/strategy.py:scan_economic_markets",
    is_active=True,
    verdict_match=None,
    drift_summary=(
        "Directional FRED-anchored GDP scanner: fires YES/NO based on whether GDPNow "
        "estimate crosses 3.5%. NOT in verdict portfolio. Verdict §B (Rejected "
        "Strategies) validates the fred_GDP_sigmoid class as -$12.91/30d, REJECTED."
    ),
    retirement_target="Migration Plan §5.3: retire at Phase 2 start; no replacement.",
)

GDPNOW_3_7_PCT = BotStrategyImpl(
    bot_name="gdpnow_3.7pct",
    display_name="GDPNow 3.7% Threshold Directional",
    file_path="kalshi_bot/strategy.py:scan_economic_markets",
    is_active=True,
    verdict_match=None,
    drift_summary="Same class as gdpnow_3.5pct, different threshold.",
    retirement_target="Migration Plan §5.3: retire at Phase 2 start; no replacement.",
)

GDPNOW_1_2_PCT = BotStrategyImpl(
    bot_name="gdpnow_1.2pct",
    display_name="GDPNow 1.2% Threshold (disabled)",
    file_path="kalshi_bot/strategy.py:scan_economic_markets",
    is_active=False,
    disabled_reason="In disabled_model_sources per ep:config (low-conviction threshold).",
    verdict_match=None,
    drift_summary="Same class as gdpnow_3.5pct.",
    retirement_target="Migration Plan §5.3: retire at Phase 2 start; no replacement.",
)

GDPNOW_1_3_PCT = BotStrategyImpl(
    bot_name="gdpnow_1.3pct",
    display_name="GDPNow 1.3% Threshold (disabled)",
    file_path="kalshi_bot/strategy.py:scan_economic_markets",
    is_active=False,
    disabled_reason="In disabled_model_sources per ep:config (low-conviction threshold).",
    verdict_match=None,
    drift_summary="Same class as gdpnow_3.5pct.",
    retirement_target="Migration Plan §5.3: retire at Phase 2 start; no replacement.",
)

FOMC_BUTTERFLY_ARB = BotStrategyImpl(
    bot_name="fomc_butterfly_arb",
    display_name="FOMC Butterfly Arb (disabled)",
    file_path="kalshi_bot/strategy.py:scan_fomc_arb",
    is_active=False,
    disabled_reason="In disabled_model_sources per ep:config; Operating SYNTHESIS notes silent-by-design on current market structure.",
    verdict_match=None,
    drift_summary=(
        "Butterfly spread arbitrage across FOMC rate-strike ladder. Not in verdict "
        "portfolio (which validates only fedwatch+zq+wsj fusion for FOMC). Verdict's "
        "Engineering doc treats butterfly arb as unvalidated."
    ),
    retirement_target="Permanently disabled; no replacement in verdict portfolio.",
)


BOT_STRATEGIES: dict[str, BotStrategyImpl] = {
    s.bot_name: s for s in (
        FEDWATCH_TBILL_TERM,
        FEDWATCH_BARE,
        GFS_NOAA_HOURLY,
        NOAA_NWS_OPEN_METEO,
        ECMWF_BLEND,
        GDPNOW_3_5_PCT,
        GDPNOW_3_7_PCT,
        GDPNOW_1_2_PCT,
        GDPNOW_1_3_PCT,
        FOMC_BUTTERFLY_ARB,
    )
}


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
                f"  {s.name:35s}  ${s.expected_annual_pnl_usd:>6,}/yr  "
                f"side={s.side:4s}  {s.verdict_section:6s}  prefixes={prefixes}"
            )
        print()

    n_active = sum(1 for s in BOT_STRATEGIES.values() if s.is_active)
    n_disabled = len(BOT_STRATEGIES) - n_active
    n_no_match = sum(1 for s in BOT_STRATEGIES.values() if s.verdict_match is None)
    print(
        f"Bot scanners (drift documentation): {len(BOT_STRATEGIES)} total — "
        f"{n_active} active, {n_disabled} disabled, {n_no_match} with no verdict match"
    )
    for impl in BOT_STRATEGIES.values():
        status = "ACTIVE  " if impl.is_active else "DISABLED"
        match = impl.verdict_match or "<none>"
        print(f"  [{status}] {impl.bot_name:30s}  verdict_match={match}")
