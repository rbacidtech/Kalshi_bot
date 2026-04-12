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
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

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
    """Extract mid-price from new Kalshi API response format."""
    yes_bid = float(market.get("yes_bid_dollars") or 0)
    yes_ask = float(market.get("yes_ask_dollars") or 1)
    last    = float(market.get("last_price_dollars") or 0)
    if yes_bid > 0 and yes_ask < 1:
        return (yes_bid + yes_ask) / 2.0
    return last if last > 0 else 0.50


def _market_volume(market: dict) -> float:
    return float(market.get("volume_fp") or 0)


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


def scan_fomc_directional(markets: list[dict], current_rate: float, max_contracts: int) -> list[Signal]:
    """
    Directional FOMC signals using FRED current rate as anchor.

    Logic: The current rate IS the floor for "above X%" contracts.
    - If current rate = 3.75%, then P(rate > 3.75%) should be HIGH (it already IS 3.75%)
    - If current rate = 3.75%, then P(rate > 4.25%) should be LOW (requires 50bp hike)

    We compare market prices against what's implied by the current rate + FRED trend.
    """
    signals = []
    groups  = _group_fomc_by_meeting(markets)
    days_per_meeting = 60  # approximate days to next meeting

    for event, group in groups.items():
        for market in group:
            strike = _extract_strike(market["ticker"])
            if strike is None:
                continue

            price = _market_mid(market)
            vol   = _market_volume(market)
            if price <= 0.01 or price >= 0.99:
                continue
            if vol < MIN_LIQUIDITY_FP:
                continue

            # Compute fair value based on current rate vs strike
            rate_diff_bps = (strike - current_rate) * 100

            # Simple model: probability decays with distance from current rate
            # Hold probability from FRED trend (~85% for near-term)
            # Each 25bp away from current rate reduces probability
            if rate_diff_bps <= 0:
                # Strike is at or below current rate — YES is very likely
                # (rate is already above strike)
                fair_yes = 0.88 + min(0.09, abs(rate_diff_bps) * 0.001)
            elif rate_diff_bps <= 25:
                # Strike is 25bp above current — requires one hike
                fair_yes = 0.12
            elif rate_diff_bps <= 50:
                # Two hikes needed
                fair_yes = 0.04
            elif rate_diff_bps <= 75:
                fair_yes = 0.02
            else:
                # Very far out — near zero
                fair_yes = 0.01

            # Clip
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
            signals.append(Signal(
                ticker            = market["ticker"],
                title             = market.get("title", ""),
                category          = "fomc",
                side              = side,
                fair_value        = fair_yes if side == "yes" else (1 - fair_yes),
                market_price      = price,
                edge              = round(edge, 4),
                fee_adjusted_edge = round(fee_edge, 4),
                contracts         = contracts,
                confidence        = 0.72,
                model_source      = f"fred_anchor_{current_rate:.2f}%",
                meeting           = event,
            ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
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
    import httpx
    cache_key = f"noaa:{wfo}:{x}:{y}"

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as http:
            url = f"https://api.weather.gov/gridpoints/{wfo}/{x},{y}/forecast/hourly"
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
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            url = (
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


async def scan_economic_markets(markets: list[dict], fred_api_key: str, max_contracts: int) -> list[Signal]:
    """Score economic threshold markets using FRED data."""
    if not fred_api_key:
        return []

    signals = []

    # Filter to economic markets
    econ_keywords = ["cpi", "inflation", "unemployment", "payroll", "jobs", "gdp", "fed"]
    econ_markets  = [
        m for m in markets
        if any(kw in m.get("title", "").lower() for kw in econ_keywords)
        and _market_volume(m) >= 50
    ]
    if not econ_markets:
        return signals

    # Fetch FRED data concurrently
    tasks = {sid: fetch_fred_series(sid, fred_api_key) for sid in ECONOMIC_SERIES}
    results = {}
    for sid, coro in tasks.items():
        try:
            results[sid] = await coro
        except Exception:
            results[sid] = None

    for market in econ_markets:
        title = market.get("title", "").lower()
        price = _market_mid(market)
        if price <= 0.01 or price >= 0.99:
            continue

        fair_value = None

        # Match market to FRED series
        for sid, config in ECONOMIC_SERIES.items():
            if not any(kw in title for kw in config["keywords"]):
                continue
            obs = results.get(sid)
            if not obs:
                continue

            latest_val = float(obs[0]["value"])
            prev_val   = float(obs[1]["value"]) if len(obs) > 1 else latest_val

            # Extract threshold from title
            thresh_match = re.search(r"(\d+\.?\d*)\s*%", market.get("title", ""))
            if not thresh_match:
                continue
            threshold = float(thresh_match.group(1))

            # Simple model: is current value above/below threshold?
            # Adjust for trend
            trend = latest_val - prev_val
            projected = latest_val + trend * 0.5  # simple linear projection

            if sid == "CPIAUCSL":
                # CPI year-over-year — compare to threshold
                fair_value = 0.75 if projected >= threshold else 0.25
            elif sid == "UNRATE":
                fair_value = 0.75 if projected >= threshold else 0.25
            elif sid == "PAYEMS":
                # Monthly job gains — convert to thousands if needed
                fair_value = 0.70 if projected >= threshold * 1000 else 0.30
            else:
                fair_value = 0.60 if projected >= threshold else 0.40

            break  # matched first series

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
            confidence        = 0.62,
            model_source      = "fred_economic",
        ))

    signals.sort(key=lambda s: s.fee_adjusted_edge, reverse=True)
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
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as http:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
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

def scan_all_markets(client, limit_per_page: int = 200) -> list[dict]:
    """
    Fetch ALL open Kalshi markets with cursor pagination.
    Returns full list with new API field names intact.
    """
    markets = []
    params  = {"status": "open", "limit": limit_per_page}
    cursor  = None

    while True:
        if cursor:
            params["cursor"] = cursor
        try:
            data   = client.get("/markets", params=params)
            page   = data.get("markets", [])
            markets.extend(page)
            cursor = data.get("cursor")
            if not cursor or not page:
                break
            if len(markets) > 2000:  # safety cap
                log.warning("Market scan capped at 2000 markets.")
                break
        except Exception as exc:
            log.warning("Market scan error: %s", exc)
            break

    log.info("Universal scan: %d total open markets", len(markets))
    return markets


# ── Main fetch_signals function ───────────────────────────────────────────────

async def fetch_signals_async(
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
) -> list[Signal]:
    """
    Scan all enabled market categories and return fee-adjusted signals.
    """
    all_markets = scan_all_markets(client)
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
            dir_sigs = scan_fomc_directional(fomc_markets, current_rate, max_contracts)
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
        "Total signals: %d (%d FOMC arb, %d FOMC dir, %d weather, %d economic, %d sports)",
        len(deduped),
        sum(1 for s in deduped if s.category == "arb"),
        sum(1 for s in deduped if s.category == "fomc"),
        sum(1 for s in deduped if s.category == "weather"),
        sum(1 for s in deduped if s.category == "economic"),
        sum(1 for s in deduped if s.category == "sports"),
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
    ))
