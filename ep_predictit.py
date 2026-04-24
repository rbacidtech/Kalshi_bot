"""
ep_predictit.py — PredictIt cross-market arbitrage monitor.

PredictIt (predictit.org) lists prediction markets on the same FOMC outcomes
as Kalshi. Persistent price divergence between platforms signals:
  1. Arbitrage opportunity (if both markets are liquid)
  2. Kalshi pricing error (if PredictIt has more informed participants)
  3. Platform-specific demand imbalance (less useful as signal)

API: GET https://www.predictit.org/api/marketdata/all
     No auth required. Returns all active markets with contract prices.

Rate limit: Be respectful — fetch no more than once per 5 minutes.
"""

import logging
import time
from typing import Optional

import httpx

# ep_config must be imported first so sys.path is set before kalshi_bot imports
import ep_config  # noqa: F401 — side effect: sys.path bootstrap
from kalshi_bot.models.cache import get_cache

__all__ = [
    "fetch_predictit_fomc",
    "compute_predictit_divergence",
    "generate_predictit_signals",
]

log = logging.getLogger("edgepulse.predictit")

# ── Constants ─────────────────────────────────────────────────────────────────

_PREDICTIT_API_URL = "https://www.predictit.org/api/marketdata/all"
_TIMEOUT           = 10.0   # seconds per HTTP request
_TTL_PREDICTIT     = 300    # 5 minutes — rate-limit courtesy
_MIN_DIVERGENCE    = 0.04   # 4 cents minimum to flag a divergence
_MAX_SIGNALS       = 2      # hard cap per cycle to avoid over-trading on stale data

# Sanity bounds for contract price sums (reject illiquid / data-error markets)
_SUM_LO = 0.80
_SUM_HI = 1.20

# Prices at the floor/ceiling are not real — skip them
_PRICE_FLOOR = 0.01
_PRICE_CEIL  = 0.99

# Extreme values that signal illiquidity — skip in divergence check
_ILLIQUID_LO = 0.02
_ILLIQUID_HI = 0.98

# ── Outcome mapping ───────────────────────────────────────────────────────────

#: Map PredictIt contract names (lowercase, partial) → OUTCOME_BPS keys
_PREDICTIT_OUTCOME_MAP: dict[str, str] = {
    "no change":         "HOLD",
    "no change (hold)":  "HOLD",
    "hold":              "HOLD",
    "increase 25":       "HIKE_25",
    "increase 0.25":     "HIKE_25",
    "raise 25":          "HIKE_25",
    "decrease 25":       "CUT_25",
    "decrease 0.25":     "CUT_25",
    "cut 25":            "CUT_25",
    "lower 25":          "CUT_25",
    "decrease 50":       "CUT_50",
    "cut 50":            "CUT_50",
    "decrease 0.50":     "CUT_50",
    "increase 50":       "HIKE_50",
}

