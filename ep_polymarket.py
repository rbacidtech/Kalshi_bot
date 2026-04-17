"""
ep_polymarket.py — Polymarket price feed and Kalshi divergence signals.

Fetches live implied probabilities from Polymarket's public Gamma API,
matches them to Kalshi markets by keyword similarity, and generates
arbitrage signals when prices diverge by more than DIVERGENCE_THRESHOLD.

Architecture:
  PolymarketFeed.refresh()     — call once per cycle; hits Gamma API
  PolymarketFeed.divergence_signals(kalshi_signals) — returns list[Signal]

Caching: Polymarket prices are cached for CACHE_TTL seconds (default 60).
Rate limit: Gamma API allows 300 req/10s — we make at most 1 req/cycle.

No auth required for price reads.
"""

import asyncio
import datetime
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

from ep_config import log

# ── Config ─────────────────────────────────────────────────────────────────────

GAMMA_URL           = "https://gamma-api.polymarket.com"
DIVERGENCE_THRESHOLD = 0.04   # 4 cents — minimum gap to generate a signal
CACHE_TTL           = 60      # seconds between Gamma API refreshes (default)
_HTTP_TIMEOUT       = 8.0

# BLS/FOMC release times (UTC): CPI ~12:30, NFP ~12:30 first Friday, FOMC 18:00
_RELEASE_HOURS_UTC = {7, 8, 12, 13, 17, 18, 19}  # hours when major releases happen


def _active_cache_ttl() -> int:
    """Return 10s TTL during macro release windows, 60s otherwise."""
    hour = datetime.datetime.utcnow().hour
    return 10 if hour in _RELEASE_HOURS_UTC else 60

# ── Static mapping: Kalshi series prefix → Polymarket search keywords ──────────
# This tells the matcher what to search for when looking up Polymarket peers.
_SERIES_KEYWORDS: Dict[str, List[str]] = {
    "KXFED":   ["federal reserve rate", "fed rate decision", "fomc"],
    "KXCPI":   ["consumer price index", "cpi inflation"],
    "KXNFP":   ["nonfarm payroll", "jobs report", "unemployment"],
    "KXGDP":   ["gdp growth", "gross domestic product"],
    "KXPCE":   ["pce inflation", "personal consumption"],
    "KXBTC":   ["bitcoin price", "btc price"],
    "KXETH":   ["ethereum price", "eth price"],
    "INX":     ["s&p 500", "sp500"],
    "NASDAQ":  ["nasdaq", "qqq"],
}

# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class PolyMarket:
    """Minimal representation of a Polymarket binary market."""
    condition_id:  str
    question:      str
    yes_price:     float   # 0.0–1.0
    no_price:      float   # 0.0–1.0 (= 1 - yes_price for binary)
    volume_24h:    float
    active:        bool


# ── Main class ─────────────────────────────────────────────────────────────────

