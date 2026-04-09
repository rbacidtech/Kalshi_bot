"""
strategy.py — FOMC-only market scanning and signal generation.

Stripped to its core: scan for FOMC markets, price them with the
FedWatch + ZQ futures model, and emit signals where the edge exceeds
the minimum threshold (after fees).

Pipeline per cycle:
  1. scan_fomc_markets()  — fetches only FOMC-series markets (fast, filtered)
  2. fetch_orderbooks()   — concurrent order book fetch for all candidates
  3. fomc.fair_value()    — prices each market using multi-source model
  4. score_signals()      — computes edge, filters by threshold

Speed notes:
  - FOMC markets are few (typically 3-8 open contracts per meeting cycle)
    so order book fetching is very fast even without concurrency
  - FedWatch data is fetched once per TTL window (5 min) and shared
    across all FOMC markets via module-level cache in fomc.py
  - asyncio.run() wraps the pipeline so the main loop stays sync
"""

import asyncio
import logging
from dataclasses import dataclass, field

from .models import fomc
from .models.fomc import fair_value_with_confidence

log = logging.getLogger(__name__)

# Only trade markets with implied probability in this range
# Outside it, edge estimates are unreliable (near-resolved)
_MIN_PROB = 0.03
_MAX_PROB = 0.97

# Minimum book depth (total contracts) to consider a market liquid enough
MIN_BOOK_DEPTH = 50

# Cache scan results to avoid REST call every cycle when WebSocket is live
_scan_cache: list[dict] = []
_scan_cache_ts: float = 0.0
_SCAN_CACHE_TTL: float = 300.0   # 5 minutes — market metadata rarely changes


@dataclass
class Signal:
    """A single actionable trade signal."""
    ticker:        str
    title:         str
    meeting:       str          # "YYYY-MM" meeting identifier
    outcome:       str          # "HOLD", "CUT_25", etc.
    side:          str          # "yes" or "no"
    fair_value:    float        # FedWatch-derived probability
    market_price:  float        # current Kalshi implied probability
    edge:          float        # abs(fair_value - market_price)
    contracts:     int
    confidence:    float        # 0-1 from FOMC model source fusion
    model_source:  str          # human-readable source description
    spread_cents:  int | None = None
    book_depth:    int = 0


# ── Market scanner ────────────────────────────────────────────────────────────

def scan_fomc_markets(client, force_refresh: bool = False) -> list[dict]:
    """
    Fetch only FOMC-series markets from Kalshi.

    Results are cached for 5 minutes — market metadata (title, close_time)
    rarely changes. Prices come from WebSocket, not this scan.
    Pass force_refresh=True to bypass the cache.
    """
    import time as _time
    global _scan_cache, _scan_cache_ts

    now = _time.monotonic()
    if not force_refresh and _scan_cache and (now - _scan_cache_ts) < _SCAN_CACHE_TTL:
        log.debug("scan_fomc_markets: returning %d cached markets.", len(_scan_cache))
        return _scan_cache

    # Try narrow series filter first
    for series in ["FOMC", "KXFED", "FED"]:
        try:
            data    = client.get("/markets", params={
                "status":        "open",
                "series_ticker": series,
                "limit":         50,
            })
            markets = data.get("markets", [])
            if markets:
                log.info("Scanner found %d FOMC markets (series=%s).", len(markets), series)
                _scan_cache    = markets
                _scan_cache_ts = now
                return markets
        except Exception as exc:
            log.debug("Series filter %s failed: %s", series, exc)

    # Fallback: full scan filtered by ticker prefix
    log.info("Series filter returned nothing — falling back to full scan.")
    try:
        data    = client.get("/markets", params={"status": "open", "limit": 200})
        markets = [
            m for m in data.get("markets", [])
            if fomc.parse_fomc_ticker(m.get("ticker", "")) is not None
        ]
        log.info("Fallback scan found %d FOMC markets.", len(markets))
        _scan_cache    = markets
        _scan_cache_ts = now
        return markets
    except Exception as exc:
        log.warning("Full scan failed: %s", exc)
        return _scan_cache  # return stale cache on error rather than empty list


# ── Order book helpers ────────────────────────────────────────────────────────

def _parse_orderbook(ob_data: dict | None, last_cents: int) -> tuple[float, int | None, int]:
    """
    Parse order book into (depth_weighted_fair, spread_cents, book_depth).
    """
    if ob_data is None:
        return last_cents / 100, None, 0

    book = ob_data.get("orderbook", {})
    bids = book.get("yes", [])
    asks = book.get("no", [])

    if not bids or not asks:
        return last_cents / 100, None, 0

    best_bid  = bids[0][0]
    best_ask  = 100 - asks[0][0]
    spread    = max(best_ask - best_bid, 0)
    mid       = (best_bid + best_ask) / 2 / 100

    bid_depth = sum(s for _, s in bids[:5])
    ask_depth = sum(s for _, s in asks[:5])
    depth     = bid_depth + ask_depth

    # Depth-weight mid price
    if depth > 0:
        weighted = (bid_depth * (best_bid / 100) +
                    ask_depth * (best_ask / 100)) / depth
        mid = 0.60 * mid + 0.40 * weighted

    return mid, spread, int(depth)


