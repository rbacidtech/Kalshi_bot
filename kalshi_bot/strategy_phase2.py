"""Verdict-portfolio scanners (Phase 2) — verdict §3-§4 strategies.

13 of the 15 verdict-validated strategies. H2H sum-to-1 already ships
in `strategy.py:scan_h2h_sum_to_1_arb`. FOMC fusion uses fomc.py's
existing fair_value_with_confidence (out of scope here).

This module exposes:

  Tier-1 structural arbs (settlement-robust, deterministic):
    scan_spread_monotonicity        — verdict §3.2  ~$10K/yr
    scan_total_monotonicity         — verdict §3.3  ~$8.8K/yr
    scan_nfl_prop_yardage_monot     — verdict §3.4  ~$1.3K/yr
    scan_crypto_threshold_monot     — verdict §3.5  ~$9.3K/yr
    scan_a2_cross_market_arb        — verdict §3.6  ~$2.5K/yr

  Tier-2 behavioral biases (T-12h+ entry mandatory):
    scan_kxmve_nfl_singlegame_longshot
    scan_kxmve_nfl_multigame_longshot
    scan_kxmve_nba_singlegame_longshot
    scan_kxmve_sports_multigame_longshot
    scan_weather_city_highs_longshot
    scan_a1_mention_no
    scan_crypto_daily_longshot
    scan_political_longshot

Engineering A.3: latency MATTERS ONLY for H2H sum-to-1 (already optimized
in strategies/hot_path/h2h_fast.py). All scanners here use normal Python
idioms — they trade on 6-12h windows where 5ms decision cost is irrelevant.

Each scanner returns `list[Signal]`. Wired into fetch_signals_async in
strategy.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

# Import Signal + helpers from main strategy module.
from kalshi_bot.strategy import (
    Signal,
    _market_mid,
    _scan_ladder_arb_core,
    KALSHI_FEE_RATE,
    MIN_EDGE_GROSS,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tier-1: Monotonicity arbs (sports spread/total, NFL prop, crypto threshold)
# ─────────────────────────────────────────────────────────────────────────────

_SPREAD_PREFIXES = ("KXNCAAMBSPREAD", "KXNCAAFSPREAD", "KXNHLSPREAD",
                    "KXNBASPREAD", "KXNFLSPREAD")
_TOTAL_PREFIXES = ("KXNCAAFTOTAL", "KXNCAAMBTOTAL", "KXNBATOTAL", "KXNFLTOTAL")
_NFL_PROP_PREFIXES = ("KXNFLRSHYDS", "KXNFLRECYDS")
_CRYPTO_THRESHOLD_PREFIXES = ("KXBTCD", "KXETHD")


def _group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in markets:
        ev = m.get("event_ticker") or ""
        if not ev:
            continue
        out.setdefault(ev, []).append(m)
    return out


def scan_spread_monotonicity(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Sports spread monotonicity — verdict §3.2 ($10K/yr).

    For each event_ticker, prices on adjacent spread thresholds must be
    monotonic. Use the FOMC-arb ladder helper which handles this for
    threshold ladders.
    """
    filtered = [m for m in markets
                if any(str(m.get("ticker", "")).startswith(p) for p in _SPREAD_PREFIXES)]
    if not filtered:
        return []
    groups = _group_by_event(filtered)
    sigs = _scan_ladder_arb_core(groups, max_contracts, series_tag="spread_monot")
    for s in sigs:
        s.model_source = "spread_monot"
        s.category = "arb"
    if sigs:
        log.info("Spread monotonicity: %d signals across %d events", len(sigs), len(groups))
    return sigs