class PolymarketFeed:
    """
    Fetches and caches Polymarket prices.  Call refresh() each cycle, then
    divergence_signals() to get any arb opportunities vs your Kalshi signals.
    """

    def __init__(self, divergence_threshold: float = DIVERGENCE_THRESHOLD) -> None:
        self._threshold = divergence_threshold
        self._cache:     Dict[str, List[PolyMarket]] = {}   # keyword → markets
        self._last_fetch: float = 0.0
        self._poly_markets: List[PolyMarket] = []

    # ── Public API ─────────────────────────────────────────────────────────────

    async def refresh(self) -> None:
        """Fetch active Polymarket markets.  Respects CACHE_TTL (reduced during macro windows)."""
        if time.time() - self._last_fetch < _active_cache_ttl():
            return
        try:
            markets = await self._fetch_active_markets()
            self._poly_markets = markets
            self._last_fetch   = time.time()
            log.debug("Polymarket: fetched %d active markets", len(markets))
        except Exception as exc:
            log.warning("Polymarket refresh failed: %s", exc)

    def divergence_signals(self, kalshi_signals: list) -> list:
        """
        Compare Kalshi signals to Polymarket prices.

        For each Kalshi signal, find the best-matching Polymarket market.
        If the absolute price difference exceeds self._threshold:
          - If Kalshi YES is cheaper → generate YES signal to trade Kalshi up
          - If Kalshi YES is pricier  → generate NO signal to trade Kalshi down

        Returns a list of new Signal objects with source="polymarket_arb".
        """
        if not self._poly_markets or not kalshi_signals:
            return []

        new_signals = []
        for sig in kalshi_signals:
            match = self._find_match(sig)
            if match is None:
                continue

            kalshi_yes = sig.market_price          # 0.0–1.0
            poly_yes   = match.yes_price           # 0.0–1.0
            diff       = poly_yes - kalshi_yes     # positive → Kalshi is cheap

            if abs(diff) < self._threshold:
                continue

            # Import here to avoid circular — Signal lives in kalshi_bot.strategy
            try:
                from kalshi_bot.strategy import Signal
            except ImportError:
                break

            if diff > 0:
                # Polymarket says YES should be higher → buy YES on Kalshi
                arb_side  = "yes"
                arb_fv    = poly_yes
                arb_edge  = diff
            else:
                # Polymarket says YES should be lower → buy NO on Kalshi
                arb_side  = "no"
                arb_fv    = 1.0 - poly_yes   # NO fair value
                arb_edge  = abs(diff)

            arb_sig = Signal(
                ticker            = sig.ticker,
                title             = sig.title,
                category          = sig.category,
                meeting           = sig.meeting,
                outcome           = sig.outcome,
                side              = arb_side,
                fair_value        = arb_fv,
                market_price      = sig.market_price,
                edge              = arb_edge,
                fee_adjusted_edge = max(0.0, arb_edge - 0.02),
                contracts         = sig.contracts,
                confidence        = min(0.80, 0.50 + arb_edge * 3),
                model_source      = f"polymarket_arb ({match.question[:40]})",
                spread_cents      = sig.spread_cents,
            )

            new_signals.append(arb_sig)
            log.info(
                "Polymarket arb: %-38s  kalshi=%.2f  poly=%.2f  diff=%+.3f  side=%s",
                sig.ticker[:38], kalshi_yes, poly_yes, diff, arb_side,
            )

        return new_signals

    # ── Internal ───────────────────────────────────────────────────────────────

    def _find_match(self, sig) -> Optional[PolyMarket]:
        """
        Find the most relevant Polymarket market for a Kalshi signal.
        Uses series prefix to determine search keywords, then picks the
        active market with the highest 24h volume.
        """
        series_prefix = sig.ticker.split("-")[0] if "-" in sig.ticker else sig.ticker
        keywords = _SERIES_KEYWORDS.get(series_prefix, [])
        if not keywords:
            return None

        candidates = []
        for pm in self._poly_markets:
            question_lower = pm.question.lower()
            if any(kw in question_lower for kw in keywords):
                candidates.append(pm)

        if not candidates:
            return None

        # Prefer highest-volume active market
        return max(candidates, key=lambda m: m.volume_24h)

    async def _fetch_active_markets(self) -> List[PolyMarket]:
        """
        Fetch active binary markets from Polymarket Gamma API.
        Returns parsed PolyMarket objects.
        """
        results: List[PolyMarket] = []
        params = {
            "active":   "true",
            "closed":   "false",
            "limit":    500,
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{GAMMA_URL}/markets", params=params)
            if resp.status_code != 200:
                log.warning("Polymarket Gamma API %d: %s", resp.status_code, resp.text[:200])
                return []
            data = resp.json()

        # Gamma API returns a list directly or wrapped in {"markets": [...]}
        if isinstance(data, list):
            raw_markets = data
        elif isinstance(data, dict):
            raw_markets = data.get("markets", [])
        else:
            return []

        for m in raw_markets:
            try:
                outcome_prices = m.get("outcomePrices", [])
                outcomes       = m.get("outcomes", [])

                if not outcome_prices or len(outcome_prices) < 2:
                    continue

                # Gamma returns outcomePrices as strings like ["0.73", "0.27"]
                yes_price = float(outcome_prices[0])
                no_price  = float(outcome_prices[1])

                # Sanity check — binary market probabilities must sum to ~1.0
                if abs(yes_price + no_price - 1.0) > 0.05:
                    continue

                results.append(PolyMarket(
                    condition_id = m.get("conditionId", ""),
                    question     = m.get("question", ""),
                    yes_price    = yes_price,
                    no_price     = no_price,
                    volume_24h   = float(m.get("volume24hr", 0) or 0),
                    active       = bool(m.get("active", True)),
                ))
            except (ValueError, TypeError, KeyError):
                continue

        return results


# ── Module-level singleton ─────────────────────────────────────────────────────
polymarket = PolymarketFeed()
