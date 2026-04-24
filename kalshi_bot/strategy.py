"""
strategy_v2.py — Universal Kalshi scanner with fee-adjusted edge detection.

Markets covered:
  FOMC    — Internal monotonicity arbitrage across T-level contracts
             + FRED rate trend as directional bias
  Weather — NOAA NWS free API (precipitation, temperature thresholds)
  Economic — FRED API for CPI/jobs/GDP threshold markets
  Sports  — ESPN public API for game outcome markets

Fee model (Kalshi):
  Fee = 7% of net winnings per side.
  Net payout on YES win: (1 - entry_price) * 0.93
  Net payout on NO win:  entry_price * 0.93
  Minimum edge to be fee-positive: ~8¢ (set threshold to 12¢ for safety margin)

Tax reserve:
  30% of paper profits are tracked as tax liability (US short-term capital gains).
  Stored in state, not deducted from balance.
"""

import asyncio
import logging
import math as _math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta
from typing import Optional

import httpx

from kalshi_bot.models.fomc import (
    fair_value_with_confidence as _fomc_fair_value,
    set_current_fed_rate       as _set_fomc_rate,
    parse_fomc_ticker          as _parse_fomc_ticker,
)

log = logging.getLogger(__name__)

# ── Shared async HTTP client (one connection pool for the process lifetime) ────
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    """Return the process-wide httpx.AsyncClient, creating it on first call.

    keepalive_expiry=10 ensures stale server-side-closed connections are not
    reused, which prevents multi-minute hangs when the far end sends FIN but
    the OS hasn't detected the dead socket via TCP keepalive yet.
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout         = 8.0,
            follow_redirects= True,
            limits          = httpx.Limits(
                max_connections     = 20,
                max_keepalive_connections = 10,
                keepalive_expiry    = 10,   # discard idle connections after 10s
            ),
        )
    return _http_client

# ── Fee constants ──────────────────────────────────────────────────────────────
KALSHI_FEE_RATE   = 0.07    # 7% of net winnings
TAX_RESERVE_RATE  = 0.30    # 30% tax reserve on profits
MIN_EDGE_GROSS    = 0.12    # 12¢ minimum gross edge (net ~5¢ after fees)
MIN_LIQUIDITY_FP  = 0.0   # minimum volume_fp to consider market tradeable

# ── YES price gate: model_source values that are EXEMPT ───────────────────────
# Arb and coherence signals derive edge from relative mispricing between
# contracts, not from the absolute price level of the YES contract.  The
# historical analysis that calibrated MIN_YES_ENTRY_PRICE was performed on
# directional signals only.  These sources bypass that gate wherever it is
# checked, even if a future refactor moves gate logic.
_PRICE_GATE_EXEMPT_SOURCES: frozenset = frozenset({
    "fomc_butterfly_arb",
    "monotonicity_arb",
    "calendar_spread_arb",
    "gdp_fomc_coherence",
    "cross_series_coherence",
})

# ── Signal dataclass ──────────────────────────────────────────────────────────
@dataclass
class Signal:
    """Standardised trading signal produced by a strategy scanner, ready for execution sizing."""
    ticker:            str
    title:             str
    category:          str          # "fomc", "weather", "economic", "sports", "arb"
    side:              str          # "yes" or "no"
    fair_value:        float        # model-derived probability
    market_price:      float        # current Kalshi mid price
    edge:              float        # gross edge = abs(fair_value - market_price)
    fee_adjusted_edge: float        # edge after Kalshi fees
    contracts:         int
    confidence:        float        # 0-1
    model_source:      str
    spread_cents:      Optional[int] = None
    book_depth:        int = 0
    meeting:           Optional[str] = None   # FOMC only
    outcome:           Optional[str] = None   # FOMC only
    arb_partner:       Optional[str] = None   # for pure arb pairs
    # Multi-leg arb: list of {"ticker", "side", "price_cents"} dicts.
    # Set on butterfly and any future N-leg structural arbs.
    arb_legs:          Optional[list] = None
    close_time:        Optional[str] = None

    def net_payout(self) -> float:
        """Expected net profit per dollar risked after fees."""
        if self.side == "yes":
            win_payout = (1.0 - self.market_price) * (1 - KALSHI_FEE_RATE)
            return self.fair_value * win_payout - (1 - self.fair_value) * self.market_price
        else:
            win_payout = self.market_price * (1 - KALSHI_FEE_RATE)
            return self.fair_value * win_payout - (1 - self.fair_value) * (1 - self.market_price)

    def tax_reserve(self) -> float:
        """Dollar amount to reserve for taxes on this potential win."""
        if self.side == "yes":
            gross_win = (1.0 - self.market_price) * self.contracts
        else:
            gross_win = self.market_price * self.contracts
        return gross_win * TAX_RESERVE_RATE


def signal_quality_score(sig: "Signal") -> float:
    """
    Return a 0.0-1.0 composite quality score for a signal.

    Components:
      - Edge magnitude (higher = better): min(1.0, abs(edge) / 0.10) × 0.30
      - Confidence:                        confidence × 0.25
      - Spread ratio (lower = better):     max(0.0, 1.0 - spread_cents/20.0) × 0.20
      - Volume/liquidity proxy:            min(1.0, yes_bid_volume/100) × 0.15
      - Source diversity (CME+Kalshi):     0.10 if CME data present, else 0.0

    Logs the score at DEBUG level.
    """
    edge_score   = min(1.0, abs(getattr(sig, "edge", 0.0)) / 0.10) * 0.30
    conf_score   = float(getattr(sig, "confidence", 0.0)) * 0.25

    spread_cents = getattr(sig, "spread_cents", None) or 0
    spread_score = max(0.0, 1.0 - spread_cents / 20.0) * 0.20

    volume       = getattr(sig, "book_depth", 0) or 0
    volume_score = min(1.0, volume / 100.0) * 0.15

    model_source = getattr(sig, "model_source", "") or ""
    has_cme      = any(s in model_source.lower() for s in ("cme", "zq", "fedwatch", "sr1", "lognormal"))
    source_score = 0.10 if has_cme else 0.0

    score = edge_score + conf_score + spread_score + volume_score + source_score
    log.debug("Signal quality score for %s: %.3f", getattr(sig, "ticker", "?"), score)
    return score


def _fee_adjusted_edge(fair_value: float, market_price: float, side: str) -> float:
    """Compute edge net of Kalshi fees.

    Convention: fair_value = P(YES wins), market_price = YES market price, for both sides.
    YES: win = (1-price)*(1-fee), lose = price.
    NO:  win = price*(1-fee), lose = (1-price). P(NO wins) = 1-fair_value.
    """
    if side == "yes":
        net_win  = (1.0 - market_price) * (1 - KALSHI_FEE_RATE)
        net_lose = market_price
        ev = fair_value * net_win - (1 - fair_value) * net_lose
    else:
        # P(NO wins) = 1 - fair_value; profit = YES_price*(1-fee); loss = NO_price
        net_win  = market_price * (1 - KALSHI_FEE_RATE)
        net_lose = 1.0 - market_price
        ev = (1 - fair_value) * net_win - fair_value * net_lose
    return ev


def _apply_regime_confidence(confidence: float, signal_side: str, macro_regime: dict) -> float:
    """Scale signal confidence based on current macro regime alignment.

    When macro signals align with the trade direction, boost confidence.
    When they conflict, reduce confidence.
    """
    if not macro_regime:
        return confidence

    t10y2y   = macro_regime.get("t10y2y")
    pce      = macro_regime.get("pce_yoy")
    core_cpi = macro_regime.get("core_cpi_yoy")
    icsa     = macro_regime.get("icsa")
    vix      = macro_regime.get("vix")

    boost   = 0.0
    penalty = 0.0

    is_cut_signal = "CUT" in (macro_regime.get("_outcome", "") or "")

    # For CUT signals: alignment means easing conditions
    if signal_side in ("yes",) and is_cut_signal:
        if t10y2y is not None and t10y2y < -0.25:
            boost += 0.03   # inverted curve aligns with cut bet
        if pce is not None and pce < 2.2:
            boost += 0.02   # inflation controlled = cut aligned
        if icsa is not None and icsa > 280_000:
            boost += 0.02   # labor softening = cut aligned

    # For HOLD signals: alignment means stable conditions
    # For HIKE signals: alignment means inflationary conditions
    if core_cpi is not None and core_cpi > 3.5:
        if is_cut_signal:
            penalty += 0.05  # high inflation conflicts with cut bet
        else:
            boost += 0.02    # high inflation aligns with hold/hike

    # VIX penalty for all signals during extreme uncertainty
    if vix is not None and vix > 35:
        penalty += 0.04      # extreme fear = unreliable signals

    adjusted = max(0.40, min(0.95, confidence + boost - penalty))
    return adjusted


def _compute_surprise_factor(series_type: str, actual: float, prior: float) -> float:
    """
    Estimate whether BLS data was a surprise vs prior period.
    A surprise (large deviation from prior) reduces confidence in forward
    rate predictions because the market is actively repricing.

    Returns a multiplier: 0.7 (big surprise) to 1.0 (no surprise) to 1.1 (confirming trend).
    """
    if series_type == "CPI":
        change = abs(actual - prior)
        if change > 0.5:    return 0.70   # huge CPI surprise — market repricing
        elif change > 0.3:  return 0.80   # notable surprise
        elif change > 0.1:  return 0.90   # small surprise
        else:               return 1.05   # confirming trend — higher confidence
    elif series_type == "NFP":
        change = abs(actual - prior)
        if change > 200_000:   return 0.72
        elif change > 100_000: return 0.82
        elif change > 50_000:  return 0.93
        else:                  return 1.05
    return 1.0


def _market_mid(market: dict) -> float:
    """Extract mid-price from new Kalshi API response format.

    Returns 0.0 (filtered out by all callers) when no real price data exists,
    so markets with missing bid/ask/last never generate signals.
    """
    yes_bid = float(market.get("yes_bid_dollars") or 0)
    yes_ask = float(market.get("yes_ask_dollars") or 1)
    last    = float(market.get("last_price_dollars") or 0)
    if yes_bid > 0 and yes_ask <= 1.0 and yes_ask >= yes_bid:
        return (yes_bid + yes_ask) / 2.0
    return last if last > 0 else 0.0


def _market_volume(market: dict) -> float:
    return float(market.get("volume_fp") or 0)


# ── Crypto price model (log-normal) ───────────────────────────────────────────

def _parse_crypto_ticker(ticker: str) -> Optional[dict]:
    """
    Parse a KXBTC or KXETH ticker into its components.

    Examples:
      KXBTC-26APR1512-T83799.99  →  asset=BTC, direction=above, threshold=83799.99
      KXBTC-26APR1512-B74000     →  asset=BTC, direction=below, threshold=74000
      KXETH-26APR1512-B1600      →  asset=ETH, direction=below, threshold=1600

    Ticker suffix:
      T<price>  = YES wins if price ends ABOVE threshold  (T = top / threshold above)
      B<price>  = YES wins if price ends BELOW threshold  (B = below)
    """
    # Match KXBTC or KXETH tickers
    m = re.match(
        r"^(KXBTC|KXETH)-(\d{2})([A-Z]{3})(\d{2})(\d{2})-([TB])(\d+\.?\d*)$",
        ticker,
    )
    if not m:
        return None
    asset_code, yr2, mon_str, day, hr, direction_char, price_str = m.groups()
    asset = "BTC" if asset_code == "KXBTC" else "ETH"
    direction = "above" if direction_char == "T" else "below"
    threshold = float(price_str)

    # Parse close time
    month_map = {
        "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
        "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12,
    }
    mon = month_map.get(mon_str)
    if not mon:
        return None
    year = 2000 + int(yr2)
    try:
        close_dt = datetime(year, mon, int(day), int(hr), 0, tzinfo=timezone.utc)
    except ValueError:
        return None

    return {
        "asset":     asset,
        "direction": direction,   # "above" or "below"
        "threshold": threshold,
        "close_dt":  close_dt,
    }


def _lognormal_prob_above(
    spot:       float,
    threshold:  float,
    hours:      float,
    annual_vol: float = 0.80,
) -> float:
    """
    P(S_T > threshold) under log-normal dynamics.

    Uses Black-Scholes binary call probability:
      d = (ln(spot/K) + 0.5*σ²*t) / (σ*√t)
      P(S_T > K) = N(d)
    where t is in years.
    """
    if hours <= 0 or threshold <= 0 or spot <= 0:
        return 0.5
    t = max(hours / 8_760.0, 1 / 8_760.0)   # floor at 1 hour
    sigma_t = annual_vol * _math.sqrt(t)
    if sigma_t == 0:
        return 1.0 if spot > threshold else 0.0
    d = (_math.log(spot / threshold) + 0.5 * sigma_t ** 2) / sigma_t
    # N(d) via error function: N(d) = 0.5*(1 + erf(d/sqrt(2)))
    return 0.5 * (1.0 + _math.erf(d / _math.sqrt(2)))


# ── FOMC Arbitrage Model ───────────────────────────────────────────────────────

def _group_fomc_by_meeting(markets: list[dict]) -> dict[str, list[dict]]:
    """Group KXFED markets by event ticker (meeting date)."""
    groups: dict[str, list[dict]] = {}
    for m in markets:
        event = m.get("event_ticker", "")
        if not event:
            continue
        groups.setdefault(event, []).append(m)
    return groups


def _extract_strike(ticker: str) -> Optional[float]:
    """Extract the rate strike from KXFED-27APR-T4.25 → 4.25"""
    match = re.search(r"-T(\d+\.\d+|\d+)$", ticker)
    return float(match.group(1)) if match else None


def _scan_ladder_arb_core(
    groups: dict[str, list[dict]],
    max_contracts: int,
    series_tag: str = "fomc",
) -> list[Signal]:
    """
    Shared monotonicity + butterfly arb core for any threshold-ladder series.

    Invariant: for any series measuring P(metric > X), the CDF must be strictly
    decreasing as X increases. Violations (lower strike priced below higher strike)
    and convexity violations (butterfly) are both tradeable.

    Args:
        groups:       Markets grouped by event_ticker (e.g. one FOMC meeting, or one CPI release).
        max_contracts: Per-signal contract cap.
        series_tag:   Short tag used in model_source and log lines (e.g. "fomc", "cpi").
    """
    mono_candidates: list[tuple[float, Signal]] = []
    butterfly_signals: list[Signal] = []

    for event, group in groups.items():
        priced = [(m, _extract_strike(m["ticker"]), _market_mid(m)) for m in group]
        priced = [(m, s, p) for m, s, p in priced if s is not None and p > 0.01]
        if len(priced) < 2:
            continue
        priced.sort(key=lambda x: x[1])  # ascending strike

        # ── Monotonicity check ────────────────────────────────────────────────
        for i in range(len(priced) - 1):
            m_low, strike_low, price_low   = priced[i]
            m_high, strike_high, price_high = priced[i + 1]

            yes_ask_low  = float(m_low.get("yes_ask_dollars") or price_low + 0.02)
            yes_bid_high = float(m_high.get("yes_bid_dollars") or price_high - 0.02)

            if yes_ask_low < yes_bid_high - 0.01:
                arb_profit = yes_bid_high - yes_ask_low
                fee_cost   = arb_profit * KALSHI_FEE_RATE * 2
                net_profit = arb_profit - fee_cost

                if net_profit >= MIN_EDGE_GROSS * 0.5:
                    contracts  = min(max_contracts, max(1, int(net_profit * 50)))
                    edge_cents = yes_bid_high - yes_ask_low
                    sig = Signal(
                        ticker            = m_low["ticker"],
                        title             = f"ARB: Buy {m_low['ticker']} YES + Buy {m_high['ticker']} NO",
                        category          = "arb",
                        side              = "yes",
                        fair_value        = yes_bid_high,
                        market_price      = yes_ask_low,
                        edge              = arb_profit,
                        fee_adjusted_edge = net_profit,
                        contracts         = contracts,
                        confidence        = 0.95,
                        model_source      = f"{series_tag}_monotonicity_arb",
                        arb_partner       = m_high["ticker"],
                        meeting           = event,
                    )
                    mono_candidates.append((edge_cents, sig))
                    log.info(
                        "%s ARB: Buy %s YES@%.2f + Buy %s NO@%.2f → net %.2f¢",
                        series_tag.upper(), m_low["ticker"], yes_ask_low,
                        m_high["ticker"], 1 - yes_bid_high, net_profit * 100,
                    )

        # ── Butterfly convexity check ─────────────────────────────────────────
        if len(priced) < 3:
            continue

        BUTTERFLY_THRESHOLD = 0.04

        for i in range(len(priced) - 2):
            m_a, strike_a, p_a = priced[i]
            m_b, strike_b, p_b = priced[i + 1]
            m_c, strike_c, p_c = priced[i + 2]

            gap_lo = round(strike_b - strike_a, 4)
            gap_hi = round(strike_c - strike_b, 4)
            if abs(gap_lo - gap_hi) > 1e-6:
                continue

            convexity_slack = p_a + p_c - 2 * p_b
            if convexity_slack >= -BUTTERFLY_THRESHOLD:
                continue

            bf_mid_b = (p_a + p_c) / 2.0
            legs = [
                (abs(p_a - bf_mid_b), m_a, strike_a, p_a),
                (abs(p_b - bf_mid_b), m_b, strike_b, p_b),
                (abs(p_c - bf_mid_b), m_c, strike_c, p_c),
            ]
            worst_dev, worst_m, worst_strike, worst_price = max(legs, key=lambda x: x[0])
            contracts = min(max_contracts, max(1, int(worst_dev * 50)))

            _bf_arb_legs = [
                {"ticker": m_a["ticker"], "side": "yes", "price_cents": max(1, int(p_a * 100))},
                {"ticker": m_b["ticker"], "side": "no",  "price_cents": max(1, int((1.0 - p_b) * 100))},
                {"ticker": m_c["ticker"], "side": "yes", "price_cents": max(1, int(p_c * 100))},
            ]
            sig = Signal(
                ticker            = worst_m["ticker"],
                title             = (
                    f"BUTTERFLY: {m_a['ticker']}/{m_b['ticker']}/{m_c['ticker']} "
                    f"leg {worst_m['ticker']} off by {worst_dev * 100:.1f}¢"
                ),
                category          = "arb",
                side              = "yes",
                fair_value        = bf_mid_b,
                market_price      = worst_price,
                edge              = worst_dev,
                fee_adjusted_edge = worst_dev * (1 - KALSHI_FEE_RATE * 2),
                contracts         = contracts,
                confidence        = 0.70,
                model_source      = f"{series_tag}_butterfly_arb",
                meeting           = event,
                arb_legs          = _bf_arb_legs,
            )
            butterfly_signals.append(sig)
            log.info(
                "%s BUTTERFLY: %s/%s/%s P=%.2f/%.2f/%.2f slack=%.2f¢ worst=%s dev=%.2f¢",
                series_tag.upper(),
                m_a["ticker"], m_b["ticker"], m_c["ticker"],
                p_a, p_b, p_c, convexity_slack * 100,
                worst_m["ticker"], worst_dev * 100,
            )

    mono_candidates.sort(key=lambda x: x[0], reverse=True)
    signals = [sig for _, sig in mono_candidates]
    signals.extend(butterfly_signals)
    return signals


def scan_fomc_arb(markets: list[dict], max_contracts: int) -> list[Signal]:
    """
    Detect monotonicity violations in FOMC T-level contracts.

    For a given meeting, P(rate > X) must decrease as X increases.
    If P(rate > 4.00%) < P(rate > 4.25%), that is impossible and tradeable.

    Pure arb: Buy lower-strike YES + Buy higher-strike NO = risk-free profit.
    Cost = yes_ask(lower) + no_ask(higher) = yes_ask(lower) + (1 - yes_bid(higher))
    For arb to exist: yes_ask(lower) + (1 - yes_bid(higher)) < 1.0
    i.e.: yes_ask(lower) < yes_bid(higher)

    Enhancement 1 — CDF rank-ordering:
    Violations are ranked by edge_cents = yes_bid(higher) - yes_ask(lower), so the
    highest-EV arb appears first in the published signal stream.

    Enhancement 2 — Butterfly spread detection:
    For three consecutive equal-spaced strikes A < B < C a valid CDF must be convex:
    P(A) + P(C) >= 2*P(B). A violation signals that the middle strike is overpriced
    relative to the wings and a 3-leg spread can capture the mispricing.
    """
    return _scan_ladder_arb_core(_group_fomc_by_meeting(markets), max_contracts, series_tag="fomc")


# Economic series prefixes that have threshold-ladder markets (same CDF invariant as FOMC)
_ECONOMIC_LADDER_PREFIXES = ("KXCPI", "KXUNRATE", "KXGDP", "KXNFP", "KXPCE", "KXGDPQ")


def scan_economic_ladder_arb(markets: list[dict], max_contracts: int) -> list[Signal]:
    """
    Monotonicity + butterfly arb for economic threshold-ladder series.

    Same invariant as FOMC: P(metric > T1) >= P(metric > T2) when T1 < T2.
    Applies to KXCPI, KXUNRATE, KXGDP, KXNFP, KXPCE, KXGDPQ.
    Markets in each series are grouped by event_ticker (the release date).
    """
    eco_markets = [
        m for m in markets
        if any(m.get("ticker", "").startswith(p) for p in _ECONOMIC_LADDER_PREFIXES)
    ]
    if not eco_markets:
        return []
    groups = _group_fomc_by_meeting(eco_markets)   # groups by event_ticker — works for any series
    signals = _scan_ladder_arb_core(groups, max_contracts, series_tag="econ")
    if signals:
        log.info("Economic ladder arb: %d signals across %d groups", len(signals), len(groups))
    return signals


async def scan_fomc_directional(
    markets:              list[dict],
    current_rate:         float,
    max_contracts:        int,
    treasury_2y:          Optional[float] = None,
    macro_regime:         Optional[dict]  = None,
    release_data:         Optional[dict]  = None,
    min_yes_entry_price:  Optional[float] = None,
) -> list[Signal]:
    """
    Scan KXFED markets for directional edges using the FOMC probability model.

    Fair value is derived from CME FedWatch + ZQ futures + WSJ consensus (via
    kalshi_bot.models.fomc); confidence is adjusted by meeting proximity and the
    2Y Treasury spread.  Falls back to a FRED rate-anchor linear decay when the
    primary model returns None.

    Args:
        markets: Open KXFED market dicts from the Kalshi API.
        current_rate: Current effective federal-funds rate (decimal, e.g. 3.75).
        max_contracts: Per-signal contract cap passed through to sizing.
        treasury_2y: Optional 2Y Treasury yield; used to apply a regime confidence delta.
        macro_regime: Optional dict from the ep:macro Redis hash for regime context.
        release_data: Optional dict from the ep:releases Redis hash for scheduled events.
        min_yes_entry_price: Minimum Kalshi mid price required to enter a YES side;
            overrides the config default when supplied (e.g. calibrated by ep_advisor).

    Returns:
        List of Signal objects with category="fomc" that clear the minimum edge threshold.
    """
    signals = []
    groups  = _group_fomc_by_meeting(markets)

    # min_yes_entry_price: caller-supplied override wins (e.g., calibrated from
    # resolution DB by ep_advisor); otherwise fall back to env/config default.
    if min_yes_entry_price is not None:
        _cfg_min_yes_entry_price: float = min_yes_entry_price
    else:
        try:
            import kalshi_bot.config as _kbc
            _cfg_min_yes_entry_price = _kbc.MIN_YES_ENTRY_PRICE
        except Exception:
            _cfg_min_yes_entry_price = 0.60

    # Keep parse_fomc_ticker in sync with the live rate passed in from FRED
    _set_fomc_rate(current_rate)

    # ── 2Y Treasury spread adjustment (additive delta, applied after proximity) ─
    tsy_conf_adj = 0.0
    if treasury_2y is not None:
        spread_bps = (treasury_2y - current_rate) * 100
        if spread_bps < -75:
            tsy_conf_adj = +0.06
        elif spread_bps < -40:
            tsy_conf_adj = +0.03
        elif spread_bps > 40:
            tsy_conf_adj = -0.04
        log.debug("2Y spread: %.1fbps → conf adj %+.2f", spread_bps, tsy_conf_adj)

    # Pre-fetch fomc model probabilities for all tickers concurrently.
    # Builds a flat list so we can run a single asyncio.gather().
    all_markets_flat = [m for group in groups.values() for m in group]
    fomc_results: dict[str, tuple] = {}  # ticker → (fair_yes, model_conf)

    async def _fetch_one(ticker: str, price: float) -> None:
        try:
            fv, mc = await _fomc_fair_value(ticker, price)
            if fv is not None:
                fomc_results[ticker] = (fv, mc)
        except Exception as exc:
            log.debug("fomc model fetch failed for %s: %s", ticker, exc)

    # Only fetch markets with a real price (≤0.01 = no bid, skip silently)
    priceable = [m for m in all_markets_flat if _market_mid(m) > 0.01]
    await asyncio.gather(*[
        _fetch_one(m["ticker"], _market_mid(m))
        for m in priceable
    ])

    model_hits   = len(fomc_results)
    model_misses = len(priceable) - model_hits   # misses among priceable tickers only
    n_meetings_with_data = len(
        {v[0:7] for k in fomc_results
         for v in [(_parse_fomc_ticker(k) or {}).get("meeting", "") or ""]
         if v}
    )
    log.info(
        "FOMC directional: model hits=%d (full_model) fallback=%d (fred_anchor) "
        "meetings=%d priceable=%d total=%d",
        model_hits, model_misses, n_meetings_with_data, len(priceable), len(all_markets_flat),
    )

    for event, group in groups.items():
        for market in group:
            ticker = market["ticker"]
            strike = _extract_strike(ticker)
            if strike is None:
                continue

            price = _market_mid(market)
            vol   = _market_volume(market)
            if price <= 0.01 or price >= 0.99:
                continue
            if vol < MIN_LIQUIDITY_FP:
                continue

            # ── Fair value: real model or FRED-anchor fallback ────────────────
            if ticker in fomc_results:
                fair_yes, model_conf = fomc_results[ticker]
                # Apply proximity adjustment to model confidence, then 2Y delta
                confidence = max(0.40, min(0.95,
                    _fomc_proximity_confidence(model_conf) + tsy_conf_adj))
                model_src  = "fedwatch+zq+wsj"
            else:
                # FRED rate-anchor fallback (unchanged from prior implementation)
                rate_diff_bps = (strike - current_rate) * 100
                if rate_diff_bps <= 0:
                    cuts_to_fail = abs(rate_diff_bps) / 25.0
                    if cuts_to_fail <= 3:
                        base_fv = max(0.50, 0.88 - cuts_to_fail * 0.07)
                    else:
                        base_fv = min(0.90, 0.55 + cuts_to_fail * 0.03)
                    try:
                        _MM = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                        _p  = event.split("-")
                        _yr = 2000 + int(_p[1][:2])
                        _mo = _MM.get(_p[1][2:], 6)
                        _now = datetime.now()
                        _months_out = (_yr - _now.year) * 12 + (_mo - _now.month)
                        _mtgs = max(1, round(_months_out / 1.5))
                    except Exception:
                        _mtgs = 4
                    decay    = max(0.50, 1.0 - (_mtgs - 1) * 0.05)
                    fair_yes = price + (base_fv - price) * decay
                elif rate_diff_bps <= 25:
                    fair_yes = 0.12
                elif rate_diff_bps <= 50:
                    fair_yes = 0.04
                elif rate_diff_bps <= 75:
                    fair_yes = 0.02
                else:
                    fair_yes = 0.01
                # Fallback: lower bar — hardcoded rate table is less reliable than
                # live CME/ZQ data; 0.55 base prevents marginal trades from firing
                confidence = max(0.40, min(0.80,
                    _fomc_proximity_confidence(0.55) + tsy_conf_adj))
                model_src  = f"fred_anchor_{current_rate:.2f}%"

            fair_yes = max(0.01, min(0.99, fair_yes))

            diff = fair_yes - price
            if abs(diff) < MIN_EDGE_GROSS:
                continue

            side = "yes" if diff > 0 else "no"

            # Skip YES signals where the target is below the model's resolution.
            # OUTCOME_BPS covers at most CUT_100 (-100bp), so any target below
            # current_rate - 1.00 causes _cumulative_yes_prob to saturate at 0.99
            # for every model outcome — the edge is a left-tail truncation artifact,
            # not real alpha.  The market correctly prices in recession tail risk
            # (rates going to 0%) that the single-meeting model cannot represent.
            if side == "yes" and strike <= current_rate - 1.00:
                continue
            # Skip NO signals where the target is above the model's resolution.
            # OUTCOME_BPS covers at most HIKE_50 (+50bp), so any target above
            # current_rate + 0.50 causes _cumulative_yes_prob to floor at 0.01
            # for every model outcome — the edge is a right-tail truncation artifact,
            # not real alpha.
            if side == "no" and strike > current_rate + 0.50:
                continue

            # ── FIX 1: Suppress low-probability KXFED YES signals ────────────
            # Analysis of 608 live trades shows YES entries below 60¢ market
            # price produce 11-13% win rates and avg -55¢ to -116¢/trade loss.
            # YES entries above 60¢ are profitable (+$51.64 at 60-80¢,
            # +$142.72 at 80-100¢).  Only apply to KXFED directional signals —
            # non-FOMC markets (GDP, weather, etc.) are unaffected.
            # Arb/coherence model sources are exempt: their edge comes from
            # relative mispricing, not absolute price level (see _PRICE_GATE_EXEMPT_SOURCES).
            if (
                side == "yes"
                and ticker.startswith("KXFED")
                and price < _cfg_min_yes_entry_price
                and model_src not in _PRICE_GATE_EXEMPT_SOURCES
            ):
                log.info(
                    "Suppressing low-probability YES: %s market_price=%.2f "
                    "< MIN_YES_ENTRY_PRICE=%.2f (model=%s)",
                    ticker, price, _cfg_min_yes_entry_price, model_src,
                )
                continue

            edge = abs(diff)
            fair_for_side = fair_yes if side == "yes" else (1 - fair_yes)
            fee_edge = _fee_adjusted_edge(fair_for_side, price if side == "yes" else (1 - price), "yes")

            if fee_edge < MIN_EDGE_GROSS * 0.5:
                continue

            # ── Task 3: Graduated spread-to-edge penalty ─────────────────────
            _yes_bid = float(market.get("yes_bid_dollars") or 0)
            _yes_ask = float(market.get("yes_ask_dollars") or 0)
            _spread_cents = int(abs(_yes_ask - _yes_bid) * 100) if (_yes_bid > 0 and _yes_ask > 0) else 0
            _fee_edge_cents = max(1, int(fee_edge * 100))
            spread_ratio = _spread_cents / _fee_edge_cents
            if spread_ratio > 2.0:
                continue   # spread more than 2x edge — guaranteed negative EV
            elif spread_ratio > 1.0:
                confidence *= 0.80   # tight but tradeable — reduce confidence
            elif spread_ratio < 0.5:
                confidence *= 1.05   # wide edge vs spread — slight confidence boost

            # ── Task 1: Regime-based confidence adjustment ────────────────────
            if macro_regime:
                # Pass the outcome string from the parsed ticker so _apply_regime_confidence
                # can determine if this is a CUT signal
                _parsed_for_regime = _parse_fomc_ticker(ticker)
                _outcome_str = (_parsed_for_regime or {}).get("outcome", "") or ""
                _regime_copy = dict(macro_regime)
                _regime_copy["_outcome"] = _outcome_str
                confidence = _apply_regime_confidence(confidence, side, _regime_copy)
                log.debug(
                    "Regime adj %s %s: outcome=%s conf=%.3f",
                    ticker, side, _outcome_str, confidence,
                )

            # ── Task 2: BLS release surprise factor ───────────────────────────
            if release_data:
                _now_utc = datetime.now(timezone.utc)
                for _series_type in ("CPI", "NFP"):
                    _rel = release_data.get(_series_type)
                    if not _rel:
                        continue
                    try:
                        _rel_time = _rel.get("timestamp")
                        # Accept both datetime objects and ISO strings
                        if isinstance(_rel_time, str):
                            _rel_time = datetime.fromisoformat(_rel_time)
                        if _rel_time and (_now_utc - _rel_time).total_seconds() <= 7200:
                            _actual = float(_rel["actual"])
                            _prior  = float(_rel["prior"])
                            _factor = _compute_surprise_factor(_series_type, _actual, _prior)
                            if _factor != 1.0:
                                log.info(
                                    "BLS %s surprise factor %.2f applied to %s "
                                    "(actual=%.2f prior=%.2f)",
                                    _series_type, _factor, ticker, _actual, _prior,
                                )
                            confidence = max(0.40, min(0.95, confidence * _factor))
                    except (KeyError, TypeError, ValueError) as _exc:
                        log.debug("BLS release_data parse error for %s: %s", _series_type, _exc)

            confidence = max(0.40, min(0.95, confidence))

            contracts = min(max_contracts, max(1, int(edge * 80)))
            _parsed   = _parse_fomc_ticker(ticker)
            signals.append(Signal(
                ticker            = ticker,
                title             = market.get("title", ""),
                category          = "fomc",
                side              = side,
                fair_value        = fair_yes if side == "yes" else (1 - fair_yes),
                market_price      = price,
                edge              = round(edge, 4),
                fee_adjusted_edge = round(fee_edge, 4),
                contracts         = contracts,
                confidence        = round(confidence, 3),
                model_source      = model_src,
                meeting           = event,
                outcome           = (_parsed or {}).get("outcome", ""),
                spread_cents      = _spread_cents if _spread_cents > 0 else None,
            ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)

    # Outcome distribution — helps diagnose model regime / signal concentration
    if signals:
        outcome_dist: dict[str, int] = {}
        for s in signals:
            outcome_dist[s.outcome or "?"] = outcome_dist.get(s.outcome or "?", 0) + 1
        log.debug("FOMC outcome dist: %s", outcome_dist)

    return signals


# ── Crypto Price Range Scanner (KXBTC / KXETH) ────────────────────────────────

# Annualised historical volatility estimates (conservative fallbacks)
_BTC_ANNUAL_VOL = 0.80   # ~80% — typical for BTC (used if Deribit DVOL unavailable)
_ETH_ANNUAL_VOL = 0.90   # ~90% — slightly higher than BTC

# ── Deribit DVOL cache ────────────────────────────────────────────────────────
_dvol_cache: dict = {"value": None, "ts": 0.0}


def _fetch_deribit_dvol() -> float:
    """Fetch BTC 30-day implied vol from Deribit public API. No auth required."""
    import time, requests
    if time.time() - _dvol_cache["ts"] < 300:  # 5-min cache
        return _dvol_cache["value"] or 0.80
    try:
        r = requests.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            params={"currency": "BTC", "resolution": "3600", "count": 1},
            timeout=5
        )
        data = r.json()["result"]["data"]
        if data:
            # data is [[timestamp_ms, open, high, low, close], ...]
            dvol_pct = data[-1][4] / 100.0  # close value, convert from % to decimal
            _dvol_cache["value"] = dvol_pct
            _dvol_cache["ts"] = time.time()
            return dvol_pct
    except Exception:
        pass
    return _dvol_cache.get("value") or 0.80


async def _fetch_eth_spot() -> Optional[float]:
    """Fetch ETH-USD spot from Coinbase public API."""
    try:
        http  = _get_http_client()
        resp  = await http.get("https://api.coinbase.com/v2/prices/ETH-USD/spot", timeout=5.0)
        if resp.status_code == 200:
            return float(resp.json()["data"]["amount"])
    except Exception as exc:
        log.debug("ETH spot fetch failed: %s", exc)
    return None


def scan_crypto_price_markets(
    markets:      list[dict],
    btc_spot:     Optional[float],
    eth_spot:     Optional[float],
    max_contracts: int = 5,
) -> list[Signal]:
    """
    Score KXBTC and KXETH daily price-range markets using a log-normal model.

    Each market is a binary bet: "Will BTC/ETH be above/below $X at time T?"
    Fair value = P(spot_T > threshold) or P(spot_T < threshold) using log-normal dynamics.

    Only generates signals when:
      - Current spot price is within ~20% of the threshold (meaningful edge zone)
      - Market has some bid/ask (not completely illiquid)
      - Hours until close > 0.5 (not expiring in < 30 min)
    """
    signals = []
    now     = datetime.now(timezone.utc)

    crypto_markets = [
        m for m in markets
        if m.get("ticker","").startswith(("KXBTC-","KXETH-"))
    ]
    if not crypto_markets:
        return signals

    for market in crypto_markets:
        ticker  = market.get("ticker","")
        parsed  = _parse_crypto_ticker(ticker)
        if not parsed:
            continue

        asset     = parsed["asset"]
        direction = parsed["direction"]
        threshold = parsed["threshold"]
        close_dt  = parsed["close_dt"]

        # Select spot price for this asset
        spot = btc_spot if asset == "BTC" else eth_spot
        if not spot or spot <= 0:
            continue

        # Hours until close
        hours = (close_dt - now).total_seconds() / 3600
        if hours < 0.5:
            continue   # too close to expiry — skip

        # Only model markets where threshold is within ±25% of spot
        # (markets far out-of-range have near-0 or near-1 prices with no edge)
        if threshold > spot * 1.25 or threshold < spot * 0.75:
            continue

        # Require a real two-sided market — skip phantom/illiquid quotes
        yes_bid = float(market.get("yes_bid_dollars") or 0)
        yes_ask = float(market.get("yes_ask_dollars") or 0)
        liq     = float(market.get("liquidity_dollars") or 0)
        if yes_bid <= 0 or yes_ask <= 0 or liq <= 0:
            continue   # no real order book

        price = (yes_bid + yes_ask) / 2.0
        if price <= 0.01 or price >= 0.99:
            continue   # no edge near certainty

        # Log-normal fair value — use live Deribit DVOL for BTC, fallback for ETH
        annual_vol = _fetch_deribit_dvol() if asset == "BTC" else _ETH_ANNUAL_VOL
        p_above    = _lognormal_prob_above(spot, threshold, hours, annual_vol)
        fair_value = p_above if direction == "above" else (1.0 - p_above)

        # Edge
        if fair_value > price:
            side = "yes"
            edge = fair_value - price
        else:
            side = "no"
            edge = price - fair_value   # buy NO at (1-price)
            # For NO side, market_price is the YES price; fair_value is P(YES)

        fee_edge = _fee_adjusted_edge(fair_value, price, side)
        if fee_edge < 0.04:   # ~4¢ minimum net edge after fees
            continue

        # Confidence scales with how far we are from 50/50 and time remaining
        # More time = more uncertainty = lower confidence
        certainty   = abs(fair_value - 0.5) * 2   # 0 at 50%, 1 at certainty
        time_factor = min(1.0, 1.0 / max(hours, 1))  # higher confidence near close
        confidence  = min(0.95, 0.50 + 0.40 * certainty + 0.10 * time_factor)

        signals.append(Signal(
            ticker            = ticker,
            title             = market.get("title","") or f"{asset} {'above' if direction=='above' else 'below'} ${threshold:,.0f}",
            category          = "crypto_price",
            side              = side,
            fair_value        = fair_value,
            market_price      = price,
            edge              = edge,
            fee_adjusted_edge = fee_edge,
            contracts         = max_contracts,
            confidence        = confidence,
            model_source      = f"lognormal_{asset.lower()}_vol{int(annual_vol*100)}pct",
            spread_cents      = int(abs(
                float(market.get("yes_ask_dollars") or price + 0.02) -
                float(market.get("yes_bid_dollars") or price - 0.02)
            ) * 100),
        ))
        log.debug(
            "Crypto %s %s %s@%.0f: spot=%.2f fair=%.3f mkt=%.3f edge=%.3f  %.1fh left",
            asset, ticker, direction, threshold, spot, fair_value, price, fee_edge, hours,
        )

    signals.sort(key=lambda s: s.fee_adjusted_edge * s.confidence, reverse=True)
    log.info("Crypto price scan: %d signals from %d markets", len(signals), len(crypto_markets))
    return signals


# ── Weather Model (Open-Meteo primary, NOAA NWS secondary) ───────────────────
#
# Open-Meteo is the primary source: free, no auth, JSON daily forecasts with
# calibrated temperature_2m_max/min and precipitation_sum per target date.
# NOAA NWS daily forecast is the secondary cross-check.
# Sigma scales with forecast horizon so uncertainty is properly modelled.

import math as _math
from datetime import date as _date

WEATHER_SERIES = {
    "KXHIGHNY":  {"lat": 40.7128, "lon": -74.0060,  "tz": "America/New_York",    "wfo": "OKX", "x": 33,  "y": 37, "type": "high_temp"},
    "KXLOWNY":   {"lat": 40.7128, "lon": -74.0060,  "tz": "America/New_York",    "wfo": "OKX", "x": 33,  "y": 37, "type": "low_temp"},
    "KXHIGHLA":  {"lat": 34.0522, "lon": -118.2437, "tz": "America/Los_Angeles", "wfo": "LOX", "x": 148, "y": 48, "type": "high_temp"},
    "KXHIGHCHI": {"lat": 41.8781, "lon": -87.6298,  "tz": "America/Chicago",     "wfo": "LOT", "x": 71,  "y": 56, "type": "high_temp"},
    "KXHIGHDC":  {"lat": 38.9072, "lon": -77.0369,  "tz": "America/New_York",    "wfo": "LWX", "x": 98,  "y": 69, "type": "high_temp"},
    "KXRAINY":   {"lat": 40.7128, "lon": -74.0060,  "tz": "America/New_York",    "wfo": "OKX", "x": 33,  "y": 37, "type": "precip"},
}

_MONTH_MAP = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def _parse_ticker_date(ticker: str) -> Optional[_date]:
    """Parse target date from a Kalshi weather ticker like KXHIGHNY-26APR19."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)", ticker)
    if not m:
        return None
    mo = _MONTH_MAP.get(m.group(2))
    if not mo:
        return None
    try:
        return _date(2000 + int(m.group(1)), mo, int(m.group(3)))
    except ValueError:
        return None