def scan_total_monotonicity(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Sports total monotonicity — verdict §3.3 ($8.8K/yr)."""
    filtered = [m for m in markets
                if any(str(m.get("ticker", "")).startswith(p) for p in _TOTAL_PREFIXES)]
    if not filtered:
        return []
    groups = _group_by_event(filtered)
    sigs = _scan_ladder_arb_core(groups, max_contracts, series_tag="total_monot")
    for s in sigs:
        s.model_source = "total_monot"
        s.category = "arb"
    if sigs:
        log.info("Total monotonicity: %d signals across %d events", len(sigs), len(groups))
    return sigs


def scan_nfl_prop_yardage_monot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """NFL prop yardage monotonicity — verdict §3.4 ($1.3K/yr).

    Same technique on per-player yardage ladders. Group by
    (event_ticker, player_id).
    """
    filtered = [m for m in markets
                if any(str(m.get("ticker", "")).startswith(p) for p in _NFL_PROP_PREFIXES)]
    if not filtered:
        return []
    # Group by (event_ticker, player_id-ish). Tickers like
    # KXNFLRSHYDS-25NOV16-BUFKC-MAHOMES-T75 — group by event+player.
    groups: dict[str, list[dict]] = {}
    for m in filtered:
        ticker = str(m.get("ticker", ""))
        parts = ticker.split("-")
        if len(parts) < 4:
            continue
        # event_ticker + player identifier (segments 1..-2 = date+teams+player)
        group_key = "-".join(parts[:-1])
        groups.setdefault(group_key, []).append(m)
    sigs = _scan_ladder_arb_core(groups, max_contracts, series_tag="nfl_prop_monot")
    for s in sigs:
        s.model_source = "nfl_prop_yardage_monot"
        s.category = "arb"
    if sigs:
        log.info("NFL prop yardage monot: %d signals across %d ladders", len(sigs), len(groups))
    return sigs


def scan_crypto_threshold_monot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Crypto threshold monotonicity — verdict §3.5 ($9.3K/yr).

    KXBTCD / KXETHD daily threshold ladders. P(BTC > X) must monotone-
    decrease as X increases. Same arb core.
    """
    filtered = [m for m in markets
                if any(str(m.get("ticker", "")).startswith(p) for p in _CRYPTO_THRESHOLD_PREFIXES)]
    if not filtered:
        return []
    groups = _group_by_event(filtered)
    sigs = _scan_ladder_arb_core(groups, max_contracts, series_tag="crypto_threshold_monot")
    for s in sigs:
        s.model_source = "crypto_threshold_monot"
        s.category = "arb"
    if sigs:
        log.info("Crypto threshold monot: %d signals across %d ladders", len(sigs), len(groups))
    return sigs


def scan_a2_cross_market_arb(markets: list[dict], max_contracts: int) -> list[Signal]:
    """A2 cross-market sum-of-prices arb — verdict §3.6 (~$2.5K/yr).

    For multi-bin events (forex daily ranges, oil thresholds, KXMLSGAME
    3-outcome), sum_yes_ask across all bins must equal 1.00 at settlement.
    When sum_ask < 0.95 (defensive threshold given multi-leg fees), buy
    YES on all bins.

    Only fires on events with 3+ outcome markets (2-outcome handled by
    scan_h2h_sum_to_1_arb).
    """
    PREFIXES = ("KXMLSGAME", "EURUSD", "USDJPY", "WTI", "TNOTED")
    SUM_THRESHOLD = 0.95   # tighter than H2H (more legs = more fees)
    filtered = [m for m in markets
                if any(str(m.get("ticker", "")).startswith(p) for p in PREFIXES)]
    if not filtered:
        return []
    by_event = _group_by_event(filtered)

    signals: list[Signal] = []
    for event, group in by_event.items():
        if len(group) < 3:
            continue   # 2-outcome handled by H2H scanner
        priced: list[tuple[dict, float]] = []
        for m in group:
            try:
                a = float(m.get("yes_ask_dollars") or 0)
            except (TypeError, ValueError):
                a = 0.0
            if 0.01 < a < 1.0:
                priced.append((m, a))
        if len(priced) < 3:
            continue
        sum_ask = sum(a for _, a in priced)
        if sum_ask >= SUM_THRESHOLD:
            continue
        gross_edge = 1.0 - sum_ask
        fee_per_pair = sum(KALSHI_FEE_RATE * a * (1 - a) for _, a in priced)
        net_edge = gross_edge - fee_per_pair
        contracts = min(max_contracts, max(1, int(gross_edge * 50)))
        sig = Signal(
            ticker            = priced[0][0]["ticker"],
            title             = f"A2 ARB: buy YES on {len(priced)} bins (sum_ask={sum_ask:.4f})",
            category          = "arb",
            side              = "yes",
            fair_value        = 1.0 / len(priced),
            market_price      = priced[0][1],
            edge              = gross_edge,
            fee_adjusted_edge = net_edge,
            contracts         = contracts,
            confidence        = 0.85,
            model_source      = "a2_cross_market_arb",
            arb_legs          = [
                {"ticker": m["ticker"], "side": "yes",
                 "price_cents": int(round(a * 100))}
                for m, a in priced
            ],
            close_time        = priced[0][0].get("close_time"),
        )
        signals.append(sig)
        log.info("A2 ARB: %s %d bins sum=%.4f gross=%.2f¢ x%d",
                 event, len(priced), sum_ask, gross_edge * 100, contracts)
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Tier-2: Longshot behavioral biases (T-12h+ entry mandatory)
# ─────────────────────────────────────────────────────────────────────────────

def _hours_to_close(market: dict) -> Optional[float]:
    """Hours until market close from now (UTC). None if unparseable."""
    ct = market.get("close_time")
    if not ct:
        return None
    try:
        if isinstance(ct, str):
            close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        else:
            close_dt = ct
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        return (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return None


def _scan_longshot_no(
    markets: list[dict],
    max_contracts: int,
    prefixes: tuple[str, ...],
    yes_max: float,
    min_hours_to_close: float,
    max_hours_to_close: Optional[float],
    model_source: str,
    confidence: float = 0.75,
) -> list[Signal]:
    """Shared longshot-bias NO-bet implementation.

    Filters to `prefixes`, requires yes_price ≤ `yes_max` AND
    `min_hours_to_close ≤ T_to_close ≤ max_hours_to_close`. Emits a NO
    signal at fair_value computed from longshot-bias correction.

    Engineering S.1 maker-first: when no_bid is available, places the
    NO buy at no_bid (rests as maker). Signal.market_price stays on the
    YES axis (= 1 - no_bid) per convention.
    """
    try:
        from ep_maker_tracker import maker_price_for_no_buy
    except Exception:  # pragma: no cover — defensive only
        maker_price_for_no_buy = None  # type: ignore[assignment]

    out: list[Signal] = []
    for m in markets:
        ticker = str(m.get("ticker", ""))
        if not any(ticker.startswith(p) for p in prefixes):
            continue
        yes_mid = _market_mid(m)
        if yes_mid <= 0 or yes_mid > yes_max:
            continue
        h = _hours_to_close(m)
        if h is None or h < min_hours_to_close:
            continue
        if max_hours_to_close is not None and h > max_hours_to_close:
            continue
        # Fair value for NO bet: market is OVERPRICED on YES (longshot bias).
        # Conservative fair: yes_mid × 0.7 (research-validated bias adjustment).
        fair_yes = yes_mid * 0.7
        edge = yes_mid - fair_yes   # how much YES is overpriced
        no_price = 1.0 - yes_mid
        fee = KALSHI_FEE_RATE * no_price * (1 - no_price)
        net_edge = edge - fee
        if net_edge < MIN_EDGE_GROSS * 0.5:
            continue
        contracts = min(max_contracts, max(1, int(edge * 50)))
        # S.1 maker-first: place at no_bid (rests as maker). When unavailable
        # falls back to no_price (= 1 - yes_mid). Signal.market_price holds
        # the YES-equivalent (= 1 - limit_no_price).
        if maker_price_for_no_buy is not None:
            yes_market_price = maker_price_for_no_buy(m, yes_mid)
        else:
            yes_market_price = yes_mid
        out.append(Signal(
            ticker            = ticker,
            title             = f"{model_source} NO at yes={yes_mid:.3f} T-{h:.1f}h limit_no={(1-yes_market_price):.3f}",
            category          = "longshot",
            side              = "no",
            fair_value        = fair_yes,
            market_price      = yes_market_price,
            edge              = edge,
            fee_adjusted_edge = net_edge,
            contracts         = contracts,
            confidence        = confidence,
            model_source      = model_source,
            close_time        = m.get("close_time"),
            # B.2 plumbing — order-book context for slippage decomposition
            yes_bid_dollars   = float(m.get("yes_bid_dollars") or 0.0),
            yes_ask_dollars   = float(m.get("yes_ask_dollars") or 0.0),
        ))
    if out:
        log.info("%s: %d signals (maker-priced)", model_source, len(out))
    return out


def scan_kxmve_nfl_singlegame_longshot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """KXMVENFLSINGLEGAME longshot NO ≤40% T-12h — verdict §4.1 ($8.6K/yr)."""
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXMVENFLSINGLEGAME",),
        yes_max=0.40, min_hours_to_close=12, max_hours_to_close=None,
        model_source="kxmve_nfl_singlegame_longshot",
    )


def scan_kxmve_nfl_multigame_longshot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """KXMVENFLMULTIGAMEEXTENDED longshot NO ≤40% T-24h — verdict §4.1 ($7K/yr)."""
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXMVENFLMULTIGAMEEXTENDED",),
        yes_max=0.40, min_hours_to_close=24, max_hours_to_close=None,
        model_source="kxmve_nfl_multigame_longshot",
    )


