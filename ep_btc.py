"""
ep_btc.py — BTC mean-reversion signal generation for EdgePulse Intel node.

Data sources:
  Polygon.io Personal plan ($200/mo) — 5-min BTC-USD OHLC (last N candles)
  Coinbase public REST API           — real-time spot price (cross-check / fallback)

Signal logic (all three conditions required):
  LONG  entry: RSI-14 < RSI_OVERSOLD  AND  price < lower_bb  AND  z < -Z_THRESHOLD
  SHORT entry: RSI-14 > RSI_OVERBOUGHT AND  price > upper_bb  AND  z >  Z_THRESHOLD

Sizing is handled by UnifiedRiskEngine._size_btc() in ep_risk.py.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import httpx

from ep_config import log, SIGNAL_TTL


# ── Env-based defaults (overridden at runtime via Redis ep:config) ────────────
RSI_PERIOD     = int(os.getenv("BTC_RSI_PERIOD",       "14"))
BB_PERIOD      = int(os.getenv("BTC_BB_PERIOD",        "20"))
BB_STD_MULT    = float(os.getenv("BTC_BB_STD",         "2.0"))
RSI_OVERSOLD   = float(os.getenv("BTC_RSI_OVERSOLD",   "35"))
RSI_OVERBOUGHT = float(os.getenv("BTC_RSI_OVERBOUGHT", "65"))
Z_THRESHOLD    = float(os.getenv("BTC_Z_THRESHOLD",    "1.5"))
CANDLE_MINUTES = int(os.getenv("BTC_CANDLE_MIN",       "5"))
CANDLE_COUNT   = int(os.getenv("BTC_CANDLE_COUNT",     "60"))   # 60 × 5m = 5 h of data


@dataclass
class BTCCandle:
    t:  int    # open time (unix ms)
    o:  float  # open
    h:  float  # high
    l:  float  # low
    c:  float  # close
    v:  float  # volume
    vw: float  # VWAP


# ── Data clients ─────────────────────────────────────────────────────────────

class PolygonBTCClient:
    """Async Polygon.io client for BTC-USD (X:BTCUSD) OHLC candles."""

    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str, timeout: float = 10.0):
        self._key     = api_key
        self._timeout = timeout

    async def get_candles(
        self,
        multiplier: int = CANDLE_MINUTES,
        timespan:   str = "minute",
        count:      int = CANDLE_COUNT,
    ) -> List[BTCCandle]:
        """
        Fetch the most recent `count` BTC-USD candles from Polygon.io.
        Returns a list sorted oldest-first.
        """
        import datetime
        now_ms   = int(time.time() * 1000)
        # Request extra candles to absorb any gaps
        from_ms  = now_ms - (count + 10) * multiplier * 60 * 1000

        from_dt = datetime.datetime.utcfromtimestamp(from_ms / 1000).strftime("%Y-%m-%d")
        to_dt   = datetime.datetime.utcfromtimestamp(now_ms  / 1000).strftime("%Y-%m-%d")

        url = (
            f"{self.BASE}/v2/aggs/ticker/X:BTCUSD/range"
            f"/{multiplier}/{timespan}/{from_dt}/{to_dt}"
        )
        params = {
            "adjusted": "true",
            "sort":     "asc",
            "limit":    str(count + 10),
            "apiKey":   self._key,
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        candles = [
            BTCCandle(
                t=r["t"], o=r["o"], h=r["h"],
                l=r["l"], c=r["c"], v=r.get("v", 0), vw=r.get("vw", r["c"]),
            )
            for r in results
        ]
        return candles[-count:]   # trim to requested count, newest N


class CoinbaseClient:
    """Async Coinbase public REST client for BTC-USD spot price."""

    BASE = "https://api.coinbase.com"

    async def get_spot(self) -> Optional[float]:
        """Returns current BTC-USD spot price in USD, or None on failure."""
        url = f"{self.BASE}/v2/prices/BTC-USD/spot"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            return float(data["data"]["amount"])
        except Exception as exc:
            log.warning("Coinbase spot fetch failed: %s", exc)
            return None


# ── Technical indicators ──────────────────────────────────────────────────────

def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """
    Wilder's RSI on a list of closing prices.
    Returns None if fewer than (period + 1) data points.
    """
    if len(closes) < period + 1:
        return None
    gains:  List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period

    if avg_l == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _bollinger(
    closes: List[float], period: int = 20, std_mult: float = 2.0,
) -> Optional[Tuple[float, float, float]]:
    """
    Bollinger Bands over the trailing `period` closes.
    Returns (lower, middle, upper) or None if insufficient data.
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid    = sum(window) / period
    std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return mid - std_mult * std, mid, mid + std_mult * std