async def fetch_open_meteo(
    lat: float, lon: float, tz: str, target: _date
) -> Optional[dict]:
    """
    Fetch Open-Meteo daily forecast for a specific date.
    Returns {"high", "low", "precip", "precip_pct", "days_ahead"} or None.
    """
    try:
        http = _get_http_client()
        params = {
            "latitude":    lat,
            "longitude":   lon,
            "daily":       "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone":    tz,
            "forecast_days": 8,
        }
        resp = await http.get("https://api.open-meteo.com/v1/forecast", params=params)
        if resp.status_code != 200:
            log.warning("Open-Meteo %d for %.4f,%.4f", resp.status_code, lat, lon)
            return None
        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        target_str = target.isoformat()
        if target_str not in dates:
            return None
        idx = dates.index(target_str)
        def _v(key):
            vals = daily.get(key, [])
            return vals[idx] if idx < len(vals) else None
        return {
            "high":       _v("temperature_2m_max"),
            "low":        _v("temperature_2m_min"),
            "precip":     _v("precipitation_sum"),
            "precip_pct": _v("precipitation_probability_max"),
            "days_ahead": (_date.today() - target).days * -1,
            "source":     "open_meteo",
        }
    except Exception as exc:
        log.warning("Open-Meteo fetch failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


async def fetch_open_meteo_ecmwf(
    lat: float, lon: float, tz: str, target: _date
) -> Optional[dict]:
    """
    Fetch ECMWF IFS model forecast from Open-Meteo.
    ECMWF generally outperforms GFS beyond day 3 and is the gold-standard
    global NWP model. Returns same dict shape as fetch_open_meteo().
    """
    try:
        http = _get_http_client()
        params = {
            "latitude":           lat,
            "longitude":          lon,
            "daily":              "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
            "temperature_unit":   "fahrenheit",
            "precipitation_unit": "inch",
            "timezone":           tz,
            "forecast_days":      8,
            "models":             "ecmwf_ifs04",
        }
        resp = await http.get("https://api.open-meteo.com/v1/forecast", params=params)
        if resp.status_code != 200:
            log.warning("Open-Meteo ECMWF %d for %.4f,%.4f", resp.status_code, lat, lon)
            return None
        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        target_str = target.isoformat()
        if target_str not in dates:
            return None
        idx = dates.index(target_str)
        def _v(key):
            vals = daily.get(key, [])
            return vals[idx] if idx < len(vals) else None
        return {
            "high":       _v("temperature_2m_max"),
            "low":        _v("temperature_2m_min"),
            "precip":     _v("precipitation_sum"),
            "precip_pct": _v("precipitation_probability_max"),
            "days_ahead": (_date.today() - target).days * -1,
            "source":     "ecmwf",
        }
    except Exception as exc:
        log.warning("Open-Meteo ECMWF fetch failed for %.4f,%.4f: %s", lat, lon, exc)
        return None


async def fetch_noaa_forecast(wfo: str, x: int, y: int) -> Optional[dict]:
    """
    Fetch NOAA NWS daily (non-hourly) forecast.
    Returns raw NWS JSON or None.
    """
    try:
        http = _get_http_client()
        url  = f"https://api.weather.gov/gridpoints/{wfo}/{x},{y}/forecast"
        resp = await http.get(url, headers={"User-Agent": "KalshiBot/1.0 (prediction-market-research)"})
        if resp.status_code == 200:
            return resp.json()
        log.warning("NOAA daily %d for %s/%d,%d", resp.status_code, wfo, x, y)
    except Exception as exc:
        log.warning("NOAA daily fetch failed for %s/%d,%d: %s", wfo, x, y, exc)
    return None


async def fetch_noaa_hourly(wfo: str, x: int, y: int) -> Optional[dict]:
    """
    Fetch NOAA NWS hourly forecast (up to ~156 hours / 6.5 days ahead).
    Deriving daily high/low from hourly periods is more precise than the
    NWS daily summary, which only reports the representative period temp.
    Returns raw NWS JSON or None.
    """
    try:
        http = _get_http_client()
        url  = f"https://api.weather.gov/gridpoints/{wfo}/{x},{y}/forecast/hourly"
        resp = await http.get(url, headers={"User-Agent": "KalshiBot/1.0 (prediction-market-research)"})
        if resp.status_code == 200:
            return resp.json()
        log.warning("NOAA hourly %d for %s/%d,%d", resp.status_code, wfo, x, y)
    except Exception as exc:
        log.warning("NOAA hourly fetch failed for %s/%d,%d: %s", wfo, x, y, exc)
    return None


def _parse_nws_daily(forecast_data: dict, target: _date) -> Optional[dict]:
    """
    Extract high/low temperature and precip prob for a specific date
    from NWS daily forecast periods.
    """
    try:
        periods = forecast_data["properties"]["periods"]
        target_str = target.isoformat()
        high = low = precip_pct = None
        for p in periods:
            if target_str not in p.get("startTime", ""):
                continue
            temp = p.get("temperature")
            if temp is None:
                continue
            if p.get("isDaytime", True):
                high = float(temp)
                pp = p.get("probabilityOfPrecipitation", {})
                if pp and pp.get("value") is not None:
                    precip_pct = pp["value"]
            else:
                low = float(temp)
        if high is None and low is None:
            return None
        return {"high": high, "low": low, "precip_pct": precip_pct, "source": "noaa_nws"}
    except Exception:
        return None


def _parse_nws_hourly(hourly_data: dict, target: _date) -> Optional[dict]:
    """
    Derive daily high/low from NWS hourly forecast periods for the target date.
    Scanning all hours of the target day yields more accurate extremes than
    the NWS daily summary temperature (which is a single representative period).
    """
    try:
        periods = hourly_data["properties"]["periods"]
        target_str = target.isoformat()
        temps: list[float] = []
        precip_probs: list[float] = []
        for p in periods:
            if target_str not in p.get("startTime", ""):
                continue
            temp = p.get("temperature")
            if temp is not None:
                unit = p.get("temperatureUnit", "F")
                temps.append(float(temp) if unit != "C" else float(temp) * 9 / 5 + 32)
            pp = p.get("probabilityOfPrecipitation", {})
            if pp and pp.get("value") is not None:
                precip_probs.append(float(pp["value"]))
        if not temps:
            return None
        return {
            "high":       max(temps),
            "low":        min(temps),
            "precip_pct": max(precip_probs) if precip_probs else None,
            "source":     "noaa_hourly",
        }
    except Exception:
        return None


def _temp_prob_above(forecast_temp: float, threshold: float, days_ahead: int) -> float:
    """
    P(actual daily high/low > threshold) using calibrated forecast uncertainty.
    Sigma grows with forecast horizon based on NWS MAE verification studies:
      Day 0-1: 2.5°F,  Day 2: 3.5°F,  Day 3: 4.5°F,  Day 4+: 5.5°F
    """
    sigma = max(2.5, min(8.0, 2.5 + max(0, days_ahead - 1) * 0.85))
    z     = (threshold - forecast_temp) / sigma
    return 0.5 * (1.0 - _math.erf(z / _math.sqrt(2)))


def _precip_prob_above(forecast_sum: Optional[float], precip_pct: Optional[float],
                       threshold: float) -> Optional[float]:
    """
    P(precipitation > threshold inches).
    Uses a two-stage model: P(any rain) from precip_pct, then
    P(>threshold | rain) from an exponential distribution around forecast_sum.
    """
    if precip_pct is None:
        return None
    p_any = min(0.99, max(0.01, precip_pct / 100.0))
    if threshold <= 0.01:
        return p_any
    if forecast_sum and forecast_sum > 0.005:
        mean_if_rain = forecast_sum / p_any
        p_above = p_any * _math.exp(-threshold / mean_if_rain)
    else:
        p_above = p_any * _math.exp(-threshold * 6.0)
    return max(0.01, min(0.99, p_above))


async def scan_weather_markets(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Score weather markets using Open-Meteo (primary) and NOAA NWS (secondary)."""
    signals = []

    weather_markets = [
        m for m in markets
        if any(m.get("ticker", "").startswith(s) for s in WEATHER_SERIES)
    ]
    if not weather_markets:
        return signals

    log.debug("Weather scanner: %d markets to evaluate", len(weather_markets))

    # Fetch forecasts per series — one Open-Meteo call covers all dates per city
    # (we fetch per ticker date so we can target the exact date)
    for market in weather_markets:
        ticker = market.get("ticker", "")
        title  = market.get("title", "")
        price  = _market_mid(market)
        vol    = _market_volume(market)

        if price < 0.01 or price >= 0.99 or vol < 20:
            log.debug("Weather: skipping %s  price=%.4f  vol=%.0f", ticker, price, vol)
            continue

        # Identify which series this market belongs to
        series = next((s for s in WEATHER_SERIES if ticker.startswith(s)), None)
        if series is None:
            continue
        cfg = WEATHER_SERIES[series]

        # Parse the target date from the ticker
        target = _parse_ticker_date(ticker)
        if target is None:
            log.debug("Weather: cannot parse date from ticker %s", ticker)
            continue
        today = _date.today()
        if target < today:
            continue   # already resolved
        days_ahead = (target - today).days
        if days_ahead == 0:
            continue   # same-day markets trigger immediate pre_expiry (close within 24h)

        # RFC3339 close_time: end of target date in UTC (weather markets close at 23:59 local ≈ midnight UTC next day)
        _close_dt = datetime(target.year, target.month, target.day, 23, 59, 59, tzinfo=timezone.utc)
        signal_close_time = _close_dt.isoformat()

        # ── Fetch all four forecast sources in parallel ───────────────────────
        # GFS (Open-Meteo default), ECMWF (Open-Meteo), NWS daily, NWS hourly
        _results = await asyncio.gather(
            fetch_open_meteo(cfg["lat"], cfg["lon"], cfg["tz"], target),
            fetch_open_meteo_ecmwf(cfg["lat"], cfg["lon"], cfg["tz"], target),
            fetch_noaa_forecast(cfg["wfo"], cfg["x"], cfg["y"]),
            fetch_noaa_hourly(cfg["wfo"], cfg["x"], cfg["y"]),
            return_exceptions=True,
        )
        om_gfs, om_ecmwf, _nws_daily_raw, _nws_hourly_raw = (
            None if isinstance(r, Exception) else r for r in _results
        )
        nws       = _parse_nws_daily(_nws_daily_raw, target)  if _nws_daily_raw  else None
        nws_hrly  = _parse_nws_hourly(_nws_hourly_raw, target) if _nws_hourly_raw else None

        # Require at least one source
        if om_gfs is None and om_ecmwf is None and nws is None and nws_hrly is None:
            log.warning("Weather: no forecast data for %s (target=%s)", ticker, target)
            continue

        # Convenience: pick best Open-Meteo result (ECMWF preferred when available)
        om = om_ecmwf if om_ecmwf is not None else om_gfs

        fair_value: Optional[float] = None
        source_tag:  list[str]   = []
        raw_temps:   list[float] = []
        strike_type = "greater"  # default; overwritten for temp markets below

        if cfg["type"] in ("high_temp", "low_temp"):
            # Prefer floor_strike from market object; fall back to title regex
            floor_strike_raw = market.get("floor_strike")
            if floor_strike_raw is not None:
                threshold = float(floor_strike_raw)
            else:
                tm = re.search(r"(\d+)\s*[°º Ff]+", title)
                if not tm:
                    log.debug("Weather: no threshold in title %r", title[:60])
                    continue
                threshold = float(tm.group(1))

            # strike_type: "greater" → YES if temp > threshold; "less" → YES if temp < threshold
            strike_type = market.get("strike_type", "greater")

            # Gather temperature estimates from all available sources.
            # Weights: ECMWF > NWS hourly > GFS > NWS daily (accuracy ranking).
            temp_weighted: list[tuple[float, float]] = []  # (temp, weight)
            _temp_key = "high" if cfg["type"] == "high_temp" else "low"
            if om_gfs:
                t = om_gfs.get(_temp_key)
                if t is not None:
                    temp_weighted.append((t, 1.0))
                    source_tag.append("gfs")
            if om_ecmwf:
                t = om_ecmwf.get(_temp_key)
                if t is not None:
                    temp_weighted.append((t, 1.3))
                    source_tag.append("ecmwf")
            if nws_hrly:
                t = nws_hrly.get(_temp_key)
                if t is not None:
                    temp_weighted.append((t, 1.2))
                    source_tag.append("noaa_hourly")
            elif nws:
                # Only use NWS daily if hourly isn't available for this day
                t = nws.get(_temp_key)
                if t is not None:
                    temp_weighted.append((t, 0.9))
                    source_tag.append("noaa_nws")

            if not temp_weighted:
                continue

            raw_temps = [t for t, _ in temp_weighted]
            total_w   = sum(w for _, w in temp_weighted)
            mean_temp = sum(t * w for t, w in temp_weighted) / total_w
            spread    = max(raw_temps) - min(raw_temps) if len(raw_temps) > 1 else 0.0

            # Effective sigma: calibrated horizon + inter-model disagreement.
            # Models agreeing tightly (spread < 1.5°F) earns a 15% sigma reduction.
            base_sigma = max(2.0, min(5.5, 2.0 + max(0, days_ahead - 1) * 0.85))
            if spread < 1.5 and len(raw_temps) >= 2:
                base_sigma *= 0.85
            effective_sig = _math.sqrt(base_sigma ** 2 + (spread / 2) ** 2)
            z             = (threshold - mean_temp) / effective_sig
            p_above       = 0.5 * (1.0 - _math.erf(z / _math.sqrt(2)))
            fair_value    = p_above if strike_type != "less" else (1.0 - p_above)

        elif cfg["type"] == "precip":
            tm = re.search(r"(\d+\.?\d*)\s*inch", title, re.IGNORECASE)
            threshold = float(tm.group(1)) if tm else 0.10

            # Gather precip probability from all available sources and average
            precip_pcts: list[float] = []
            f_sum = None
            if om_gfs and om_gfs.get("precip_pct") is not None:
                precip_pcts.append(om_gfs["precip_pct"])
                source_tag.append("gfs")
                if om_gfs.get("precip") is not None:
                    f_sum = om_gfs["precip"]
            if om_ecmwf and om_ecmwf.get("precip_pct") is not None:
                precip_pcts.append(om_ecmwf["precip_pct"])
                source_tag.append("ecmwf")
                if f_sum is None and om_ecmwf.get("precip") is not None:
                    f_sum = om_ecmwf["precip"]
            if nws_hrly and nws_hrly.get("precip_pct") is not None:
                precip_pcts.append(nws_hrly["precip_pct"])
                source_tag.append("noaa_hourly")
            elif nws and nws.get("precip_pct") is not None:
                precip_pcts.append(nws["precip_pct"])
                source_tag.append("noaa_nws")

            if not precip_pcts:
                continue

            f_pct = sum(precip_pcts) / len(precip_pcts)
            fv = _precip_prob_above(f_sum, f_pct, threshold)
            if fv is None:
                continue
            fair_value = fv

        if fair_value is None:
            continue

        fair_value = max(0.02, min(0.98, fair_value))

        # For "less" (below-threshold) B-series markets, Kalshi stores the price
        # as the "above" YES price.  Invert to get the actual YES price for this market.
        if strike_type == "less":
            price = 1.0 - price

        diff = fair_value - price
        if abs(diff) < MIN_EDGE_GROSS:
            continue

        side     = "yes" if diff > 0 else "no"
        edge     = abs(diff)
        fee_edge = _fee_adjusted_edge(
            fair_value if side == "yes" else (1 - fair_value),
            price      if side == "yes" else (1 - price),
            "yes"
        )
        if fee_edge < MIN_EDGE_GROSS * 0.5:
            continue

        # Confidence tiers based on source count and inter-model spread.
        # 3+ sources strongly agreeing → near-FOMC tier confidence.
        n_src        = len(set(source_tag))
        model_spread = (max(raw_temps) - min(raw_temps)) if (
            cfg["type"] in ("high_temp", "low_temp") and len(raw_temps) > 1
        ) else 0.0
        tight_agree  = n_src >= 3 and model_spread < 2.0
        multi_source = n_src >= 2
        conf = (
            0.88 if (tight_agree and days_ahead <= 2) else
            0.84 if tight_agree else
            0.80 if (multi_source and days_ahead <= 1) else
            0.76 if multi_source else
            0.65
        )

        # Multiplier raised 60→90: weather markets are daily (high capital velocity),
        # comparable to FOMC directional at 80 but with extra credit for daily turnover.
        contracts = min(max_contracts, max(1, int(edge * 90)))
        signals.append(Signal(
            ticker            = ticker,
            title             = title,
            category          = "weather",
            side              = side,
            fair_value        = fair_value if side == "yes" else (1 - fair_value),
            market_price      = price,
            edge              = round(edge, 4),
            fee_adjusted_edge = round(fee_edge, 4),
            contracts         = contracts,
            confidence        = conf,
            model_source      = "+".join(sorted(set(source_tag))) or "weather",
            close_time        = signal_close_time,
        ))
        log.info(
            "Weather signal: %s  %s  fv=%.2f  market=%.2f  edge=%.2f  "
            "conf=%.2f  src=%s  n=%d  spread=%.1f°F  days=%d",
            ticker, side, fair_value, price, edge, conf,
            "+".join(sorted(set(source_tag))), len(set(source_tag)),
            (max(raw_temps) - min(raw_temps)) if len(raw_temps) > 1 else 0.0,
            days_ahead,
        )

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
    log.info("Weather scanner: %d signals generated", len(signals))
    return signals


# ── Economic Model (FRED) ─────────────────────────────────────────────────────

ECONOMIC_SERIES = {
    "CPIAUCSL_PC1": {"name": "CPI", "keywords": ["cpi", "inflation", "consumer price"]},
    "UNRATE":    {"name": "UNEMP", "keywords": ["unemployment", "jobless"]},
    "PAYEMS":    {"name": "JOBS",  "keywords": ["nonfarm", "payroll", "jobs added"]},
    # "GDP" disabled — fred_GDP_sigmoid consistently underperformed (-$13 / 30d)
    # "GDP":       {"name": "GDP",   "keywords": ["gdp", "gross domestic"]},
    "FEDFUNDS":  {"name": "RATE",  "keywords": ["fed funds", "interest rate"]},
}


async def fetch_fred_series(series_id: str, api_key: str, limit: int = 3) -> Optional[list]:
    """Fetch recent observations from FRED."""
    import datetime as _dt
    try:
        http = _get_http_client()
        url  = (  # FRED requires api_key as query param; no header auth supported — accepted risk
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={api_key}"
            f"&file_type=json&sort_order=desc&limit={limit}"
        )
        resp = await http.get(url)
        if resp.status_code == 200:
            obs = [
                o for o in resp.json().get("observations", [])
                if o.get("value") != "."
            ]
            # Staleness check: warn only if missed 2+ monthly releases (>65 days)
            # or 2+ quarterly releases (>200 days for GDP).  Monthly data has a
            # 30-45 day publication lag so 45-65 days is completely normal.
            if obs and obs[0].get("date"):
                obs_date   = _dt.date.fromisoformat(obs[0]["date"])
                days_stale = (_dt.date.today() - obs_date).days
                _stale_threshold = 200 if series_id == "A191RL1Q225SBEA" else 65
                if days_stale > _stale_threshold:
                    log.warning(
                        "FRED %s: most recent observation is %d days old (%s) — "
                        "may have missed a release",
                        series_id, days_stale, obs[0]["date"],
                    )
            return obs
    except Exception as exc:
        log.debug("FRED %s fetch failed: %s", series_id, exc)
    return None


# ── FOMC meeting calendar ────────────────────────────────────────────────────
# Second (decision) day of each two-day meeting through 2027.
_FOMC_MEETINGS: list[date] = [
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 4, 30),
    date(2025, 6, 18), date(2025, 7, 30), date(2025, 9, 17),
    date(2025, 10, 29), date(2025, 12, 17),
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 16),
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28),
    date(2027, 6, 16), date(2027, 7, 28), date(2027, 9, 15),
    date(2027, 10, 27), date(2027, 12, 15),
]


def _fomc_proximity_confidence(base: float = 0.72) -> float:
    """
    Adjust base FOMC confidence by proximity to the next meeting decision.

      0–2 days after  : -0.15  (market repricing post-decision)
      3+ days after   : 0      (settled)
      > 45 days until : -0.10  (far out, high uncertainty)
      15–45 days until: -0.05
      4–14 days until : 0      (standard window)
      0–3 days until  : +0.08  (near-final pricing, high certainty)
    """
    today   = date.today()
    future  = [m for m in _FOMC_MEETINGS if m >= today]
    past    = [m for m in _FOMC_MEETINGS if m < today]

    if not future:
        return base

    next_mtg   = future[0]
    days_until = (next_mtg - today).days

    # Post-meeting repricing window
    if past:
        days_since = (today - past[-1]).days
        if days_since <= 2:
            return max(0.40, base - 0.15)

    if days_until <= 3:
        return min(0.95, base + 0.08)
    elif days_until <= 14:
        return base
    elif days_until <= 45:
        return max(0.55, base - 0.05)
    else:
        return max(0.50, base - 0.10)


async def fetch_treasury_2y_yield(fred_api_key: str) -> Optional[float]:
    """
    Fetch the current 2-year Treasury constant maturity yield from FRED (DGS2).
    Returns yield as a percentage (e.g. 3.85 means 3.85%), or None on failure.

    The spread between 2Y yield and Fed Funds rate is the bond market's
    consensus on the rate path:
      2Y < Fed Funds  → market pricing cuts   → reinforces cut-side Kalshi signals
      2Y > Fed Funds  → market pricing hikes  → reinforces hike-side signals
      |spread| < 0.10 → neutral / uncertain
    """
    if not fred_api_key:
        return None
    try:
        http = _get_http_client()
        url  = (  # FRED requires api_key as query param; no header auth supported — accepted risk
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DGS2&api_key={fred_api_key}"
            "&file_type=json&sort_order=desc&limit=3"
        )
        resp = await http.get(url, timeout=8.0)
        if resp.status_code == 200:
            obs = [o for o in resp.json().get("observations", [])
                   if o.get("value", ".") != "."]
            if obs:
                yield_pct = float(obs[0]["value"])
                log.info("FRED DGS2: 2Y Treasury yield = %.3f%%", yield_pct)
                return yield_pct
    except Exception as exc:
        log.debug("FRED DGS2 fetch failed: %s", exc)
    return None


def _compute_surprise_z(vals: list[float]) -> Optional[float]:
    """
    Return a normalised Z-score surprise for the most recent observation.

    surprise = (most_recent - mean_last_6) / std_last_6

    Returns None when there are fewer than 3 observations (not enough history
    for a meaningful standard deviation).  ``vals`` is assumed to be sorted
    newest-first (i.e. ``vals[0]`` is the most recent observation).
    """
    if len(vals) < 3:
        return None
    window = vals[1:7]          # six observations *preceding* the most recent
    if len(window) < 2:
        return None
    mean6 = sum(window) / len(window)
    var6  = sum((v - mean6) ** 2 for v in window) / len(window)
    std6  = _math.sqrt(var6)
    if std6 == 0.0:
        return None
    return (vals[0] - mean6) / std6


def _compute_momentum(vals: list[float]) -> Optional[int]:
    """
    Return +1 (bullish), -1 (bearish), or 0 (mixed) based on whether the last
    three readings are all above or all below the 12-month mean.

    ``vals`` is sorted newest-first.  Returns None when there is insufficient
    data.
    """
    if len(vals) < 4:           # need ≥3 recent + enough for a mean
        return None
    recent   = vals[:3]
    mean12   = sum(vals[:12]) / min(len(vals), 12)
    if all(v > mean12 for v in recent):
        return +1
    if all(v < mean12 for v in recent):
        return -1
    return 0


async def scan_economic_markets(markets: list[dict], fred_api_key: str, max_contracts: int) -> list[Signal]:
    """Score economic threshold markets using FRED data."""
    if not fred_api_key:
        return []

    signals = []

    # Filter to economic markets.
    # Exclude KXGDP-prefixed tickers — those are GDP growth-rate threshold
    # markets handled by scan_gdp_markets(), which uses GDPNow correctly.
    # Including them here would apply the nominal GDP level series (FRED "GDP"
    # in billions) against a mis-parsed threshold, producing a spurious ~0.98
    # fair value for every KXGDP contract.
    econ_keywords = ["cpi", "inflation", "unemployment", "payroll", "jobs", "gdp", "fed"]
    econ_markets  = [
        m for m in markets
        if any(kw in m.get("title", "").lower() for kw in econ_keywords)
        and not m.get("ticker", "").startswith(("KXGDP", "KXFED"))
        and _market_volume(m) >= 50
    ]
    if not econ_markets:
        return signals

    # Fetch FRED data concurrently.
    # Use limit=14 so we have ≥12 months of history for surprise and momentum
    # calculations.  ADP (ADPWNUSNERSA) is fetched alongside but is not a
    # standalone Kalshi series — it is used only as a leading indicator for
    # PAYEMS (nonfarm payrolls) markets.
    sids    = list(ECONOMIC_SERIES.keys())
    fetched = await asyncio.gather(
        *[fetch_fred_series(sid, fred_api_key, limit=14) for sid in sids],
        fetch_fred_series("ADPWNUSNERSA", fred_api_key, limit=14),
        return_exceptions=True,
    )
    # Last element of fetched is ADP
    adp_obs: Optional[list] = None
    _adp_raw = fetched[-1]
    if not isinstance(_adp_raw, Exception) and _adp_raw:
        adp_obs = _adp_raw
    results = {
        sid: (None if isinstance(val, Exception) else val)
        for sid, val in zip(sids, fetched[:-1])
    }

    # Pre-compute ADP signal direction for use inside the PAYEMS match block.
    # adp_signal_direction: +1 if latest ADP > 12-month mean, -1 if below, 0 otherwise.
    adp_signal_direction = 0
    adp_val_logged: Optional[float] = None
    adp_mean_logged: Optional[float] = None
    if adp_obs:
        adp_vals: list[float] = []
        for _o in adp_obs:
            try:
                adp_vals.append(float(_o["value"]))
            except (ValueError, KeyError):
                pass
        if len(adp_vals) >= 2:
            adp_val_logged  = adp_vals[0]
            adp_mean_logged = sum(adp_vals[:12]) / min(len(adp_vals), 12)
            if adp_val_logged > adp_mean_logged:
                adp_signal_direction = +1
            elif adp_val_logged < adp_mean_logged:
                adp_signal_direction = -1
            log.info(
                "ADP leading indicator: %s vs mean %s → direction=%+d",
                f"{adp_val_logged:,.0f}",
                f"{adp_mean_logged:,.0f}",
                adp_signal_direction,
            )

    # Sigmoid steepness per series.  One "scale unit" corresponds to the
    # typical noise level for that indicator — values beyond ±2 scales from
    # the threshold are treated as high-confidence (outcome nearly certain).
    _ECON_SCALES: dict[str, float] = {
        "CPIAUCSL_PC1": 0.30,  # 0.30pp YoY CPI percent-change noise
        "UNRATE":   0.20,    # 20 bp of unemployment
        "PAYEMS":   50.0,    # 50k payroll jobs (series in thousands)
    }
    _DEFAULT_SCALE = 0.25

    for market in econ_markets:
        title = market.get("title", "").lower()
        price = _market_mid(market)
        if price <= 0.01 or price >= 0.99:
            continue

        fair_value      = None
        confidence      = 0.55
        matched_sid     = "fred"
        _surprise_z     = None   # set inside loop on successful match
        _momentum_boost = 0.0   # set inside loop on successful match

        for sid, config in ECONOMIC_SERIES.items():
            if not any(kw in title for kw in config["keywords"]):
                continue
            obs = results.get(sid)
            if not obs:
                continue

            # ── Multi-point weighted trend (exponential decay, up to 6 pts) ──
            # Collect up to 14 obs (we now fetch 14); the trend model still
            # uses only the first 6, but surprise/momentum use the full window.
            vals: list[float] = []
            for o in obs[:14]:
                try:
                    vals.append(float(o["value"]))
                except (ValueError, KeyError):
                    pass
            if not vals:
                continue

            latest_val = vals[0]

            if len(vals) >= 3:
                # Weighted least-squares linear trend (recent observations
                # weighted 2× more than older ones via exponential decay w=0.5^i)
                wts  = [0.5 ** i for i in range(len(vals))]
                sw   = sum(wts)
                mi   = sum(i * w for i, w in enumerate(wts)) / sw
                mv   = sum(v * w for v, w in zip(vals, wts)) / sw
                num  = sum(w * (i - mi) * (v - mv)
                           for i, (v, w) in enumerate(zip(vals, wts)))
                den  = sum(w * (i - mi) ** 2 for i, w in enumerate(wts))
                slope = num / den if den > 0 else 0.0
                projected = latest_val + slope * 0.5
            elif len(vals) == 2:
                projected = latest_val + (vals[0] - vals[1]) * 0.5
            else:
                projected = latest_val

            # Extract threshold from title (handles "above 3.5%" or "below 200k")
            thresh_match = re.search(
                r"(?:above|below|over|under|exceed|than|least|most)\s+(\d+\.?\d*)\s*[%k]?",
                market.get("title", ""), re.IGNORECASE,
            )
            if not thresh_match:
                continue
            threshold = float(thresh_match.group(1))

            # PAYEMS: FRED values are in thousands of persons; titles typically
            # quote whole numbers like "above 200" meaning 200k jobs.
            if sid == "PAYEMS" and threshold < 2_000:
                threshold *= 1_000   # convert to same units as FRED series

            # ── Sigmoid fair value ────────────────────────────────────────────
            # P(projected > threshold) — smooth transition instead of binary 0.75/0.25
            scale    = _ECON_SCALES.get(sid, _DEFAULT_SCALE)
            x        = (projected - threshold) / scale
            # Clip exponent to prevent overflow
            fair_yes = 1.0 / (1.0 + _math.exp(-max(-20.0, min(20.0, x))))

            # ── Distance-scaled confidence ────────────────────────────────────
            # Far from threshold → outcome nearly certain → high confidence
            # Near threshold     → coin-flip → low confidence
            dist_sigma = abs(projected - threshold) / scale
            confidence = round(min(0.85, 0.50 + 0.15 * min(dist_sigma, 2.0)), 3)

            # ── Economic surprise (Z-score) ───────────────────────────────────
            # Measures how far the latest reading is from its trailing 6-month
            # mean, normalised by the trailing 6-month standard deviation.
            # A large surprise (|z| > 1.5) means the series is running well
            # above or below trend — boost confidence accordingly.
            surprise_z  = _compute_surprise_z(vals)
            conf_boost  = 0.0
            series_name = config["name"]
            if surprise_z is not None:
                abs_z = abs(surprise_z)
                if abs_z > 2.5:
                    conf_boost = 0.10
                    log.info(
                        "%s surprise_z=%+.2f (>2.5σ) → conf boost +0.10",
                        series_name, surprise_z,
                    )
                elif abs_z > 1.5:
                    conf_boost = 0.05
                    log.info(
                        "%s surprise_z=%+.2f (>1.5σ) → conf boost +0.05",
                        series_name, surprise_z,
                    )
            confidence = round(min(0.92, confidence + conf_boost), 3)

            # ── Economic momentum signal ──────────────────────────────────────
            # If the last 3 readings are all above (or all below) the 12-month
            # mean, that persistent trend boosts the directional edge by 0.03.
            momentum       = _compute_momentum(vals)
            momentum_boost = 0.0
            if momentum == +1:
                log.info("%s: bullish momentum (last 3 readings all above 12m mean) → +0.03 edge", series_name)
                momentum_boost = 0.03
            elif momentum == -1:
                log.info("%s: bearish momentum (last 3 readings all below 12m mean) → +0.03 edge", series_name)
                momentum_boost = 0.03

            # ── ADP leading indicator (PAYEMS markets only) ───────────────────
            # ADP private payrolls are released ~2 days before NFP.  When ADP
            # confirms the signal direction, it lifts confidence; disagreement
            # reduces it.
            adp_confidence_mult = 1.0
            if sid == "PAYEMS" and adp_signal_direction != 0 and adp_val_logged is not None:
                # Determine which direction the model is pointing (yes → above threshold)
                model_direction = +1 if fair_yes >= 0.5 else -1
                if adp_signal_direction == model_direction:
                    adp_confidence_mult = 1.10
                    log.info(
                        "ADP leading indicator: %s vs mean %s → direction=%+d  "
                        "(agrees with model → confidence ×1.10)",
                        f"{adp_val_logged:,.0f}",
                        f"{adp_mean_logged:,.0f}",
                        adp_signal_direction,
                    )
                else:
                    adp_confidence_mult = 0.85
                    log.info(
                        "ADP leading indicator: %s vs mean %s → direction=%+d  "
                        "(disagrees with model → confidence ×0.85)",
                        f"{adp_val_logged:,.0f}",
                        f"{adp_mean_logged:,.0f}",
                        adp_signal_direction,
                    )
            confidence = round(min(0.92, confidence * adp_confidence_mult), 3)

            fair_value     = fair_yes
            matched_sid    = sid
            _surprise_z    = surprise_z        # carry out of loop for model_source tag
            _momentum_boost = momentum_boost   # carry out of loop for edge adjustment
            break   # matched first relevant series

        if fair_value is None:
            continue

        fair_value = max(0.02, min(0.98, fair_value))
        diff = fair_value - price
        if abs(diff) < MIN_EDGE_GROSS:
            continue

        side     = "yes" if diff > 0 else "no"
        edge     = round(abs(diff) + _momentum_boost, 4)
        fee_edge = _fee_adjusted_edge(
            fair_value if side == "yes" else (1 - fair_value),
            price if side == "yes" else (1 - price),
            "yes"
        )
        if fee_edge < MIN_EDGE_GROSS * 0.5:
            continue

        # Build a model_source tag that surfaces key signal components.
        _z_tag = f"_z{_surprise_z:+.1f}" if _surprise_z is not None else ""
        _m_tag = f"_mom{'+' if _momentum_boost > 0 else '0'}" if _momentum_boost else ""

        contracts = min(max_contracts, max(1, int(edge * 50)))
        signals.append(Signal(
            ticker            = market["ticker"],
            title             = market.get("title", ""),
            category          = "economic",
            side              = side,
            fair_value        = fair_value if side == "yes" else (1 - fair_value),
            market_price      = price,
            edge              = edge,
            fee_adjusted_edge = round(fee_edge, 4),
            contracts         = contracts,
            confidence        = confidence,
            model_source      = f"fred_{matched_sid}_sigmoid{_z_tag}{_m_tag}",
        ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
    return signals


# ── GDP Scanner (KXGDP) ───────────────────────────────────────────────────────

async def scan_gdp_markets(
    markets:      list[dict],
    fred_api_key: str = "",
    max_contracts: int = 5,
    macro_regime: Optional[dict] = None,
) -> list[Signal]:
    """
    Score KXGDP markets using the Atlanta Fed GDPNow real-time estimate (FRED: GDPNOW).

    KXGDP tickers: KXGDP-26APR30-T4.5 = "Will Q1 2026 real GDP growth > 4.5%?"

    GDPNow updates every few days as new economic data arrives and has ~0.9pp RMSE
    near publication — significantly more accurate than a backward-looking trend model.
    """
    signals = []
    gdp_markets = [m for m in markets if m.get("ticker","").startswith("KXGDP-")]
    if not gdp_markets or not fred_api_key:
        return signals

    # Fetch Atlanta Fed GDPNow from FRED (series GDPNOW — updates intra-quarter).
    # Fetch several quarters so we can match each market to the correct quarter.
    # FRED observation `date` = quarter start (e.g. 2026-01-01 = Q1 2026).
    # `realtime_start` = actual publication date (what we log for clarity).
    gdp_by_quarter: dict = {}   # "YYYY-QN" → float estimate
    gdp_pub_date:   dict = {}   # "YYYY-QN" → publication date string
    gdp_source = "gdpnow"
    try:
        http = _get_http_client()
        url  = (  # FRED requires api_key as query param; no header auth supported — accepted risk
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=GDPNOW&api_key={fred_api_key}"
            "&file_type=json&sort_order=desc&limit=4"
        )
        resp = await http.get(url, timeout=8.0)
        if resp.status_code == 200:
            obs = [o for o in resp.json().get("observations", [])
                   if o.get("value", ".") != "."]
            for o in obs:
                period = o["date"]   # e.g. "2026-01-01"
                try:
                    yr, mo, _ = period.split("-")
                    mo_i = int(mo)
                    q    = (mo_i - 1) // 3 + 1
                    key  = f"{yr}-Q{q}"
                    gdp_by_quarter[key] = float(o["value"])
                    gdp_pub_date[key]   = o.get("realtime_start", period)
                except (ValueError, KeyError):
                    pass
            if gdp_by_quarter:
                for k, v in gdp_by_quarter.items():
                    log.info("GDP model: GDPNow=%s  %.2f%%  (published %s)",
                             k, v, gdp_pub_date.get(k, "?"))
    except Exception as exc:
        log.debug("FRED GDPNow fetch failed: %s — trying fallback", exc)

    # Fallback: backward-looking weighted average of last 4 reported quarters
    _fallback_estimate: Optional[float] = None
    if not gdp_by_quarter:
        try:
            url2 = (  # FRED requires api_key as query param; no header auth supported — accepted risk
                "https://api.stlouisfed.org/fred/series/observations"
                f"?series_id=A191RL1Q225SBEA&api_key={fred_api_key}"
                "&file_type=json&sort_order=desc&limit=4"
            )
            resp2 = await http.get(url2, timeout=8.0)
            if resp2.status_code == 200:
                obs2 = [float(o["value"]) for o in resp2.json().get("observations", [])
                        if o.get("value", ".") != "."]
                if obs2:
                    weights            = [4, 3, 2, 1][:len(obs2)]
                    weighted           = sum(v * w for v, w in zip(obs2, weights)) / sum(weights)
                    _fallback_estimate = weighted * 0.6 + 2.5 * 0.4
                    gdp_source         = "fred_trend"
                    log.info("GDP model (fallback): last_4q=%s  estimate=%.2f%%", obs2, _fallback_estimate)
        except Exception as exc2:
            log.debug("FRED GDP fallback failed: %s", exc2)

    if not gdp_by_quarter and _fallback_estimate is None:
        log.debug("No GDP estimate available — skipping KXGDP scan")
        return signals

    # Uncertainty: GDPNow ~0.9pp RMSE; fallback trend model ~1.5pp
    gdp_uncertainty = 0.9 if gdp_source == "gdpnow" else 1.5

    # Map expiry month → GDP quarter (BEA advance release calendar):
    #   APR expiry → Q1 GDP (Jan-Mar)  → FRED key YYYY-Q1
    #   JUL expiry → Q2 GDP (Apr-Jun)  → FRED key YYYY-Q2
    #   OCT expiry → Q3 GDP (Jul-Sep)  → FRED key YYYY-Q3
    #   JAN expiry → Q4 GDP of prior yr → FRED key (YYYY-1)-Q4
    _MONTH_ABBR = {
        "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
        "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12,
    }
    _EXPIRY_TO_GDPQ = {4: 1, 7: 2, 10: 3, 1: 4}  # expiry month → GDP quarter

    for market in gdp_markets:
        ticker = market.get("ticker","")
        # Parse threshold from ticker: KXGDP-26APR30-T4.5 → 4.5
        threshold_match = re.search(r"-T(\d+\.?\d*)$", ticker)
        if not threshold_match:
            continue
        threshold = float(threshold_match.group(1))

        # Determine which quarter's GDP this market resolves against
        gdp_estimate: Optional[float] = None
        expiry_match = re.search(r"KXGDP-(\d{2})([A-Z]{3})\d{2}-", ticker)
        if expiry_match and gdp_by_quarter:
            yr_2d  = int(expiry_match.group(1))
            mo_str = expiry_match.group(2)
            exp_yr = 2000 + yr_2d
            exp_mo = _MONTH_ABBR.get(mo_str, 0)
            gdp_q  = _EXPIRY_TO_GDPQ.get(exp_mo)
            if gdp_q:
                gdp_yr   = exp_yr - 1 if gdp_q == 4 else exp_yr
                fred_key = f"{gdp_yr}-Q{gdp_q}"
                gdp_estimate = gdp_by_quarter.get(fred_key)
                if gdp_estimate is None:
                    log.debug("GDP: no %s estimate for %s — skipping", fred_key, ticker)
                    continue
        elif _fallback_estimate is not None:
            gdp_estimate = _fallback_estimate
        else:
            continue

        price = _market_mid(market)
        if price <= 0.01 or price >= 0.99:
            continue

        # P(GDP > threshold) using normal distribution around nowcast
        z          = (gdp_estimate - threshold) / gdp_uncertainty
        p_above    = 0.5 * (1.0 + _math.erf(z / _math.sqrt(2)))
        fair_value = p_above   # YES = GDP growth > threshold

        if fair_value > price:
            side = "yes"
            edge = fair_value - price
        else:
            side = "no"
            edge = price - fair_value

        fee_edge = _fee_adjusted_edge(fair_value, price, side)
        if fee_edge < 0.04:
            continue

        # ── GDP directional consistency guard ────────────────────────────────
        # Only bet in the direction our GDPNow model predicts.
        #
        # YES guard: suppress YES bets when GDPNow is well below threshold.
        #   GDPNow RMSE ≈ 0.9pp; suppress when nowcast < threshold - 1.0pp
        #   (comfortable miss predicted — YES very unlikely).
        #
        # NO guard: suppress NO bets when GDPNow is at or above threshold.
        #   If GDPNow ≥ threshold - 0.5pp, GDP is likely to EXCEED the threshold
        #   (YES resolves), making a NO bet directionally wrong vs our own model.
        #   0.5pp buffer covers GDPNow noise near the boundary.
        if side == "yes" and gdp_estimate < (threshold - 1.0):
            log.debug(
                "GDP YES suppressed: %s  gdpnow=%.2f%%  threshold=%.2f%%  "
                "(gdpnow < threshold - 1.0pp — YES very unlikely)",
                ticker, gdp_estimate, threshold,
            )
            continue
        if side == "no" and gdp_estimate >= (threshold - 0.5):
            log.debug(
                "GDP NO suppressed: %s  gdpnow=%.2f%%  threshold=%.2f%%  "
                "(gdpnow near or above threshold — NO bet contradicts model direction)",
                ticker, gdp_estimate, threshold,
            )
            continue

        confidence = min(0.85, 0.50 + abs(fair_value - 0.5))

        # ── Task 6: GDP regime-aware confidence adjustment ────────────────────
        if macro_regime and side == "yes":
            _t10y2y = macro_regime.get("t10y2y")
            _unrate = macro_regime.get("unrate", 4.0)
            if _t10y2y is not None and _t10y2y < -0.25:
                confidence -= 0.05   # inverted yield curve → recession risk → GDP likely disappoints
                log.debug("GDP regime adj: inverted curve (t10y2y=%.3f) → conf -0.05", _t10y2y)
            if _unrate is not None and _unrate > 5.0:
                confidence -= 0.03   # rising unemployment → growth concern
                log.debug("GDP regime adj: high unrate (%.2f%%) → conf -0.03", _unrate)
            confidence = max(0.40, confidence)

        signals.append(Signal(
            ticker            = ticker,
            title             = market.get("title","") or f"GDP growth > {threshold}%",
            category          = "gdp",
            side              = side,
            fair_value        = fair_value,
            market_price      = price,
            edge              = edge,
            fee_adjusted_edge = fee_edge,
            contracts         = max_contracts,
            confidence        = confidence,
            model_source      = f"{gdp_source}_{gdp_estimate:.1f}pct",
            spread_cents      = int(abs(
                float(market.get("yes_ask_dollars") or price + 0.02) -
                float(market.get("yes_bid_dollars") or price - 0.02)
            ) * 100),
        ))

    if signals:
        _latest_est = (
            next(iter(gdp_by_quarter.values())) if gdp_by_quarter
            else _fallback_estimate
        )
        log.info("GDP scan: %d signals (nowcast=%.2f%%)", len(signals), _latest_est or 0.0)
    return signals


# ── Sports Model (ESPN) ───────────────────────────────────────────────────────

SPORT_SERIES = {
    "KXNBA":  {"sport": "basketball", "league": "nba"},
    "KXNFL":  {"sport": "football",   "league": "nfl"},
    "KXMLB":  {"sport": "baseball",   "league": "mlb"},
    "KXNHL":  {"sport": "hockey",     "league": "nhl"},
    "KXSOCCER": {"sport": "soccer",   "league": "mls"},
}


async def fetch_espn_odds(sport: str, league: str) -> Optional[list]:
    """Fetch ESPN scoreboard with current odds/lines."""
    try:
        http = _get_http_client()
        url  = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        resp = await http.get(url)
        if resp.status_code == 200:
            return resp.json().get("events", [])
    except Exception as exc:
        log.debug("ESPN %s/%s fetch failed: %s", sport, league, exc)
    return None


def _ml_to_prob(ml: float) -> float:
    """Convert American moneyline to implied probability (before vig removal)."""
    if ml < 0:
        return abs(ml) / (100.0 + abs(ml))
    return 100.0 / (100.0 + abs(ml))


def _parse_espn_win_prob(event: dict) -> Optional[dict]:
    """
    Extract win probability from ESPN event data.

    Priority order (ESPN public API availability):
      1. competitions[0].predictor — in-game win probability (most accurate when live)
      2. competitions[0].odds[0].{home|away}TeamOdds.winPercentage — pre-game model
      3. competitions[0].odds[0].{home|away}TeamOdds.moneyLine — convert to probability
    """
    try:
        competitions = event.get("competitions", [])
        if not competitions:
            return None
        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        # Build team name lookup: homeAway → shortDisplayName
        home_team = next((c.get("team", {}).get("shortDisplayName", "Home")
                         for c in competitors if c.get("homeAway") == "home"), "Home")
        away_team = next((c.get("team", {}).get("shortDisplayName", "Away")
                         for c in competitors if c.get("homeAway") == "away"), "Away")

        # ── Source 1: predictor (in-game, most reliable) ──────────────────────
        predictor = comp.get("predictor", {})
        home_wp = predictor.get("homeWinPercentage") or predictor.get("homeTeam", {}).get("gameProjection")
        if home_wp is not None:
            home_p = float(home_wp) / 100.0 if float(home_wp) > 1.0 else float(home_wp)
            if 0.01 < home_p < 0.99:
                return {home_team: home_p, away_team: 1.0 - home_p}

        # ── Source 2: odds.winPercentage ──────────────────────────────────────
        odds = comp.get("odds", [])
        if odds:
            home_wp_pct = odds[0].get("homeTeamOdds", {}).get("winPercentage")
            if home_wp_pct is not None:
                home_p = float(home_wp_pct) / 100.0 if float(home_wp_pct) > 1.0 else float(home_wp_pct)
                if 0.01 < home_p < 0.99:
                    return {home_team: home_p, away_team: 1.0 - home_p}

            # ── Source 3: moneyline → probability ─────────────────────────────
            home_ml = odds[0].get("homeTeamOdds", {}).get("moneyLine")
            away_ml = odds[0].get("awayTeamOdds", {}).get("moneyLine")
            if home_ml and away_ml:
                raw_home = _ml_to_prob(float(home_ml))
                raw_away = _ml_to_prob(float(away_ml))
                total    = raw_home + raw_away
                if total > 0.01:
                    home_p = raw_home / total   # remove vig
                    if 0.01 < home_p < 0.99:
                        return {home_team: home_p, away_team: 1.0 - home_p}

        return None
    except Exception:
        return None


async def scan_sports_markets(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Score sports markets using ESPN odds."""
    signals = []

    # Filter to sports markets
    sports_markets = [
        m for m in markets
        if any(m.get("ticker", "").startswith(s) for s in SPORT_SERIES)
        and _market_volume(m) >= 100
    ]
    if not sports_markets:
        return signals

    # Fetch ESPN data for each relevant league
    espn_data: dict[str, Optional[list]] = {}
    for series, config in SPORT_SERIES.items():
        relevant = [m for m in sports_markets if m.get("ticker", "").startswith(series)]
        if not relevant:
            continue
        espn_data[series] = await fetch_espn_odds(config["sport"], config["league"])

    for series, events in espn_data.items():
        if not events:
            continue
        relevant = [m for m in sports_markets if m.get("ticker", "").startswith(series)]

        for market in relevant:
            price = _market_mid(market)
            vol   = _market_volume(market)
            if price <= 0.02 or price >= 0.98 or vol < 100:
                continue

            title = market.get("title", "")

            # Match market title to ESPN event
            best_match = None
            best_prob  = None
            for event in events:
                win_probs = _parse_espn_win_prob(event)
                if not win_probs:
                    continue
                # Check if any team name appears in market title
                for team, prob in win_probs.items():
                    if team.lower() in title.lower():
                        best_match = team
                        best_prob  = prob
                        break
                if best_match:
                    break

            if best_prob is None:
                continue

            fair_value = max(0.02, min(0.98, best_prob))
            diff = fair_value - price
            if abs(diff) < MIN_EDGE_GROSS:
                continue

            side     = "yes" if diff > 0 else "no"
            edge     = abs(diff)
            fee_edge = _fee_adjusted_edge(
                fair_value if side == "yes" else (1 - fair_value),
                price if side == "yes" else (1 - price),
                "yes"
            )
            if fee_edge < MIN_EDGE_GROSS * 0.5:
                continue

            contracts = min(max_contracts, max(1, int(edge * 60)))
            signals.append(Signal(
                ticker            = market["ticker"],
                title             = title,
                category          = "sports",
                side              = side,
                fair_value        = fair_value,   # always P(YES wins)
                market_price      = price,
                edge              = round(edge, 4),
                fee_adjusted_edge = round(fee_edge, 4),
                contracts         = contracts,
                confidence        = 0.65,
                model_source      = f"espn_{best_match}",
            ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
    return signals


# ── Improvement 5: Cross-Meeting Bayes Coherence Arbitrage ───────────────────

# Meeting order for rate path coherence checks (chronological)
_FOMC_MEETING_ORDER = ["JUN", "JUL", "SEP", "OCT", "DEC", "JAN"]


def scan_cross_meeting_coherence(
    markets_data: list[dict],
    prices_data:  dict,
) -> list[Signal]:
    """
    Detect Bayesian coherence violations across KXFED meeting dates.

    A forward rate path must be monotonically consistent:
      P(rate > X at later meeting) must be >= P(rate > X at earlier meeting) * 0.95
      (rates don't jump back up easily once cut)

    If P(rate > X at later meeting) > P(rate > X at earlier meeting) + 0.08,
    this is a coherence violation — signal YES on the earlier meeting (underpriced)
    OR NO on the later meeting (overpriced).

    Returns list[Signal].
    """

    min_edge_cents = 8   # minimum gap in cents to generate a signal
    min_fv_gap     = 0.06  # fair_value - market_price must exceed this

    # Filter to KXFED markets only
    fomc_markets = [m for m in markets_data if m.get("ticker", "").startswith("KXFED-")]
    if not fomc_markets:
        return []

    # Group by strike value (e.g. T3.25 → 3.25)
    strikes: dict[float, list[tuple]] = {}   # strike → [(meeting_label, price, market), ...]
    for m in fomc_markets:
        ticker = m.get("ticker", "")
        strike = _extract_strike(ticker)
        if strike is None:
            continue

        # Determine which meeting this ticker belongs to (e.g. "JUN", "JUL", etc.)
        # Ticker format: KXFED-{YY}{MON}{DD}-T{strike}  e.g. KXFED-26JUN18-T3.25
        parts = ticker.split("-")
        if len(parts) < 2:
            continue
        date_str = parts[1]   # e.g. "26JUN18"
        if len(date_str) < 5:
            continue
        mon_str = date_str[2:5].upper()   # e.g. "JUN"
        if mon_str not in _FOMC_MEETING_ORDER:
            continue

        # Get price from prices_data or fall back to market mid
        price = prices_data.get(ticker)
        if price is None:
            price = _market_mid(m)
        else:
            try:
                price = float(price) / 100.0 if float(price) > 1.0 else float(price)
            except (TypeError, ValueError):
                price = _market_mid(m)

        if price <= 0.01 or price >= 0.99:
            continue

        strikes.setdefault(strike, []).append((mon_str, price, m))

    signals = []

    for strike, entries in strikes.items():
        # Sort entries by meeting order (chronological)
        ordered = sorted(
            entries,
            key=lambda e: _FOMC_MEETING_ORDER.index(e[0]) if e[0] in _FOMC_MEETING_ORDER else 99,
        )
        if len(ordered) < 2:
            continue

        # Check each consecutive pair for coherence violation
        for i in range(len(ordered) - 1):
            early_mon, early_price, early_mkt = ordered[i]
            late_mon,  late_price,  late_mkt  = ordered[i + 1]

            # Coherence check: P(rate > X at later) must not exceed P(rate > X at earlier) + 0.08
            gap = late_price - early_price
            if gap <= min_edge_cents / 100.0:
                continue   # no violation

            # Violation detected: early meeting is underpriced OR late meeting is overpriced
            # Signal YES on early meeting (fair_value = late_price, which is higher)
            early_fair  = late_price         # the later market is implying this price
            early_edge  = early_fair - early_price
            if early_edge >= min_fv_gap:
                fee_e = _fee_adjusted_edge(early_fair, early_price, "yes")
                if fee_e > 0:
                    try:
                        _et = early_mkt.get("ticker", "")
                        sig = Signal(
                            ticker            = _et,
                            title             = early_mkt.get("title", _et),
                            category          = "fomc",
                            side              = "yes",
                            market_price      = round(early_price, 4),
                            fair_value        = round(early_fair,  4),
                            edge              = round(early_edge,  4),
                            fee_adjusted_edge = round(fee_e,       4),
                            contracts         = 1,
                            confidence        = 0.70,
                            model_source      = f"cross_meeting_coherence_{early_mon}_vs_{late_mon}",
                            meeting           = early_mkt.get("event_ticker", ""),
                        )
                        signals.append(sig)
                        log.info(
                            "Cross-meeting coherence: YES %s  early=%s@%.3f  late=%s@%.3f  "
                            "gap=%.3f  edge=%.3f",
                            _et, early_mon, early_price, late_mon, late_price, gap, early_edge,
                        )
                    except Exception as exc:
                        log.debug("cross_meeting_coherence signal build failed: %s", exc)

            # Also signal NO on the later meeting (overpriced given earlier)
            late_fair  = early_price        # the earlier market is implying this as upper bound
            late_edge  = late_price - late_fair
            if late_edge >= min_fv_gap:
                # For NO side: fair_value is P(YES) = late_fair; market_price is the YES mid
                fee_e = _fee_adjusted_edge(late_fair, late_price, "no")
                if fee_e > 0:
                    try:
                        _lt = late_mkt.get("ticker", "")
                        sig = Signal(
                            ticker            = _lt,
                            title             = late_mkt.get("title", _lt),
                            category          = "fomc",
                            side              = "no",
                            market_price      = round(late_price, 4),
                            fair_value        = round(late_fair,  4),
                            edge              = round(late_edge,  4),
                            fee_adjusted_edge = round(fee_e,      4),
                            contracts         = 1,
                            confidence        = 0.70,
                            model_source      = f"cross_meeting_coherence_{early_mon}_vs_{late_mon}",
                            meeting           = late_mkt.get("event_ticker", ""),
                        )
                        signals.append(sig)
                        log.info(
                            "Cross-meeting coherence: NO  %s  early=%s@%.3f  late=%s@%.3f  "
                            "gap=%.3f  edge=%.3f",
                            _lt, early_mon, early_price, late_mon, late_price, gap, late_edge,
                        )
                    except Exception as exc:
                        log.debug("cross_meeting_coherence signal build failed: %s", exc)

    return signals


# ── Improvement 7: Election Market Ensemble ───────────────────────────────────

# Election ticker prefixes Kalshi uses
_ELECTION_PREFIXES = ("KXPRES", "KXSEN", "KXHOUSE", "KXGOV")


def scan_election_markets(
    kalshi_markets:    list[dict],
    polymarket_prices: dict,
    predictit_prices:  dict,
) -> "list":
    """
    Compute ensemble fair values for Kalshi election markets using cross-platform prices.

    Sources and weights:
      Kalshi: 0.4 (treated as market price, NOT included in fair value computation)
      Polymarket: 0.4
      PredictIt: 0.2 (if available; weight redistributed if absent)

    Signals when abs(ensemble_fair_value - kalshi_price) > 0.07 with confidence >= 0.65.

    Returns list[SignalMessage].  Returns empty list gracefully if no election markets.
    """
    from ep_schema import SignalMessage  # noqa: PLC0415

    min_gap        = 0.07
    min_confidence = 0.65

    election_markets = [
        m for m in kalshi_markets
        if any(m.get("ticker", "").startswith(p) for p in _ELECTION_PREFIXES)
    ]
    if not election_markets:
        return []

    signals = []

    for market in election_markets:
        ticker = market.get("ticker", "")
        title  = market.get("title", "").lower()

        # Kalshi YES price (mid)
        kalshi_price = _market_mid(market)
        if kalshi_price <= 0.01 or kalshi_price >= 0.99:
            continue

        # Gather prices from other sources by matching candidate/party name in title
        poly_price: Optional[float] = None
        pi_price:   Optional[float] = None

        # Polymarket: prices keyed by market slug or candidate name (lowercase partial match)
        for key, val in polymarket_prices.items():
            key_lower = key.lower()
            # Match if any word from the key appears in the Kalshi title
            if any(word in title for word in key_lower.split() if len(word) > 3):
                try:
                    poly_price = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        # PredictIt: prices keyed similarly
        for key, val in predictit_prices.items():
            key_lower = key.lower()
            if any(word in title for word in key_lower.split() if len(word) > 3):
                try:
                    pi_price = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        # Compute ensemble fair value
        # We need at least Polymarket to have signal
        if poly_price is None:
            continue

        if pi_price is not None:
            # All three sources: Kalshi 0.4, Poly 0.4, PI 0.2
            # Kalshi is the "market price" we're evaluating — include it in ensemble
            fair_value = 0.4 * kalshi_price + 0.4 * poly_price + 0.2 * pi_price
        else:
            # No PredictIt: redistribute its 0.2 weight → Kalshi 0.5, Poly 0.5
            fair_value = 0.5 * kalshi_price + 0.5 * poly_price

        fair_value = max(0.01, min(0.99, fair_value))
        gap = fair_value - kalshi_price

        if abs(gap) <= min_gap:
            continue

        side = "yes" if gap > 0 else "no"
        edge = abs(gap)
        fee_e = _fee_adjusted_edge(
            fair_value if side == "yes" else (1.0 - fair_value),
            kalshi_price if side == "yes" else (1.0 - kalshi_price),
            "yes",
        )
        if fee_e <= 0:
            continue

        # Confidence: higher when sources agree; base 0.65 boosted by convergence
        n_sources   = 2 + (1 if pi_price is not None else 0)
        source_vals = [kalshi_price, poly_price] + ([pi_price] if pi_price else [])
        spread      = max(source_vals) - min(source_vals)
        # More agreement → higher confidence
        confidence  = max(min_confidence, min(0.85, 0.65 + (0.10 * n_sources * (1 - spread * 2))))

        try:
            sig = SignalMessage(
                asset_class       = "kalshi",
                strategy          = "election_ensemble",
                category          = "election",
                ticker            = ticker,
                exchange          = "kalshi",
                side              = side,
                market_price      = round(kalshi_price, 4),
                fair_value        = round(fair_value,   4),
                edge              = round(edge,         4),
                fee_adjusted_edge = round(fee_e,        4),
                confidence        = round(confidence,   3),
                suggested_size    = 1,
                kelly_fraction    = 0.02,
                model_source      = (
                    f"election_ensemble_poly={poly_price:.3f}"
                    + (f"_pi={pi_price:.3f}" if pi_price else "")
                ),
            )
            signals.append(sig)
            log.info(
                "Election ensemble: %s %s  kalshi=%.3f  fair=%.3f  edge=%.3f  conf=%.2f",
                side.upper(), ticker, kalshi_price, fair_value, edge, confidence,
            )
        except Exception as exc:
            log.debug("election_ensemble signal build failed for %s: %s", ticker, exc)

    return signals


# ── Improvement 8: BLS Release Pre-Positioning ────────────────────────────────

import datetime as _dt_module

# BLS release schedule for 2026 (UTC times).
# Tuple: (month, day, hour, minute)
_BLS_RELEASE_TIMES_UTC = [
    # CPI releases (approximately 15th of each month at 13:30 UTC / 08:30 ET)
    (4, 30, 12, 30),   # CPI April
    (5, 13, 12, 30),   # CPI May
    (6, 11, 12, 30),   # CPI June
    # NFP: first Friday each month at 12:30 UTC
]

# Pre-release window: signal if we are within 0–300 seconds BEFORE the release
_BLS_PRERELEASE_WINDOW_S = 300   # 5 minutes


def scan_bls_preposition(
    current_markets: list[dict],
    prices:          dict,
) -> "list":
    """
    Signal strangle entry on KXFED contracts in the 5 minutes before a BLS release.

    For contracts priced between 35–65¢ (high uncertainty), signal BOTH yes and no
    simultaneously to enter a pre-release strangle.

    Only fires if fewer than 2 existing bls_preposition positions are open (checked
    via the prices dict — caller should pass open positions count if available, but
    the function uses a simple in-dict check as a best-effort guard).

    Returns list[SignalMessage].
    """
    from ep_schema import SignalMessage  # noqa: PLC0415

    now_utc = _dt_module.datetime.now(_dt_module.timezone.utc)

    # Check if we are within the pre-release window for any scheduled release
    in_window = False
    for month, day, hour, minute in _BLS_RELEASE_TIMES_UTC:
        try:
            release_dt = _dt_module.datetime(
                now_utc.year, month, day, hour, minute,
                tzinfo=_dt_module.timezone.utc,
            )
        except ValueError:
            continue
        delta_s = (release_dt - now_utc).total_seconds()
        if 0 < delta_s < _BLS_PRERELEASE_WINDOW_S:
            in_window = True
            log.info(
                "BLS pre-position window: release at %s UTC in %.0fs",
                release_dt.strftime("%Y-%m-%d %H:%M"), delta_s,
            )
            break

    if not in_window:
        return []

    # Count existing bls_preposition positions as a best-effort guard.
    # The caller may embed open position counts in the prices dict under a
    # special key, or the function just counts how many bls_preposition entries
    # exist in the prices dict.
    existing_bls = sum(
        1 for k in prices
        if isinstance(k, str) and k.startswith("bls_preposition:")
    )
    if existing_bls >= 2:
        log.debug("BLS pre-position: already %d positions open — skipping", existing_bls)
        return []

    # Filter KXFED contracts in the uncertain 35–65¢ range
    fomc_markets = [m for m in current_markets if m.get("ticker", "").startswith("KXFED-")]

    signals = []
    fair_value  = 0.5    # neutral — resolution determines outcome
    confidence  = 0.60
    notes_str   = "pre-release strangle — exit loser within 60s of print"

    for market in fomc_markets:
        ticker = market.get("ticker", "")
        price  = prices.get(ticker)
        if price is None:
            price = _market_mid(market)
        else:
            try:
                price = float(price) / 100.0 if float(price) > 1.0 else float(price)
            except (TypeError, ValueError):
                price = _market_mid(market)

        if not (0.35 <= price <= 0.65):
            continue   # only target high-uncertainty contracts

        edge  = abs(fair_value - price)
        fee_e = _fee_adjusted_edge(fair_value, price, "yes")

        # Emit YES leg
        try:
            yes_sig = SignalMessage(
                asset_class       = "kalshi",
                strategy          = "bls_preposition",
                category          = "fomc",
                ticker            = ticker,
                exchange          = "kalshi",
                side              = "yes",
                market_price      = round(price,      4),
                fair_value        = fair_value,
                edge              = round(edge,       4),
                fee_adjusted_edge = round(fee_e,      4),
                confidence        = confidence,
                suggested_size    = 1,
                kelly_fraction    = 0.01,
                model_source      = "bls_preposition",
                meeting           = market.get("event_ticker", ""),
            )
            signals.append(yes_sig)
        except Exception as exc:
            log.debug("bls_preposition YES build failed for %s: %s", ticker, exc)

        # Emit NO leg
        no_price = 1.0 - price
        fee_e_no = _fee_adjusted_edge(1.0 - fair_value, no_price, "yes")
        try:
            no_sig = SignalMessage(
                asset_class       = "kalshi",
                strategy          = "bls_preposition",
                category          = "fomc",
                ticker            = ticker,
                exchange          = "kalshi",
                side              = "no",
                market_price      = round(price,       4),
                fair_value        = fair_value,
                edge              = round(edge,        4),
                fee_adjusted_edge = round(fee_e_no,    4),
                confidence        = confidence,
                suggested_size    = 1,
                kelly_fraction    = 0.01,
                model_source      = "bls_preposition",
                meeting           = market.get("event_ticker", ""),
            )
            signals.append(no_sig)
        except Exception as exc:
            log.debug("bls_preposition NO build failed for %s: %s", ticker, exc)

    if signals:
        log.info(
            "BLS pre-position: %d strangle legs for %d contracts",
            len(signals), len(signals) // 2,
        )
    return signals


# ── Universal Market Scanner ─────────────────────────────────────────────────

# Targeted series we have models for — fetched first, always included
_TARGETED_SERIES = [
    "KXFED",      # Fed rate (FOMC directional + arb)
    "KXBTC",      # BTC daily price range
    "KXETH",      # ETH daily price range
    "KXGDP",      # GDP threshold
    "KXHIGHNY", "KXLOWNY", "KXHIGHLA", "KXHIGHCHI", "KXHIGHDC", "KXRAINY",  # weather
    "KXMLB", "KXNBA", "KXNHL", "KXNFL",   # sports championships
]

def scan_all_markets(client, limit_per_page: int = 200) -> list[dict]:
    """
    Fetch open Kalshi markets.  Priority order:
      1. Targeted series we have models for (fast, targeted API calls)
      2. Generic pagination for additional discovery (capped at 1000)
    Returns deduped list sorted by liquidity descending.
    """
    markets    = []
    seen_tickers = set()

    def _add(ms):
        for m in ms:
            t = m.get("ticker","")
            if t and t not in seen_tickers:
                seen_tickers.add(t)
                markets.append(m)

    # 1. Targeted fetches
    for series in _TARGETED_SERIES:
        try:
            data = client.get("/markets", params={"status":"open","series_ticker":series,"limit":200})
            _add(data.get("markets",[]))
        except Exception as exc:
            log.debug("Targeted fetch failed for %s: %s", series, exc)

    # 2. Discovery pagination (cap at 1000 generic markets, sorted by OI)
    cursor = None
    disc_count = 0
    while disc_count < 1000:
        params = {"status":"open","limit":limit_per_page}
        if cursor:
            params["cursor"] = cursor
        try:
            data   = client.get("/markets", params=params)
            page   = data.get("markets",[])
            _add(page)
            cursor = data.get("cursor")
            disc_count += len(page)
            if not cursor or not page:
                break
        except Exception as exc:
            log.warning("Market scan error: %s", exc)
            break

    # Sort by liquidity descending so highest-quality markets are first
    markets.sort(
        key=lambda m: float(m.get("liquidity_dollars") or 0) + float(m.get("volume_fp") or 0),
        reverse=True,
    )

    log.info("Universal scan: %d total open markets (%d targeted series)", len(markets), len(_TARGETED_SERIES))
    return markets


# ── Cross-series coherence scanner (GDP-FOMC) ─────────────────────────────────

async def scan_cross_series_coherence(
    fomc_markets: list[dict],
    gdp_markets: list[dict],
    gdpnow_pct: float,
    max_contracts: int = 3,
) -> list[Signal]:
    """
    GDP-FOMC coherence: weak GDP implies more rate cuts.
    If KXFED YES prices for rate cuts are too low given GDPNow, generate a signal.
    """
    signals: list[Signal] = []

    # Skip if economy is normal/strong — coherence signal less meaningful
    if gdpnow_pct > 2.5:
        log.debug("Cross-series coherence: skipping — GDPNow=%.2f%% > 2.5%%", gdpnow_pct)
        return signals

    # No KXFED markets available
    if not fomc_markets:
        log.debug("Cross-series coherence: no KXFED markets")
        return signals

    # Only meaningful when GDP is weak (< 1.5%) — expect 1+ cut
    if gdpnow_pct >= 1.5:
        log.debug("Cross-series coherence: GDPNow=%.2f%% not weak enough (<1.5%%) for cut signal", gdpnow_pct)
        return signals

    # Linear model: weak GDP → higher cut probability threshold
    # e.g. gdpnow=0.5 → 0.75, gdpnow=1.4 → 0.72
    implied_cut_prob = (2.0 - gdpnow_pct) * 0.3 + 0.40

    # Target KXFED markets for T3.75 or T4.0 (rate ending at/below these levels implies cuts)
    # Require ≥45 days to close — near-term meetings can't move the rate that far
    cut_strikes = {"T3.75", "T4.0"}
    _now_ts = datetime.utcnow().replace(tzinfo=timezone.utc)
    candidate_markets = []
    for m in fomc_markets:
        ticker = m.get("ticker", "")
        if not any(strike in ticker for strike in cut_strikes):
            continue
        close_raw = m.get("close_time") or m.get("expiration_time") or ""
        if close_raw:
            try:
                close_dt = datetime.fromisoformat(
                    close_raw.replace("Z", "+00:00")
                )
                if (close_dt - _now_ts).days < 45:
                    log.debug("Cross-series coherence: skipping %s — closes in <45 days", ticker)
                    continue
            except (ValueError, TypeError):
                pass
        price = _market_mid(m)
        if price > 0:
            candidate_markets.append(m)

    if not candidate_markets:
        log.debug("Cross-series coherence: no T3.75/T4.0 KXFED markets found")
        return signals

    # Sort by ticker (nearest meeting first — alphabetical approximation for KXFED-YYMMM format)
    candidate_markets.sort(key=lambda m: m.get("ticker", ""))

    emitted = 0
    for market in candidate_markets:
        if emitted >= 2:
            break

        ticker = market.get("ticker", "")
        price  = _market_mid(market)

        # If market YES price is below our implied cut probability, it's underpricing cuts
        if price >= implied_cut_prob:
            log.debug(
                "Cross-series coherence: %s YES=%.2f already >= implied=%.2f — skip",
                ticker, price, implied_cut_prob,
            )
            continue

        fair_value = implied_cut_prob
        edge       = fair_value - price          # positive: YES is cheap
        fee_edge   = _fee_adjusted_edge(fair_value, price, "yes")

        if fee_edge <= 0:
            continue

        log.info(
            "Cross-series coherence signal: %s  YES price=%.2f  implied=%.2f  edge=%.2f  gdpnow=%.2f%%",
            ticker, price, fair_value, edge, gdpnow_pct,
        )
        signals.append(Signal(
            ticker            = ticker,
            title             = market.get("title", "") or f"KXFED cut coherence ({ticker})",
            category          = "fomc",
            side              = "yes",
            fair_value        = round(fair_value, 4),
            market_price      = round(price, 4),
            edge              = round(edge, 4),
            fee_adjusted_edge = round(fee_edge, 4),
            contracts         = max_contracts,
            confidence        = 0.60,
            model_source      = "gdp_fomc_coherence",
            spread_cents      = int(abs(
                float(market.get("yes_ask_dollars") or price + 0.02) -
                float(market.get("yes_bid_dollars") or price - 0.02)
            ) * 100),
        ))
        emitted += 1

    if signals:
        log.info(
            "GDP-FOMC coherence: %d signal(s)  gdpnow=%.2f%%  implied_cut_prob=%.2f",
            len(signals), gdpnow_pct, implied_cut_prob,
        )
    return signals


# ── Rate-path calendar spread value scanner ───────────────────────────────────

async def scan_rate_path_value(
    fomc_markets: list[dict],
    max_contracts: int = 3,
) -> list[Signal]:
    """
    Detect calendar spread mispricings: for the same rate threshold,
    P(rate > X at December) should not greatly exceed P(rate > X at June).

    If Dec YES > Jun YES + 0.10 for the same strike, the December market is
    overpriced — emit a NO signal for the overpriced December market.
    """
    signals: list[Signal] = []

    if not fomc_markets:
        return signals

    # Parse ticker format: KXFED-YYMMMDD-TX.XX
    # Group by strike, collect (meeting_date_str, ticker, market) tuples
    import re as _re

    strike_meetings: dict[str, list[tuple[str, str, dict]]] = {}
    for market in fomc_markets:
        ticker = market.get("ticker", "")
        # Match KXFED-YYMMM... format
        m = _re.match(r"^KXFED-(\d{2}[A-Z]{3}\d{2})-(.+)$", ticker)
        if not m:
            continue
        date_str = m.group(1)   # e.g. "25JUN18"
        strike   = m.group(2)   # e.g. "T4.25"
        price    = _market_mid(market)
        if price <= 0:
            continue
        strike_meetings.setdefault(strike, []).append((date_str, ticker, market))

    # For each strike with 2+ meetings, look for Dec YES >> Jun YES
    for strike, meetings in strike_meetings.items():
        if len(meetings) < 2:
            continue

        # Sort chronologically (YYMMM date strings sort alphabetically for same year)
        meetings.sort(key=lambda t: t[0])

        # Slide a window: compare each adjacent earlier/later pair
        for i in range(len(meetings) - 1):
            early_date, early_ticker, early_market = meetings[i]
            later_date, later_ticker, later_market  = meetings[i + 1]

            early_yes = _market_mid(early_market)
            later_yes  = _market_mid(later_market)

            # Violation: later meeting YES > earlier meeting YES + 0.10
            if later_yes > early_yes + 0.10:
                # Later-meeting market is overpriced — sell YES (buy NO)
                # For a NO signal: fair_value is the probability that YES resolves NO,
                # i.e. 1 - later_yes as a rough fair value for NO side.
                # We model the "fair" later price as early_yes + 0.05 (small premium allowed)
                fair_later_yes = early_yes + 0.05
                fair_no_value  = 1.0 - fair_later_yes   # fair prob of NO
                market_no_price = 1.0 - later_yes        # what the market implies for NO

                edge     = fair_no_value - market_no_price   # positive: NO is cheap
                fee_edge = _fee_adjusted_edge(fair_no_value, market_no_price, "no")

                if fee_edge <= 0:
                    continue

                log.info(
                    "Rate-path calendar arb: %s YES=%.2f >> %s YES=%.2f (+%.2f) → NO signal on later",
                    early_ticker, early_yes, later_ticker, later_yes, later_yes - early_yes,
                )
                market_obj = later_market
                signals.append(Signal(
                    ticker            = later_ticker,
                    title             = later_market.get("title", "") or f"Calendar spread NO ({later_ticker})",
                    category          = "arb",
                    side              = "no",
                    fair_value        = round(fair_no_value, 4),
                    market_price      = round(market_no_price, 4),
                    edge              = round(edge, 4),
                    fee_adjusted_edge = round(fee_edge, 4),
                    contracts         = max_contracts,
                    confidence        = 0.65,
                    model_source      = "calendar_spread_arb",
                    spread_cents      = int(abs(
                        float(market_obj.get("yes_ask_dollars") or later_yes + 0.02) -
                        float(market_obj.get("yes_bid_dollars") or later_yes - 0.02)
                    ) * 100),
                    arb_partner       = early_ticker,
                ))

    if signals:
        log.info("Rate-path calendar spread: %d signal(s)", len(signals))
    return signals


# ── YES+NO same-market book arb ───────────────────────────────────────────────

def scan_book_arb(markets: list[dict], min_profit_cents: int = 7) -> list[Signal]:
    """
    Scan all open markets for YES+NO pairs where buying both sides locks in a
    guaranteed profit after Kalshi's 7% fee.

    In a normal market: yes_ask + no_ask ≥ $1.00 (spread > 0).
    Mispricing occurs on illiquid markets when stale resting sell orders create
    yes_ask + no_ask < $0.93, i.e. the total cost is below the fee-adjusted payout.

    Guaranteed net profit (worst case):
      net_yes = (1 - yes_ask) * 0.93 - no_ask
      net_no  = (1 - no_ask)  * 0.93 - yes_ask
      locked  = min(net_yes, net_no)

    Only generates signals when locked > min_profit_cents / 100.
    """
    signals: list[Signal] = []
    min_profit = min_profit_cents / 100.0

    for market in markets:
        if market.get("status", "") != "open":
            continue

        yes_ask = float(market.get("yes_ask_dollars") or 0)
        yes_bid = float(market.get("yes_bid_dollars") or 0)

        if yes_ask <= 0.01 or yes_bid <= 0 or yes_ask >= 0.99:
            continue

        no_ask = 1.0 - yes_bid   # binary market identity: no_ask = 1 - yes_bid

        if no_ask <= 0.01 or no_ask >= 0.99:
            continue

        net_yes = (1.0 - yes_ask) * (1.0 - 0.07) - no_ask
        net_no  = (1.0 - no_ask)  * (1.0 - 0.07) - yes_ask
        locked  = min(net_yes, net_no)

        if locked < min_profit:
            continue

        ticker = market.get("ticker", "")
        title  = market.get("title", ticker)

        signals.append(Signal(
            ticker            = ticker,
            title             = title,
            category          = "arb",
            side              = "yes",
            fair_value        = 0.5,
            market_price      = round((yes_ask + no_ask) / 2, 4),
            edge              = round(locked, 4),
            fee_adjusted_edge = round(locked, 4),
            contracts         = 1,
            confidence        = 0.95,
            model_source      = "book_arb_yes_no",
            arb_legs          = [
                {"ticker": ticker, "side": "yes", "price_cents": int(yes_ask * 100)},
                {"ticker": ticker, "side": "no",  "price_cents": int(no_ask  * 100)},
            ],
        ))
        log.info(
            "Book arb: %s  yes_ask=%.2f  no_ask=%.2f  total=%.2f  locked=%.4f",
            ticker, yes_ask, no_ask, yes_ask + no_ask, locked,
        )

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
    if signals:
        log.info("Book arb scanner: %d opportunities found", len(signals))
    return signals


# ── Near-resolution decay entry ───────────────────────────────────────────────

def scan_near_resolution(markets: list[dict], max_contracts: int = 2) -> list[Signal]:
    """
    Scan for markets priced >95¢ that expire within 6 hours.

    These markets represent nearly-certain outcomes. The remaining premium decays
    to zero at resolution. Edge comes from the gap between current ask price and the
    guaranteed $1.00 payout, minus the Kalshi fee on the win.

    Fee-adjusted edge: (1 - yes_ask) * 0.93 - (1 - yes_ask) = -(yes_ask * 0.07)
    ... that's always negative. The real edge is: time-decay of the remaining premium.

    Actually the bet is:
      Pay yes_ask, receive $1.00 at resolution if YES.
      Net payout after fee: (1 - yes_ask) * 0.93
      Edge = (1 - yes_ask) * 0.93 - (some small residual uncertainty term)

    We only enter when:
      - yes_ask < 0.98 (not already fully priced, some premium left)
      - yes_ask > 0.90 (high confidence market, not directional)
      - hours_to_close < 6.0
      - market volume >= 50 (enough liquidity to fill)
    """
    signals: list[Signal] = []
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

    for market in markets:
        if market.get("status", "") != "open":
            continue

        close_raw = market.get("close_time") or market.get("expiration_time")
        if not close_raw:
            continue
        try:
            close_dt = datetime.fromisoformat(close_raw.replace("Z", "+00:00"))
        except Exception:
            continue

        hours_left = (close_dt - now_utc).total_seconds() / 3600.0
        if hours_left < 0 or hours_left > 6.0:
            continue

        yes_ask = float(market.get("yes_ask_dollars") or 0)
        yes_bid = float(market.get("yes_bid_dollars") or 0)
        vol     = _market_volume(market)

        if yes_ask <= 0.90 or yes_ask >= 0.98 or yes_bid <= 0:
            continue
        if vol < 50:
            continue

        # fair_value = 1.0 (near-certain YES within hours); use _fee_adjusted_edge
        fee_edge = _fee_adjusted_edge(1.0, yes_ask, "yes")
        if fee_edge < 0.005:   # at least 0.5¢ net EV
            continue

        ticker = market.get("ticker", "")
        title  = market.get("title", ticker)
        signals.append(Signal(
            ticker            = ticker,
            title             = title,
            category          = "arb",
            side              = "yes",
            fair_value        = 1.0,
            market_price      = yes_ask,
            edge              = round(1.0 - yes_ask, 4),
            fee_adjusted_edge = round(fee_edge, 4),
            contracts         = max(1, min(max_contracts, max(1, int(fee_edge * 50)))),
            confidence        = min(0.95, 0.90 + (6.0 - hours_left) / 60.0),
            model_source      = "near_resolution_decay",
            close_time        = close_raw,
        ))
        log.info(
            "Near-resolution: %s  yes_ask=%.2f  hours=%.1f  net_payout=%.3f",
            ticker, yes_ask, hours_left, fee_edge,
        )

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
    return signals


# ── Main fetch_signals function ───────────────────────────────────────────────

async def fetch_signals_async(
    client,
    edge_threshold:      float = MIN_EDGE_GROSS,
    max_contracts:       int   = 5,
    min_confidence:      float = 0.60,
    fred_api_key:        str   = "",
    current_rate:        float = 3.75,
    treasury_2y:         Optional[float] = None,
    enable_fomc:         bool  = True,
    enable_weather:      bool  = True,
    enable_economic:     bool  = True,
    enable_sports:       bool  = True,
    markets_cache:       list  = None,
    btc_spot:            Optional[float] = None,
    eth_spot:            Optional[float] = None,
    enable_crypto_price: bool  = True,
    enable_gdp:          bool  = True,
    macro_regime:        Optional[dict] = None,   # from ep:macro Redis hash
    release_data:        Optional[dict] = None,   # from ep:releases Redis hash
    min_yes_entry_price: Optional[float] = None,  # calibrated override from ep_advisor
) -> list[Signal]:
    """
    Main signal-generation entry point — runs all enabled strategy scanners and
    returns consolidated, fee-adjusted signals ready for execution.

    Scanners run in sequence (FOMC arb, FOMC directional, weather, economic, sports,
    crypto price, GDP, coherence checks).  Each scanner is gated by its feature flag
    and any exception is caught individually so a single failure does not abort the run.

    Args:
        client: Authenticated KalshiClient used to fetch live market data.
        edge_threshold: Minimum fee-adjusted edge (decimal) required to keep a signal.
        max_contracts: Per-signal contract cap forwarded to every scanner.
        min_confidence: Signals below this confidence score are dropped before return.
        fred_api_key: FRED API key; economic scanner is skipped when empty.
        current_rate: Current effective federal-funds rate (decimal) for FOMC pricing.
        treasury_2y: Optional 2Y Treasury yield forwarded to the FOMC directional scanner.
        enable_fomc / enable_weather / enable_economic / enable_sports /
        enable_crypto_price / enable_gdp: Feature flags that gate each scanner.
        markets_cache: Pre-fetched market list; re-fetched from Kalshi when None.
        btc_spot / eth_spot: Current BTC/ETH spot prices for the crypto scanner.
        macro_regime: Dict from the ep:macro Redis hash passed to regime-aware scanners.
        release_data: Dict from the ep:releases Redis hash for scheduled data releases.
        min_yes_entry_price: Minimum Kalshi mid required for YES-side FOMC entries.

    Returns:
        List of Signal objects filtered by edge_threshold and min_confidence.
    """
    all_markets = markets_cache if markets_cache else scan_all_markets(client)
    # Targeted KXFED fetch
    try:
        kxfed_data = client.get("/markets", params={"status": "open", "series_ticker": "KXFED", "limit": 200})
        fomc_markets = kxfed_data.get("markets", [])
        log.info("Targeted KXFED scan: %d markets", len(fomc_markets))
    except Exception as exc:
        log.warning("KXFED scan failed: %s", exc)
        fomc_markets = []

    all_signals: list[Signal] = []

    # 0a. YES+NO same-market book arb (runs on all markets, no feature flag needed)
    try:
        book_arb_sigs = scan_book_arb(all_markets)
        all_signals.extend(book_arb_sigs)
    except Exception as exc:
        log.warning("Book arb scan failed: %s", exc)

    # 0b. Near-resolution decay entry (<6h to expiry, >90¢)
    try:
        near_res_sigs = scan_near_resolution(all_markets, max_contracts=2)
        all_signals.extend(near_res_sigs)
    except Exception as exc:
        log.warning("Near-resolution scan failed: %s", exc)

    # 1. FOMC pure arbitrage (highest confidence, no model needed)
    if enable_fomc:
        try:
            arb_sigs = scan_fomc_arb(fomc_markets, max_contracts)
            all_signals.extend(arb_sigs)
            if arb_sigs:
                log.info("FOMC ARB: %d opportunities", len(arb_sigs))
        except Exception as exc:
            log.warning("FOMC arb scan failed: %s", exc)

    # 1b. Economic ladder arb (same invariant as FOMC, applied to CPI/jobs/unemployment/GDP)
    if enable_economic:
        try:
            eco_arb_sigs = scan_economic_ladder_arb(all_markets, max_contracts)
            all_signals.extend(eco_arb_sigs)
        except Exception as exc:
            log.warning("Economic ladder arb scan failed: %s", exc)

    # 2. FOMC directional (FRED rate anchor)
    if enable_fomc:
        try:
            dir_sigs = await scan_fomc_directional(
                fomc_markets, current_rate, max_contracts,
                treasury_2y=treasury_2y,
                macro_regime=macro_regime,
                release_data=release_data,
                min_yes_entry_price=min_yes_entry_price,
            )
            dir_sigs = [s for s in dir_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(dir_sigs)
            if dir_sigs:
                log.info("FOMC directional: %d signals", len(dir_sigs))
        except Exception as exc:
            log.warning("FOMC directional scan failed: %s", exc)

    # 2b. PredictIt divergence signals (Task 4)
    try:
        from ep_predictit import fetch_predictit_fomc, generate_predictit_signals
        predictit_probs = await fetch_predictit_fomc()
        if predictit_probs:
            kalshi_prices = {m.get("ticker", ""): _market_mid(m) for m in fomc_markets if _market_mid(m) > 0}
            predictit_sigs = await generate_predictit_signals(fomc_markets, kalshi_prices)
            all_signals.extend(predictit_sigs)
            if predictit_sigs:
                log.info("PredictIt divergence: %d signals", len(predictit_sigs))
    except ImportError:
        pass   # ep_predictit not available
    except Exception as exc:
        log.warning("PredictIt signal generation failed: %s", exc)

    # 3. Weather
    if enable_weather:
        try:
            weather_sigs = await scan_weather_markets(all_markets, max_contracts)
            weather_sigs = [s for s in weather_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(weather_sigs)
            if weather_sigs:
                log.info("Weather: %d signals", len(weather_sigs))
        except Exception as exc:
            log.warning("Weather scan failed: %s", exc)

    # 4. Economic
    if enable_economic and fred_api_key:
        try:
            econ_sigs = await scan_economic_markets(all_markets, fred_api_key, max_contracts)
            econ_sigs = [s for s in econ_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(econ_sigs)
            if econ_sigs:
                log.info("Economic: %d signals", len(econ_sigs))
        except Exception as exc:
            log.warning("Economic scan failed: %s", exc)

    # 5. Sports
    if enable_sports:
        try:
            sports_sigs = await scan_sports_markets(all_markets, max_contracts)
            sports_sigs = [s for s in sports_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(sports_sigs)
            if sports_sigs:
                log.info("Sports: %d signals", len(sports_sigs))
        except Exception as exc:
            log.warning("Sports scan failed: %s", exc)

    # 6. Crypto price range (KXBTC / KXETH)
    if enable_crypto_price and (btc_spot or eth_spot):
        try:
            # Fetch ETH spot if not provided
            if eth_spot is None:
                eth_spot = await _fetch_eth_spot()
            crypto_sigs = scan_crypto_price_markets(all_markets, btc_spot, eth_spot, max_contracts)
            crypto_sigs = [s for s in crypto_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(crypto_sigs)
            if crypto_sigs:
                log.info("Crypto price: %d signals", len(crypto_sigs))
        except Exception as exc:
            log.warning("Crypto price scan failed: %s", exc)

    # 7. GDP
    if enable_gdp:
        try:
            gdp_sigs = await scan_gdp_markets(all_markets, fred_api_key, max_contracts, macro_regime=macro_regime)
            gdp_sigs = [s for s in gdp_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(gdp_sigs)
            if gdp_sigs:
                log.info("GDP: %d signals", len(gdp_sigs))
        except Exception as exc:
            log.warning("GDP scan failed: %s", exc)

    # 8. Cross-series coherence (GDP-FOMC)
    if enable_fomc and enable_gdp and fred_api_key:
        try:
            # Fetch latest GDPNow estimate for coherence scanner
            _gdpnow_pct: Optional[float] = None
            try:
                _http = _get_http_client()
                _url  = (
                    "https://api.stlouisfed.org/fred/series/observations"
                    f"?series_id=GDPNOW&api_key={fred_api_key}"
                    "&file_type=json&sort_order=desc&limit=1"
                )
                _resp = await _http.get(_url, timeout=8.0)
                if _resp.status_code == 200:
                    _obs = [o for o in _resp.json().get("observations", [])
                            if o.get("value", ".") != "."]
                    if _obs:
                        _gdpnow_pct = float(_obs[0]["value"])
            except Exception as _exc:
                log.debug("GDPNow fetch for coherence scanner failed: %s", _exc)

            if _gdpnow_pct is not None:
                _gdp_markets = [m for m in all_markets if m.get("ticker", "").startswith("KXGDP-")]
                coherence_sigs = await scan_cross_series_coherence(
                    fomc_markets, _gdp_markets, _gdpnow_pct, max_contracts
                )
                coherence_sigs = [s for s in coherence_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
                all_signals.extend(coherence_sigs)
                if coherence_sigs:
                    log.info("Cross-series coherence: %d signals", len(coherence_sigs))
        except Exception as exc:
            log.warning("Cross-series coherence scan failed: %s", exc)

    # 9a. Cross-meeting Bayesian coherence (forward-path monotonicity across FOMC dates)
    if enable_fomc:
        try:
            _prices_snap = {}   # mid prices keyed by ticker — use market data already loaded
            for _m in fomc_markets:
                _t = _m.get("ticker", "")
                if _t:
                    _prices_snap[_t] = _market_mid(_m)
            cm_coherence_sigs = scan_cross_meeting_coherence(fomc_markets, _prices_snap)
            cm_coherence_sigs = [s for s in cm_coherence_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(cm_coherence_sigs)
            if cm_coherence_sigs:
                log.info("Cross-meeting coherence: %d signals", len(cm_coherence_sigs))
        except Exception as exc:
            log.warning("Cross-meeting coherence scan failed: %s", exc)

    # 9b. Rate-path calendar spread value
    if enable_fomc:
        try:
            rate_path_sigs = await scan_rate_path_value(fomc_markets, max_contracts)
            rate_path_sigs = [s for s in rate_path_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(rate_path_sigs)
            if rate_path_sigs:
                log.info("Rate-path calendar spread: %d signals", len(rate_path_sigs))
        except Exception as exc:
            log.warning("Rate-path calendar spread scan failed: %s", exc)

    # Filter by min confidence
    all_signals = [s for s in all_signals if s.confidence >= 0.50]

    # Sort by fee-adjusted edge × confidence
    all_signals.sort(key=lambda s: s.fee_adjusted_edge * s.confidence, reverse=True)

    # Deduplicate (same ticker, keep highest score)
    seen = set()
    deduped = []
    for s in all_signals:
        if s.ticker not in seen:
            deduped.append(s)
            seen.add(s.ticker)

    # Final sort by composite signal quality score (best signals first,
    # important when category limits are hit downstream)
    deduped.sort(key=signal_quality_score, reverse=True)

    log.info(
        "Total signals: %d (%d arb/calendar, %d fomc/coherence, %d weather, %d economic, %d sports, %d crypto_price, %d gdp)",
        len(deduped),
        sum(1 for s in deduped if s.category == "arb"),
        sum(1 for s in deduped if s.category == "fomc"),
        sum(1 for s in deduped if s.category == "weather"),
        sum(1 for s in deduped if s.category == "economic"),
        sum(1 for s in deduped if s.category == "sports"),
        sum(1 for s in deduped if s.category == "crypto_price"),
        sum(1 for s in deduped if s.category == "gdp"),
    )
    return deduped


def fetch_signals(
    client,
    edge_threshold:  float = MIN_EDGE_GROSS,
    max_contracts:   int   = 5,
    min_confidence:  float = 0.60,
    fred_api_key:    str   = "",
    current_rate:    float = 3.75,
    enable_fomc:     bool  = True,
    enable_weather:  bool  = True,
    enable_economic: bool  = True,
    enable_sports:   bool  = True,
    markets_cache:   list  = None,
) -> list[Signal]:
    """Synchronous entry point. Creates fresh event loop each call to avoid semaphore issues."""
    return asyncio.run(fetch_signals_async(
        client         = client,
        edge_threshold = edge_threshold,
        max_contracts  = max_contracts,
        min_confidence = min_confidence,
        fred_api_key   = fred_api_key,
        current_rate   = current_rate,
        enable_fomc    = enable_fomc,
        enable_weather = enable_weather,
        enable_economic= enable_economic,
        enable_sports  = enable_sports,
        markets_cache  = markets_cache,
    ))