# Browser-like headers to avoid bot-blocking
_HEADERS = {
    "User-Agent":      (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.predictit.org/",
}

_cache = get_cache()

# Shared async HTTP client
_http_client: "httpx.AsyncClient | None" = None


async def _get_http_client() -> "httpx.AsyncClient":
    """Return the shared httpx client, creating it if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers=_HEADERS,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )
    return _http_client


def _map_contract_name(name: str) -> Optional[str]:
    """
    Map a PredictIt contract name to a OUTCOME_BPS key.

    Performs a case-insensitive prefix/substring search against the outcome map.
    Returns None if no match found.
    """
    name_lower = name.lower().strip()
    # Exact match first
    if name_lower in _PREDICTIT_OUTCOME_MAP:
        return _PREDICTIT_OUTCOME_MAP[name_lower]
    # Partial match — longest key that is contained in the contract name wins
    best_key   = None
    best_len   = 0
    for key, outcome in _PREDICTIT_OUTCOME_MAP.items():
        if key in name_lower and len(key) > best_len:
            best_key = outcome
            best_len = len(key)
    return best_key


def _is_fomc_market(short_name: str) -> bool:
    """Return True if the market's shortName looks like an FOMC rate market."""
    low = short_name.lower()
    return any(kw in low for kw in (
        "fed", "fomc", "federal funds",
        "rate hike", "rate cut", "rate decision", "interest rate", "basis points",
    ))


async def fetch_predictit_fomc() -> dict[str, dict]:
    """
    Fetch PredictIt FOMC rate markets and return probability maps keyed by
    a Kalshi-compatible ticker prefix.

    Returns:
        {
            "KXFED-25APR": {"HOLD": 0.72, "CUT_25": 0.20, ...},
            ...
        }

    On any error returns {} (never raises).
    Cached for _TTL_PREDICTIT seconds (300 s).
    """
    cache_key = "predictit:fomc:all"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        client = await _get_http_client()
        resp   = await client.get(_PREDICTIT_API_URL)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("fetch_predictit_fomc: HTTP error — %s", exc)
        return {}

    markets_raw = data.get("markets", [])
    if not markets_raw:
        log.warning("fetch_predictit_fomc: empty markets list in response")
        return {}

    result: dict[str, dict] = {}
    matched_count = 0

    for market in markets_raw:
        short_name = market.get("shortName", "")
        if not _is_fomc_market(short_name):
            log.debug("fetch_predictit_fomc: skipping non-FOMC market: %r", short_name)
            continue

        matched_count += 1
        contracts = market.get("contracts", [])
        if not contracts:
            continue

        # Build outcome → best-price mapping
        outcome_prices: dict[str, float] = {}
        for contract in contracts:
            contract_name = contract.get("shortName", "")
            # PredictIt uses lastTradePrice or bestBuyYesCost for the "Yes" price
            price = contract.get("lastTradePrice") or contract.get("bestBuyYesCost") or 0.0
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue

            # Skip floor/ceiling prices — they are not real market prices
            if price == _PRICE_FLOOR or price == _PRICE_CEIL:
                log.debug(
                    "fetch_predictit_fomc: skipping floor/ceil price %.2f for '%s'",
                    price, contract_name,
                )
                continue

            outcome = _map_contract_name(contract_name)
            if outcome is None:
                log.debug(
                    "fetch_predictit_fomc: unmapped contract '%s' in '%s'",
                    contract_name, short_name,
                )
                continue

            # Keep the highest-priced contract if a duplicate outcome appears
            if outcome not in outcome_prices or price > outcome_prices[outcome]:
                outcome_prices[outcome] = price

        if not outcome_prices:
            continue

        # Sanity check: total should sum close to 1.0
        price_sum = sum(outcome_prices.values())
        if not (_SUM_LO <= price_sum <= _SUM_HI):
            log.warning(
                "fetch_predictit_fomc: skipping market '%s' — price sum %.3f "
                "outside [%.2f, %.2f]",
                short_name, price_sum, _SUM_LO, _SUM_HI,
            )
            continue

        # Derive a Kalshi-like ticker prefix from the market name / ID
        # e.g. "Will the Fed change rates at the April 2025 meeting?" → "KXFED-25APR"
        market_id  = market.get("id", 0)
        ticker_key = f"KXFED-PI-{market_id}"

        result[ticker_key] = outcome_prices
        log.debug(
            "fetch_predictit_fomc: captured market '%s' (%s) → %s",
            short_name, ticker_key, outcome_prices,
        )

    _cache.set(cache_key, result, ttl=_TTL_PREDICTIT)
    log.info(
        "fetch_predictit_fomc: %d total markets, %d matched FOMC filter",
        len(markets_raw), matched_count,
    )
    if len(result) == 0:
        log.debug(
            "fetch_predictit_fomc: fetched 0 FOMC markets from PredictIt (PI may not list FOMC rate markets)",
        )
    else:
        log.info(
            "fetch_predictit_fomc: fetched %d FOMC markets from PredictIt",
            len(result),
        )
    return result


async def compute_predictit_divergence(
    kalshi_probs: dict,
    predictit_probs: dict,
    meeting_key: str,
) -> list[dict]:
    """
    Compare Kalshi outcome probabilities against PredictIt probabilities.

    Args:
        kalshi_probs:    {outcome: probability}  from Kalshi market prices
        predictit_probs: {outcome: probability}  from PredictIt
        meeting_key:     FOMC meeting identifier, e.g. "2025-05"

    Returns:
        List of divergence dicts for outcomes where
        abs(kalshi_p - predictit_p) >= _MIN_DIVERGENCE (0.04).

        Each element:
        {
            "outcome":       "CUT_25",
            "kalshi":        0.72,
            "predictit":     0.65,
            "divergence":    0.07,
            "edge_direction": "kalshi_high",   # or "predictit_high"
            "confidence":    "MEDIUM",          # HIGH / MEDIUM / LOW
            "meeting":       "2025-05",
        }
    """
    divergences: list[dict] = []

    # Only compare outcomes that appear in both dicts
    shared_outcomes = set(kalshi_probs) & set(predictit_probs)

    for outcome in shared_outcomes:
        kalshi_p    = kalshi_probs[outcome]
        predictit_p = predictit_probs[outcome]

        # Skip extreme values — illiquid, not real prices
        if not (_ILLIQUID_LO < kalshi_p < _ILLIQUID_HI):
            log.debug(
                "compute_predictit_divergence: skipping %s — kalshi_p %.3f "
                "outside illiquid bounds",
                outcome, kalshi_p,
            )
            continue
        if not (_ILLIQUID_LO < predictit_p < _ILLIQUID_HI):
            log.debug(
                "compute_predictit_divergence: skipping %s — predictit_p %.3f "
                "outside illiquid bounds",
                outcome, predictit_p,
            )
            continue

        div = abs(kalshi_p - predictit_p)
        if div < _MIN_DIVERGENCE:
            continue

        edge_direction = "kalshi_high" if kalshi_p > predictit_p else "predictit_high"

        # Confidence tiers
        if div > 0.07:
            confidence = "HIGH"
        elif div >= 0.05:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        divergences.append({
            "outcome":        outcome,
            "kalshi":         round(kalshi_p,    4),
            "predictit":      round(predictit_p, 4),
            "divergence":     round(div,          4),
            "edge_direction": edge_direction,
            "confidence":     confidence,
            "meeting":        meeting_key,
        })

    # Sort by divergence descending so the caller sees the strongest signal first
    divergences.sort(key=lambda d: d["divergence"], reverse=True)
    return divergences


async def generate_predictit_signals(
    markets: list,
    kalshi_prices: dict,
) -> list:
    """
    Compare Kalshi vs PredictIt for each FOMC meeting and emit trade signals.

    Logic:
      - HIGH confidence divergence where PredictIt shows LOWER prob than Kalshi
        → Kalshi is overpriced vs consensus → BUY NO
      - HIGH confidence divergence where PredictIt shows HIGHER prob than Kalshi
        → Kalshi is underpriced vs consensus → BUY YES

    Args:
        markets:       list of Kalshi market dicts (must contain 'ticker', 'meeting',
                       'outcome', 'yes_price' / 'no_price' fields)
        kalshi_prices: {ticker: {"yes_price": float, "no_price": float, ...}}

    Returns:
        List of Signal-compatible dicts (at most _MAX_SIGNALS = 2 per cycle).
    """
    predictit_data = await fetch_predictit_fomc()
    if not predictit_data:
        log.debug("generate_predictit_signals: no PredictIt data available")
        return []

    # Group Kalshi markets by meeting key
    by_meeting: dict[str, list] = {}
    for mkt in markets:
        meeting = mkt.get("meeting") or mkt.get("subtitle", "")
        if not meeting:
            continue
        by_meeting.setdefault(meeting, []).append(mkt)

    signals: list[dict] = []

    for meeting_key, meeting_markets in by_meeting.items():
        if len(signals) >= _MAX_SIGNALS:
            break

        # Build Kalshi prob map for this meeting
        kalshi_probs: dict[str, float] = {}
        ticker_for_outcome: dict[str, dict] = {}
        for mkt in meeting_markets:
            outcome = mkt.get("outcome")
            ticker  = mkt.get("ticker", "")
            price_info = kalshi_prices.get(ticker, {})
            yes_price  = price_info.get("yes_price") or price_info.get("last_price", 0.0)
            try:
                yes_price = float(yes_price)
            except (TypeError, ValueError):
                continue
            if outcome and 0.0 < yes_price < 1.0:
                kalshi_probs[outcome]       = yes_price
                ticker_for_outcome[outcome] = mkt

        if not kalshi_probs:
            continue

        # Find the best matching PredictIt market for this meeting
        # (in practice there's usually only 1 FOMC market per meeting)
        for pi_key, predictit_probs in predictit_data.items():
            if len(signals) >= _MAX_SIGNALS:
                break

            divergences = await compute_predictit_divergence(
                kalshi_probs, predictit_probs, meeting_key
            )

            for div in divergences:
                if len(signals) >= _MAX_SIGNALS:
                    break

                if div["confidence"] != "HIGH":
                    continue

                outcome     = div["outcome"]
                kalshi_p    = div["kalshi"]
                predictit_p = div["predictit"]
                edge_dir    = div["edge_direction"]

                mkt = ticker_for_outcome.get(outcome)
                if mkt is None:
                    continue

                ticker = mkt.get("ticker", "")

                # Determine trade direction
                if edge_dir == "kalshi_high":
                    # Kalshi overpriced → sell YES → BUY NO
                    side       = "no"
                    market_p   = 1.0 - kalshi_p  # NO price
                    fair_v     = 1.0 - predictit_p
                else:
                    # Kalshi underpriced → BUY YES
                    side       = "yes"
                    market_p   = kalshi_p
                    fair_v     = predictit_p

                edge = abs(fair_v - market_p)

                model_source = (
                    f"predictit_div_v={predictit_p:.2f}_k={kalshi_p:.2f}"
                )

                signal = {
                    "asset_class": "kalshi",
                    "strategy":    "predictit_divergence",
                    "category":    "fomc",
                    "ticker":      ticker,
                    "exchange":    "kalshi",
                    "side":        side,
                    "market_price":      round(market_p, 4),
                    "fair_value":        round(fair_v,   4),
                    "edge":              round(edge,     4),
                    # Fee proxy aligned with ep_polymarket.py (2¢ flat). Using a
                    # shared value so divergence signals from both feeds rank
                    # consistently against each other at the exec-side filter.
                    # Kalshi actual fee ≈ 7% of net winnings (variable); 2¢ is
                    # a reasonable flat approximation for typical mid-priced
                    # contracts. Was 1¢ — caused PredictIt signals to appear
                    # 1¢ "cheaper" than Polymarket for identical edge.
                    "fee_adjusted_edge": round(max(0.0, edge - 0.02), 4),
                    # Confidence: 0.65 (MEDIUM) to 0.75 (HIGH)
                    "confidence":  0.75,
                    "suggested_size": 1,
                    "kelly_fraction": 0.02,
                    "risk_flags":  ["MODEL_DIVERGENCE"],
                    "meeting":     meeting_key,
                    "outcome":     outcome,
                    "model_source": model_source,
                }

                signals.append(signal)
                log.info(
                    "generate_predictit_signals: signal %s %s %s "
                    "kalshi=%.3f predictit=%.3f div=%.3f",
                    side.upper(), outcome, ticker,
                    kalshi_p, predictit_p, div["divergence"],
                )

    if len(signals) > _MAX_SIGNALS:
        signals = signals[:_MAX_SIGNALS]

    return signals