def _z_score(closes: List[float], period: int = 20) -> Optional[float]:
    """Rolling z-score of the latest close vs the trailing window."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid    = sum(window) / period
    std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    if std == 0.0:
        return 0.0
    return (closes[-1] - mid) / std


# ── Strategy ──────────────────────────────────────────────────────────────────

class BTCMeanReversionStrategy:
    """
    Generates BTC mean-reversion SignalMessages for the EdgePulse Redis bus.

    Parameters are read from env on construction. The LLM agent can
    update them at runtime by writing to ep:config in Redis; ep_intel.py
    applies those overrides before each call to generate().
    """

    def __init__(
        self,
        polygon_api_key: str,
        rsi_oversold:    float = RSI_OVERSOLD,
        rsi_overbought:  float = RSI_OVERBOUGHT,
        z_threshold:     float = Z_THRESHOLD,
        rsi_period:      int   = RSI_PERIOD,
        bb_period:       int   = BB_PERIOD,
        bb_std_mult:     float = BB_STD_MULT,
        candle_minutes:  int   = CANDLE_MINUTES,
        candle_count:    int   = CANDLE_COUNT,
        source_node:     str   = "",
        ttl_ms:          int   = SIGNAL_TTL,
    ):
        self._polygon      = PolygonBTCClient(polygon_api_key)
        self._coinbase     = CoinbaseClient()
        self.rsi_os        = rsi_oversold
        self.rsi_ob        = rsi_overbought
        self.z_thresh      = z_threshold
        self.rsi_period    = rsi_period
        self.bb_period     = bb_period
        self.bb_std_mult   = bb_std_mult
        self.candle_min    = candle_minutes
        self.candle_cnt    = candle_count
        self.source_node   = source_node
        self.ttl_ms        = ttl_ms

        # Last computed indicators — exposed for metrics / LLM context
        self.last_spot:     Optional[float] = None
        self.last_rsi:      Optional[float] = None
        self.last_z:        Optional[float] = None
        self.last_bb_lower: Optional[float] = None
        self.last_bb_mid:   Optional[float] = None
        self.last_bb_upper: Optional[float] = None

    async def generate(self) -> List:
        """
        Fetch BTC data, compute indicators, and return 0 or 1 SignalMessages.
        Never raises — returns [] on any error.
        """
        from ep_schema import SignalMessage

        signals: List[SignalMessage] = []

        # Parallel fetch: Polygon candles + Coinbase spot
        try:
            candles_result, spot_result = await asyncio.gather(
                self._polygon.get_candles(self.candle_min, "minute", self.candle_cnt),
                self._coinbase.get_spot(),
                return_exceptions=True,
            )
        except Exception as exc:
            log.error("BTC gather failed: %s", exc)
            return signals

        if isinstance(candles_result, Exception):
            log.error("Polygon fetch error: %s", candles_result)
            return signals

        candles: List[BTCCandle] = candles_result
        min_data = max(self.rsi_period + 1, self.bb_period)
        if len(candles) < min_data:
            log.debug("Insufficient BTC candles: %d (need %d)", len(candles), min_data)
            return signals

        closes     = [c.c for c in candles]
        spot_price = (
            spot_result
            if isinstance(spot_result, float) and spot_result and spot_result > 0
            else closes[-1]
        )

        rsi = _rsi(closes, self.rsi_period)
        bb  = _bollinger(closes, self.bb_period, self.bb_std_mult)
        z   = _z_score(closes, self.bb_period)

        # Store for external access (metrics, LLM context)
        self.last_spot = spot_price
        self.last_rsi  = rsi
        self.last_z    = z
        if bb:
            self.last_bb_lower, self.last_bb_mid, self.last_bb_upper = bb

        if rsi is None or bb is None or z is None:
            return signals

        lower_bb, mid_bb, upper_bb = bb

        log.debug(
            "BTC indicators: spot=%.2f  RSI=%.1f  z=%.2f  "
            "BB=[%.2f / %.2f / %.2f]",
            spot_price, rsi, z, lower_bb, mid_bb, upper_bb,
        )

        # ── LONG entry ────────────────────────────────────────────────────────
        if rsi < self.rsi_os and spot_price < lower_bb and z < -self.z_thresh:
            edge       = (mid_bb - spot_price) / spot_price
            confidence = min(1.0, abs(z) / 3.0)
            sig = SignalMessage(
                source_node       = self.source_node,
                asset_class       = "btc_spot",
                strategy          = "btc_mr",
                category          = "mean_reversion",
                ticker            = "BTC-USD",
                exchange          = "coinbase",
                side              = "buy",
                market_price      = spot_price,
                fair_value        = mid_bb,
                edge              = edge,
                fee_adjusted_edge = max(0.0, edge - 0.001),   # ~0.1% Coinbase fee
                confidence        = confidence,
                suggested_size    = 1,
                btc_price         = spot_price,
                btc_z_score       = z,
                btc_lookback_m    = self.candle_min * self.candle_cnt,
                ttl_ms            = self.ttl_ms,
            )
            signals.append(sig)
            log.info(
                "BTC LONG  signal: spot=%.2f  z=%.2f  RSI=%.1f  "
                "edge=%.3f  conf=%.2f",
                spot_price, z, rsi, edge, confidence,
            )

        # ── SHORT entry ───────────────────────────────────────────────────────
        elif rsi > self.rsi_ob and spot_price > upper_bb and z > self.z_thresh:
            edge       = (spot_price - mid_bb) / spot_price
            confidence = min(1.0, abs(z) / 3.0)
            sig = SignalMessage(
                source_node       = self.source_node,
                asset_class       = "btc_spot",
                strategy          = "btc_mr",
                category          = "mean_reversion",
                ticker            = "BTC-USD",
                exchange          = "coinbase",
                side              = "sell",
                market_price      = spot_price,
                fair_value        = mid_bb,
                edge              = edge,
                fee_adjusted_edge = max(0.0, edge - 0.001),
                confidence        = confidence,
                suggested_size    = 1,
                btc_price         = spot_price,
                btc_z_score       = z,
                btc_lookback_m    = self.candle_min * self.candle_cnt,
                ttl_ms            = self.ttl_ms,
            )
            signals.append(sig)
            log.info(
                "BTC SHORT signal: spot=%.2f  z=%.2f  RSI=%.1f  "
                "edge=%.3f  conf=%.2f",
                spot_price, z, rsi, edge, confidence,
            )

        return signals
