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

# ── Signal dataclass ──────────────────────────────────────────────────────────
@dataclass
class Signal:
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


def _fee_adjusted_edge(fair_value: float, market_price: float, side: str) -> float:
    """Compute edge net of Kalshi fees."""
    if side == "yes":
        net_win  = (1.0 - market_price) * (1 - KALSHI_FEE_RATE)
        net_lose = market_price
        ev = fair_value * net_win - (1 - fair_value) * net_lose
    else:
        net_win  = market_price * (1 - KALSHI_FEE_RATE)
        net_lose = 1.0 - market_price
        ev = fair_value * net_win - (1 - fair_value) * net_lose
    return ev


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


def scan_fomc_arb(markets: list[dict], max_contracts: int) -> list[Signal]:
    """
    Detect monotonicity violations in FOMC T-level contracts.

    For a given meeting, P(rate > X) must decrease as X increases.
    If P(rate > 4.00%) < P(rate > 4.25%), that is impossible and tradeable.

    Pure arb: Buy lower-strike YES + Buy higher-strike NO = risk-free profit.
    Cost = yes_ask(lower) + no_ask(higher) = yes_ask(lower) + (1 - yes_bid(higher))
    For arb to exist: yes_ask(lower) + (1 - yes_bid(higher)) < 1.0
    i.e.: yes_ask(lower) < yes_bid(higher)
    """
    signals = []
    groups  = _group_fomc_by_meeting(markets)

    for event, group in groups.items():
        # Sort by strike ascending
        priced = [(m, _extract_strike(m["ticker"]), _market_mid(m)) for m in group]
        priced = [(m, s, p) for m, s, p in priced if s is not None and p > 0.01]
        if len(priced) < 2:
            continue
        priced.sort(key=lambda x: x[1])  # ascending strike

        # Check monotonicity: P(rate > lower_strike) >= P(rate > higher_strike)
        for i in range(len(priced) - 1):
            m_low, strike_low, price_low   = priced[i]
            m_high, strike_high, price_high = priced[i + 1]

            yes_ask_low  = float(m_low.get("yes_ask_dollars") or price_low + 0.02)
            yes_bid_high = float(m_high.get("yes_bid_dollars") or price_high - 0.02)

            # Arb exists if: buying lower YES + buying higher NO costs < $1
            # = yes_ask_low + (1 - yes_bid_high) < 1.0
            # = yes_ask_low < yes_bid_high
            if yes_ask_low < yes_bid_high - 0.01:  # 1¢ buffer for fees
                arb_profit = yes_bid_high - yes_ask_low
                fee_cost   = arb_profit * KALSHI_FEE_RATE * 2  # fees on both legs
                net_profit = arb_profit - fee_cost

                if net_profit >= MIN_EDGE_GROSS * 0.5:  # lower bar for pure arb
                    contracts = min(max_contracts, max(1, int(net_profit * 50)))
                    signals.append(Signal(
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
                        model_source      = "monotonicity_arb",
                        arb_partner       = m_high["ticker"],
                        meeting           = event,
                    ))
                    log.info(
                        "FOMC ARB: Buy %s YES@%.2f + Buy %s NO@%.2f → net %.2f¢",
                        m_low["ticker"], yes_ask_low,
                        m_high["ticker"], 1 - yes_bid_high,
                        net_profit * 100
                    )

    return signals