# ── Core async pipeline ───────────────────────────────────────────────────────

async def _score_markets(
    client,
    markets: list[dict],
    edge_threshold: float,
    max_contracts: int,
    min_confidence: float,
) -> list[Signal]:
    """
    Fetch order books concurrently, run FOMC model, emit signals.
    """
    # Filter to tradeable range before any I/O
    candidates = [
        m for m in markets
        if _MIN_PROB < m.get("last_price", 50) / 100 < _MAX_PROB
        and fomc.parse_fomc_ticker(m.get("ticker", "")) is not None
    ]

    if not candidates:
        log.info("No tradeable FOMC candidates this cycle.")
        return []

    # Concurrent order book fetch
    paths      = [f"/markets/{m['ticker']}/orderbook" for m in candidates]
    ob_results = await client.get_many(paths)

    # Build order book data map
    ob_map: dict[str, tuple] = {}
    for market, ob_data in zip(candidates, ob_results):
        ticker     = market["ticker"]
        last_cents = market.get("last_price", 50)
        ob_map[ticker] = _parse_orderbook(ob_data, last_cents)

    # Fetch (fair_value, confidence) for all candidates in one concurrent gather
    # fair_value_with_confidence() returns both in a single get_meeting_probs() call
    fvc_tasks = [
        fair_value_with_confidence(m["ticker"], m.get("last_price", 50) / 100)
        for m in candidates
    ]
    fvc_results = await asyncio.gather(*fvc_tasks, return_exceptions=True)

    # Score each market
    signals = []
    for market, fvc in zip(candidates, fvc_results):
        ticker     = market["ticker"]
        last_price = market.get("last_price", 50) / 100

        if isinstance(fvc, Exception):
            log.debug("fair_value_with_confidence error for %s: %s", ticker, fvc)
            continue

        fv, confidence = fvc
        if fv is None:
            log.debug("No fair value for %s — skipping.", ticker)
            continue

        diff = fv - last_price
        if abs(diff) < edge_threshold:
            continue

        side = "yes" if diff > 0 else "no"
        edge = abs(diff)

        # Get order book data
        ob_fair, spread, depth = ob_map.get(ticker, (last_price, None, 0))

        # Skip illiquid markets
        if depth < MIN_BOOK_DEPTH:
            log.info("Skipping %s — book depth %d < %d.", ticker, depth, MIN_BOOK_DEPTH)
            continue

        if confidence < min_confidence:
            log.info("Skipping %s — confidence %.2f < %.2f.", ticker, confidence, min_confidence)
            continue

        # parse_fomc_ticker already called during filtering — re-use result
        parsed     = fomc.parse_fomc_ticker(ticker)  # fast: no I/O
        contracts  = min(max_contracts, max(1, int(edge * 100)))

        # Source description from cached meeting probs
        mp         = await fomc.get_meeting_probs(parsed["meeting"])  # hits cache
        source_str = " + ".join(mp.sources) if mp else "fedwatch" 

        signals.append(Signal(
            ticker       = ticker,
            title        = market.get("title", ""),
            meeting      = parsed["meeting"],
            outcome      = parsed["outcome"],
            side         = side,
            fair_value   = round(fv, 4),
            market_price = round(last_price, 4),
            edge         = round(edge, 4),
            contracts    = contracts,
            confidence   = confidence,
            model_source = source_str,
            spread_cents = spread,
            book_depth   = depth,
        ))

    # Sort: highest (edge × confidence) first
    signals.sort(key=lambda s: s.edge * s.confidence, reverse=True)

    if signals:
        log.info("FOMC signals this cycle: %d", len(signals))
        for s in signals:
            log.info(
                "  %-40s  side=%-3s  fv=%.3f  price=%.3f  edge=%.3f  "
                "conf=%.2f  depth=%d  src=%s",
                s.ticker[:40], s.side, s.fair_value, s.market_price,
                s.edge, s.confidence, s.book_depth, s.model_source,
            )
    else:
        log.info("No FOMC signals this cycle.")

    return signals


# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_signals_async(
    client,
    edge_threshold: float,
    max_contracts: int,
    min_confidence: float = 0.60,
) -> list[Signal]:
    markets = scan_fomc_markets(client)
    return await _score_markets(
        client, markets, edge_threshold, max_contracts, min_confidence
    )


def fetch_signals(
    client,
    edge_threshold: float,
    max_contracts: int,
    min_confidence: float = 0.60,
) -> list[Signal]:
    """Synchronous entry point for the main loop."""
    return asyncio.run(
        fetch_signals_async(client, edge_threshold, max_contracts, min_confidence)
    )