def scan_kxmve_nba_singlegame_longshot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """KXMVENBASINGLEGAME longshot NO ≤40% T-12h — verdict §4.1 ($616/yr)."""
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXMVENBASINGLEGAME",),
        yes_max=0.40, min_hours_to_close=12, max_hours_to_close=None,
        model_source="kxmve_nba_singlegame_longshot",
    )


def scan_kxmve_sports_multigame_longshot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """KXMVESPORTSMULTIGAMEEXTENDED longshot NO ≤40% T-12h — verdict §4.1 ($863/yr)."""
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXMVESPORTSMULTIGAMEEXTENDED",),
        yes_max=0.40, min_hours_to_close=12, max_hours_to_close=None,
        model_source="kxmve_sports_multigame_longshot",
    )


def scan_weather_city_highs_longshot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Weather city highs longshot NO ≤25% T-12h — verdict §4.2 ($9K/yr).

    Replaces the bot's directional weather strategies per Migration Plan §5.3.
    """
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXHIGH",),
        yes_max=0.25, min_hours_to_close=12, max_hours_to_close=None,
        model_source="weather_longshot",
    )


def scan_a1_mention_no(markets: list[dict], max_contracts: int) -> list[Signal]:
    """A1 mention markets NO bet, p ∈ [0, 0.50], T-12h — verdict §4.3 ($2.5K/yr).

    CORRECTED FROM EARLIER YES BET — verdict §4.3 §S.4.A1 bug class.
    """
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXTRUMPMENTION", "KXSECPRESSMENTION", "KXVANCEMENTION"),
        yes_max=0.50, min_hours_to_close=6, max_hours_to_close=24,
        model_source="a1_mention_no",
    )


def scan_crypto_daily_longshot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Crypto daily longshot NO ≤25% T-12h — verdict §4.4 ($1.8K/yr)."""
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXBTCD", "KXETHD"),
        yes_max=0.25, min_hours_to_close=12, max_hours_to_close=None,
        model_source="crypto_daily_longshot",
    )


def scan_political_longshot(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Political longshot NO ≤25% T-12h to T-24h — verdict §4.5 ($1.4K/yr)."""
    return _scan_longshot_no(
        markets, max_contracts,
        prefixes=("KXTRUMPMENTION", "APRPOTUS", "538APPROVE"),
        yes_max=0.25, min_hours_to_close=12, max_hours_to_close=None,
        model_source="political_longshot",
    )