async def scan_fomc_directional(
    markets:       list[dict],
    current_rate:  float,
    max_contracts: int,
    treasury_2y:   Optional[float] = None,
) -> list[Signal]:
    """
    Directional FOMC signals using CME FedWatch + ZQ futures + WSJ consensus.

    Primary model: fair_value_with_confidence() from kalshi_bot.models.fomc
      - FedWatch (60% weight) + ZQ futures (30%) + WSJ (10%)
      - Confidence reflects source agreement and data freshness
      - Divergence between sources → automatic confidence reduction

    Fallback (when fomc model returns None): FRED rate-anchor linear decay.

    Confidence is further adjusted by:
      - FOMC meeting proximity  (_fomc_proximity_confidence)
      - 2Y Treasury spread      (bond market's rate path consensus)
    """
    signals = []
    groups  = _group_fomc_by_meeting(markets)

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
    log.info(
        "FOMC directional: model hits=%d misses=%d/%d (priceable/total=%d/%d)",
        model_hits, model_misses, len(priceable), len(priceable), len(all_markets_flat),
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
                model_src  = "kalshi_implied+fred"
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
                # Fallback: proximity adjustment on 0.72 base, then 2Y delta
                confidence = max(0.40, min(0.95,
                    _fomc_proximity_confidence(0.72) + tsy_conf_adj))
                model_src  = f"fred_anchor_{current_rate:.2f}%"

            fair_yes = max(0.01, min(0.99, fair_yes))

            diff = fair_yes - price
            if abs(diff) < MIN_EDGE_GROSS:
                continue

            side = "yes" if diff > 0 else "no"
            edge = abs(diff)
            fair_for_side = fair_yes if side == "yes" else (1 - fair_yes)
            fee_edge = _fee_adjusted_edge(fair_for_side, price if side == "yes" else (1 - price), "yes")

            if fee_edge < MIN_EDGE_GROSS * 0.5:
                continue

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

# Annualised historical volatility estimates (conservative)
_BTC_ANNUAL_VOL = 0.80   # ~80% — typical for BTC
_ETH_ANNUAL_VOL = 0.90   # ~90% — slightly higher than BTC


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

        # Log-normal fair value
        annual_vol = _BTC_ANNUAL_VOL if asset == "BTC" else _ETH_ANNUAL_VOL
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


# ── Weather Model (NOAA NWS) ─────────────────────────────────────────────────

# Known Kalshi weather series and their NOAA grid mapping
WEATHER_SERIES = {
    "KXHIGHNY": {"wfo": "OKX", "x": 33, "y": 37, "type": "high_temp"},
    "KXLOWNY":  {"wfo": "OKX", "x": 33, "y": 37, "type": "low_temp"},
    "KXHIGHLA": {"wfo": "LOX", "x": 148, "y": 48, "type": "high_temp"},
    "KXHIGHCHI":{"wfo": "LOT", "x": 71, "y": 56, "type": "high_temp"},
    "KXHIGHDC": {"wfo": "LWX", "x": 98, "y": 69, "type": "high_temp"},
    "KXRAINY":  {"wfo": "OKX", "x": 33, "y": 37, "type": "precip"},
}


async def fetch_noaa_forecast(wfo: str, x: int, y: int) -> Optional[dict]:
    """Fetch NOAA NWS hourly forecast for a grid point."""
    try:
        http = _get_http_client()
        url  = f"https://api.weather.gov/gridpoints/{wfo}/{x},{y}/forecast/hourly"
        resp = await http.get(url, headers={"User-Agent": "KalshiBot/1.0 (prediction-market-research)"})
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        log.debug("NOAA fetch failed for %s/%d,%d: %s", wfo, x, y, exc)
    return None


def _parse_noaa_temps(forecast_data: dict, hours_ahead: int = 24) -> Optional[dict]:
    """Extract high/low temperature predictions from NOAA forecast."""
    try:
        periods = forecast_data["properties"]["periods"][:hours_ahead]
        temps   = [p["temperature"] for p in periods if p.get("temperature")]
        if not temps:
            return None
        return {
            "high": max(temps),
            "low":  min(temps),
            "avg":  sum(temps) / len(temps),
            "count": len(temps),
        }
    except Exception:
        return None


def _parse_noaa_precip(forecast_data: dict, hours_ahead: int = 24) -> Optional[float]:
    """Extract precipitation probability from NOAA forecast."""
    try:
        periods = forecast_data["properties"]["periods"][:hours_ahead]
        probs   = []
        for p in periods:
            pchance = p.get("probabilityOfPrecipitation", {})
            if pchance and pchance.get("value") is not None:
                probs.append(pchance["value"] / 100.0)
        return max(probs) if probs else None
    except Exception:
        return None


async def scan_weather_markets(markets: list[dict], max_contracts: int) -> list[Signal]:
    """Score weather markets using NOAA NWS forecasts."""
    signals = []

    # Filter to weather markets
    weather_markets = [
        m for m in markets
        if any(m.get("ticker", "").startswith(s) for s in WEATHER_SERIES)
    ]

    if not weather_markets:
        return signals

    # Group by series
    series_forecasts: dict[str, Optional[dict]] = {}
    for series, config in WEATHER_SERIES.items():
        relevant = [m for m in weather_markets if m.get("ticker", "").startswith(series)]
        if not relevant:
            continue
        forecast = await fetch_noaa_forecast(config["wfo"], config["x"], config["y"])
        series_forecasts[series] = (forecast, config)

    for series, result in series_forecasts.items():
        if result is None:
            continue
        forecast, config = result
        relevant = [m for m in weather_markets if m.get("ticker", "").startswith(series)]

        for market in relevant:
            price = _market_mid(market)
            vol   = _market_volume(market)
            if price <= 0.01 or price >= 0.99 or vol < 50:
                continue

            ticker = market.get("ticker", "")
            title  = market.get("title", "")
            fair_value = None

            if config["type"] in ("high_temp", "low_temp"):
                temps = _parse_noaa_temps(forecast)
                if temps is None:
                    continue

                # Extract threshold from title (e.g. "above 75°F")
                thresh_match = re.search(r"(\d+)\s*°?F", title)
                if not thresh_match:
                    continue
                threshold = float(thresh_match.group(1))

                if config["type"] == "high_temp":
                    # P(high > threshold) using normal distribution around forecast
                    import math
                    mean = temps["high"]
                    std  = 4.0  # ~4°F uncertainty for next-day forecast
                    z = (threshold - mean) / std
                    fair_value = 0.5 * (1 - math.erf(z / math.sqrt(2)))

            elif config["type"] == "precip":
                rain_prob = _parse_noaa_precip(forecast)
                if rain_prob is None:
                    continue
                # Kalshi asks "will it rain > X inches" — NOAA gives probability of any precip
                # Adjust down for specific threshold
                thresh_match = re.search(r"(\d+\.?\d*)\s*inch", title, re.IGNORECASE)
                if thresh_match:
                    threshold = float(thresh_match.group(1))
                    # Simple adjustment: more rain needed = lower probability
                    fair_value = rain_prob * max(0.1, 1.0 - threshold * 0.3)
                else:
                    fair_value = rain_prob

            if fair_value is None:
                continue

            fair_value = max(0.02, min(0.98, fair_value))
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
                ticker            = ticker,
                title             = title,
                category          = "weather",
                side              = side,
                fair_value        = fair_value if side == "yes" else (1 - fair_value),
                market_price      = price,
                edge              = round(edge, 4),
                fee_adjusted_edge = round(fee_edge, 4),
                contracts         = contracts,
                confidence        = 0.68,
                model_source      = "noaa_nws",
            ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
    return signals


# ── Economic Model (FRED) ─────────────────────────────────────────────────────

ECONOMIC_SERIES = {
    "CPIAUCSL":  {"name": "CPI",   "keywords": ["cpi", "inflation", "consumer price"]},
    "UNRATE":    {"name": "UNEMP", "keywords": ["unemployment", "jobless"]},
    "PAYEMS":    {"name": "JOBS",  "keywords": ["nonfarm", "payroll", "jobs added"]},
    "GDP":       {"name": "GDP",   "keywords": ["gdp", "gross domestic"]},
    "FEDFUNDS":  {"name": "RATE",  "keywords": ["fed funds", "interest rate"]},
}


async def fetch_fred_series(series_id: str, api_key: str, limit: int = 3) -> Optional[list]:
    """Fetch recent observations from FRED."""
    try:
        http = _get_http_client()
        url  = (
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
        url  = (
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
        and not m.get("ticker", "").startswith("KXGDP")
        and _market_volume(m) >= 50
    ]
    if not econ_markets:
        return signals

    # Fetch FRED data concurrently
    sids    = list(ECONOMIC_SERIES.keys())
    fetched = await asyncio.gather(
        *[fetch_fred_series(sid, fred_api_key) for sid in sids],
        return_exceptions=True,
    )
    results = {
        sid: (None if isinstance(val, Exception) else val)
        for sid, val in zip(sids, fetched)
    }

    # Sigmoid steepness per series.  One "scale unit" corresponds to the
    # typical noise level for that indicator — values beyond ±2 scales from
    # the threshold are treated as high-confidence (outcome nearly certain).
    _ECON_SCALES: dict[str, float] = {
        "CPIAUCSL": 0.30,    # 30 basis-points of CPI change
        "UNRATE":   0.20,    # 20 bp of unemployment
        "PAYEMS":   50.0,    # 50k payroll jobs (series in thousands)
    }
    _DEFAULT_SCALE = 0.25

    for market in econ_markets:
        title = market.get("title", "").lower()
        price = _market_mid(market)
        if price <= 0.01 or price >= 0.99:
            continue

        fair_value  = None
        confidence  = 0.55
        matched_sid = "fred"

        for sid, config in ECONOMIC_SERIES.items():
            if not any(kw in title for kw in config["keywords"]):
                continue
            obs = results.get(sid)
            if not obs:
                continue

            # ── Multi-point weighted trend (exponential decay, up to 6 pts) ──
            vals: list[float] = []
            for o in obs[:6]:
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
            thresh_match = re.search(r"(\d+\.?\d*)\s*[%k]?", market.get("title", ""))
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

            fair_value  = fair_yes
            matched_sid = sid
            break   # matched first relevant series

        if fair_value is None:
            continue

        fair_value = max(0.02, min(0.98, fair_value))
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

        contracts = min(max_contracts, max(1, int(edge * 50)))
        signals.append(Signal(
            ticker            = market["ticker"],
            title             = market.get("title", ""),
            category          = "economic",
            side              = side,
            fair_value        = fair_value if side == "yes" else (1 - fair_value),
            market_price      = price,
            edge              = round(edge, 4),
            fee_adjusted_edge = round(fee_edge, 4),
            contracts         = contracts,
            confidence        = confidence,
            model_source      = f"fred_{matched_sid}_sigmoid",
        ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
    return signals


# ── GDP Scanner (KXGDP) ───────────────────────────────────────────────────────

async def scan_gdp_markets(
    markets:      list[dict],
    fred_api_key: str = "",
    max_contracts: int = 5,
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

    # Fetch Atlanta Fed GDPNow from FRED (series GDPNOW — updates intra-quarter)
    gdp_estimate: Optional[float] = None
    gdp_source = "gdpnow"
    try:
        http = _get_http_client()
        url  = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=GDPNOW&api_key={fred_api_key}"
            "&file_type=json&sort_order=desc&limit=1"
        )
        resp = await http.get(url, timeout=8.0)
        if resp.status_code == 200:
            obs = [o for o in resp.json().get("observations", [])
                   if o.get("value", ".") != "."]
            if obs:
                gdp_estimate = float(obs[0]["value"])
                log.info("GDP model: GDPNow=%.2f%% (as-of %s)", gdp_estimate, obs[0]["date"])
    except Exception as exc:
        log.debug("FRED GDPNow fetch failed: %s — trying fallback", exc)

    # Fallback: backward-looking weighted average of last 4 reported quarters
    if gdp_estimate is None:
        try:
            url2 = (
                "https://api.stlouisfed.org/fred/series/observations"
                f"?series_id=A191RL1Q225SBEA&api_key={fred_api_key}"
                "&file_type=json&sort_order=desc&limit=4"
            )
            resp2 = await http.get(url2, timeout=8.0)
            if resp2.status_code == 200:
                obs2 = [float(o["value"]) for o in resp2.json().get("observations", [])
                        if o.get("value", ".") != "."]
                if obs2:
                    weights      = [4, 3, 2, 1][:len(obs2)]
                    weighted     = sum(v * w for v, w in zip(obs2, weights)) / sum(weights)
                    gdp_estimate = weighted * 0.6 + 2.5 * 0.4
                    gdp_source   = "fred_trend"
                    log.info("GDP model (fallback): last_4q=%s  estimate=%.2f%%", obs2, gdp_estimate)
        except Exception as exc2:
            log.debug("FRED GDP fallback failed: %s", exc2)

    if gdp_estimate is None:
        log.debug("No GDP estimate available — skipping KXGDP scan")
        return signals

    # Uncertainty: GDPNow ~0.9pp RMSE; fallback trend model ~1.5pp
    gdp_uncertainty = 0.9 if gdp_source == "gdpnow" else 1.5

    for market in gdp_markets:
        ticker = market.get("ticker","")
        # Parse threshold from ticker: KXGDP-26APR30-T4.5 → 4.5
        threshold_match = re.search(r"-T(\d+\.?\d*)$", ticker)
        if not threshold_match:
            continue
        threshold = float(threshold_match.group(1))

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

        confidence = min(0.85, 0.50 + abs(fair_value - 0.5))

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
        log.info("GDP scan: %d signals (nowcast=%.2f%%)", len(signals), gdp_estimate)
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


def _parse_espn_win_prob(event: dict) -> Optional[dict]:
    """Extract win probability from ESPN event data."""
    try:
        competitions = event.get("competitions", [])
        if not competitions:
            return None
        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        probs = {}
        for c in competitors:
            team = c.get("team", {}).get("shortDisplayName", "?")
            # ESPN sometimes includes odds
            odds = comp.get("odds", [{}])
            if odds and odds[0].get("homeTeamOdds"):
                if c.get("homeAway") == "home":
                    ml = odds[0]["homeTeamOdds"].get("moneyLine")
                    if ml:
                        prob = 100 / (100 + abs(ml)) if ml < 0 else abs(ml) / (100 + abs(ml))
                        probs[team] = prob
                else:
                    ml = odds[0]["awayTeamOdds"].get("moneyLine")
                    if ml:
                        prob = 100 / (100 + abs(ml)) if ml < 0 else abs(ml) / (100 + abs(ml))
                        probs[team] = prob

        return probs if probs else None
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
                fair_value        = fair_value if side == "yes" else (1 - fair_value),
                market_price      = price,
                edge              = round(edge, 4),
                fee_adjusted_edge = round(fee_edge, 4),
                contracts         = contracts,
                confidence        = 0.65,
                model_source      = f"espn_{best_match}",
            ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
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
) -> list[Signal]:
    """
    Scan all enabled market categories and return fee-adjusted signals.
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

    # 1. FOMC pure arbitrage (highest confidence, no model needed)
    if enable_fomc:
        try:
            arb_sigs = scan_fomc_arb(fomc_markets, max_contracts)
            all_signals.extend(arb_sigs)
            if arb_sigs:
                log.info("FOMC ARB: %d opportunities", len(arb_sigs))
        except Exception as exc:
            log.warning("FOMC arb scan failed: %s", exc)

    # 2. FOMC directional (FRED rate anchor)
    if enable_fomc:
        try:
            dir_sigs = await scan_fomc_directional(fomc_markets, current_rate, max_contracts, treasury_2y=treasury_2y)
            dir_sigs = [s for s in dir_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(dir_sigs)
            if dir_sigs:
                log.info("FOMC directional: %d signals", len(dir_sigs))
        except Exception as exc:
            log.warning("FOMC directional scan failed: %s", exc)

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
            gdp_sigs = await scan_gdp_markets(all_markets, fred_api_key, max_contracts)
            gdp_sigs = [s for s in gdp_sigs if s.fee_adjusted_edge >= edge_threshold * 0.7]
            all_signals.extend(gdp_sigs)
            if gdp_sigs:
                log.info("GDP: %d signals", len(gdp_sigs))
        except Exception as exc:
            log.warning("GDP scan failed: %s", exc)

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

    log.info(
        "Total signals: %d (%d FOMC arb, %d FOMC dir, %d weather, %d economic, %d sports, %d crypto_price, %d gdp)",
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
