"""
ep_btc.py — BTC mean-reversion signal generation for EdgePulse Intel node.

Data sources (in priority order):
  Polygon.io Personal plan (optional) — 5-min BTC-USD OHLC (last N candles)
  Coinbase Exchange public REST API   — 5-min BTC-USD OHLC, no auth required
  Coinbase public REST API            — real-time spot price (cross-check / fallback)

If POLYGON_API_KEY is not set, Coinbase Exchange candles are used automatically.
No configuration needed — BTC indicators work out of the box.

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
CANDLE_COUNT   = int(os.getenv("BTC_CANDLE_COUNT",     "100"))  # 100 × 5m = 8.3 h of data
# Volume spike filter: skip entry if latest candle volume exceeds this multiple
# of the 20-candle average.  High volume at the band = breakdown, not exhaustion.
VOL_SPIKE_MULT = float(os.getenv("BTC_VOL_SPIKE_MULT", "1.5"))
# Trend filter: skip entry if 20-SMA has drifted this far below/above 50-SMA.
# Catches sustained trends where mean reversion is unlikely.  0 = disabled.
TREND_FILTER_THRESH = float(os.getenv("BTC_TREND_THRESH", "0.015"))  # 1.5% deviation
TREND_SMA_PERIOD    = int(os.getenv("BTC_TREND_SMA",      "50"))
COINBASE_TAKER_FEE  = float(os.getenv("COINBASE_TAKER_FEE", "0.006"))   # 0.6% default (low-volume taker)


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
        }
        headers = {"Authorization": f"Bearer {self._key}"}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
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


class CoinbaseOHLCClient:
    """
    Async Coinbase Exchange public REST client for BTC-USD OHLC candles.

    Uses the free, unauthenticated Coinbase Exchange (Pro) candles endpoint.
    No API key required.  Granularity options: 60, 300, 900, 3600, 21600, 86400 seconds.
    Returns up to 300 candles per request, newest-first.
    """

    BASE = "https://api.exchange.coinbase.com"

    async def get_candles(
        self,
        multiplier: int = CANDLE_MINUTES,
        count:      int = CANDLE_COUNT,
    ) -> List[BTCCandle]:
        """
        Fetch the most recent `count` BTC-USD candles.
        `multiplier` is candle width in minutes; converted to seconds for the API.
        """
        granularity = multiplier * 60
        end_ts   = int(time.time())
        start_ts = end_ts - (count + 5) * granularity   # request a few extra

        url = f"{self.BASE}/products/BTC-USD/candles"
        params = {
            "granularity": granularity,
            "start":       str(start_ts),
            "end":         str(end_ts),
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        # Response: [[time, low, high, open, close, volume], ...] newest-first
        if not isinstance(data, list):
            log.error("CoinbaseOHLCClient: unexpected response type %s — %s",
                      type(data).__name__, str(data)[:120])
            return []
        candles = []
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            t, low, high, open_, close, volume = row[0], row[1], row[2], row[3], row[4], row[5]
            candles.append(BTCCandle(
                t=int(t) * 1000, o=float(open_), h=float(high),
                l=float(low),    c=float(close), v=float(volume), vw=float(close),
            ))

        # Sort oldest-first, return most recent `count`
        candles.sort(key=lambda c: c.t)
        return candles[-count:]


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


class BinanceOHLCClient:
    """
    Async Binance public REST client for BTC-USDT OHLC candles.

    Free, no API key required.  Used as emergency fallback when both
    Polygon and Coinbase Exchange candle endpoints fail.
    Note: returns BTC-USDT (Tether) not BTC-USD; price difference is negligible.
    """

    BASE = "https://api.binance.com"
    _INTERVAL_MAP = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 60: "1h"}

    async def get_candles(
        self,
        multiplier: int = CANDLE_MINUTES,
        count:      int = CANDLE_COUNT,
    ) -> List[BTCCandle]:
        """Fetch BTC-USDT klines from Binance. Returns BTCCandle list oldest-first."""
        interval = self._INTERVAL_MAP.get(multiplier, "5m")
        url = f"{self.BASE}/api/v3/klines"
        params = {"symbol": "BTCUSDT", "interval": interval, "limit": min(count, 1000)}

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        candles = []
        for row in data:
            # Binance: [open_time_ms, open, high, low, close, volume, ...]
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            candles.append(BTCCandle(
                t=int(row[0]),       # open time ms
                o=float(row[1]),
                h=float(row[2]),
                l=float(row[3]),
                c=float(row[4]),
                v=float(row[5]),
                vw=float(row[4]),    # simplified: use close as VWAP
            ))
        return candles[-count:]

    async def get_spot(self) -> Optional[float]:
        """Returns current BTC-USDT price from Binance, or None on failure."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self.BASE}/api/v3/ticker/price",
                    params={"symbol": "BTCUSDT"},
                )
                resp.raise_for_status()
                return float(resp.json()["price"])
        except Exception as exc:
            log.debug("Binance spot fetch failed: %s", exc)
        return None


# ── Sentiment data ────────────────────────────────────────────────────────────

async def _fetch_fear_greed() -> Optional[dict]:
    """
    Fetch Crypto Fear & Greed Index from alternative.me (free, no key).
    Returns {"value": int 0-100, "classification": str} or None on failure.
      0-24  = Extreme Fear   (mean-reversion LONG setups are higher conviction)
      25-49 = Fear
      50-74 = Greed
      75-100 = Extreme Greed (LONG setups are lower conviction — "buy the dip" is crowded)
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.alternative.me/fng/?limit=1")
            resp.raise_for_status()
            data = resp.json()["data"][0]
            return {
                "value":          int(data["value"]),
                "classification": data["value_classification"],
            }
    except Exception as exc:
        log.debug("Fear & Greed fetch failed: %s", exc)
    return None


async def _fetch_btc_funding_rate() -> Optional[float]:
    """
    Fetch BTC perpetual swap funding rate from OKX (free, no key).
    Positive = longs paying shorts (crowded long, caution for LONG entries).
    Negative = shorts paying longs (crowded short, LONG entries more contrarian).
    Typical range: -0.03% to +0.03% per 8h funding period.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
            )
            resp.raise_for_status()
            data = resp.json()["data"][0]
            return float(data["fundingRate"])
    except Exception as exc:
        log.debug("OKX funding rate fetch failed: %s", exc)
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
    """Rolling z-score of the latest close vs the trailing window.

    With the default period=20 and CANDLE_MINUTES=5, the window is 100 min
    (not 60). The z_thresh calibration (1.8) is tied to this 100-min window;
    changing `period` here without recalibrating the threshold will silently
    shift signal frequency.
    """
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
        polygon_api_key: str = "",
        rsi_oversold:    float = RSI_OVERSOLD,
        rsi_overbought:  float = RSI_OVERBOUGHT,
        z_threshold:     float = Z_THRESHOLD,
        rsi_period:      int   = RSI_PERIOD,
        bb_period:       int   = BB_PERIOD,
        bb_std_mult:     float = BB_STD_MULT,
        candle_minutes:  int   = CANDLE_MINUTES,
        candle_count:    int   = CANDLE_COUNT,
        vol_spike_mult:      float = VOL_SPIKE_MULT,
        trend_filter_thresh: float = TREND_FILTER_THRESH,
        trend_sma_period:    int   = TREND_SMA_PERIOD,
        source_node:         str   = "",
        ttl_ms:              int   = SIGNAL_TTL,
    ):
        self._polygon      = PolygonBTCClient(polygon_api_key) if polygon_api_key else None
        self._cb_ohlc      = CoinbaseOHLCClient()
        self._coinbase     = CoinbaseClient()
        self._binance      = BinanceOHLCClient()
        self.rsi_os        = rsi_oversold
        self.rsi_ob        = rsi_overbought
        self.z_thresh      = z_threshold
        self.rsi_period    = rsi_period
        self.bb_period     = bb_period
        self.bb_std_mult   = bb_std_mult
        self.candle_min    = candle_minutes
        self.candle_cnt    = candle_count
        self.vol_spike_mult      = vol_spike_mult
        self.trend_filter_thresh = trend_filter_thresh
        self.trend_sma_period    = trend_sma_period
        self.source_node         = source_node
        self.ttl_ms        = ttl_ms

        # Last computed indicators — exposed for metrics / LLM context
        self.last_spot:      Optional[float] = None
        self.last_rsi:       Optional[float] = None
        self.last_z:         Optional[float] = None
        self.last_bb_lower:  Optional[float] = None
        self.last_bb_mid:    Optional[float] = None
        self.last_bb_upper:  Optional[float] = None
        self.last_vol_ratio: Optional[float] = None   # latest_vol / vol_ma

    def _vol_regime(self, closes: List[float]) -> str:
        """Classify vol regime: 'low', 'normal', 'high', 'extreme'.
        Uses ratio of 10-bar realized vol to 50-bar realized vol.
        Returns 'insufficient_data' if not enough bars."""
        if len(closes) < 50:
            return "insufficient_data"

        def _rvol(n):
            w = closes[-n:]
            rets = [w[i]/w[i-1]-1 for i in range(1, len(w))]
            if not rets:
                return 0.0
            mean = sum(rets) / len(rets)
            return (sum((r - mean)**2 for r in rets) / len(rets)) ** 0.5

        rv10 = _rvol(10)
        rv50 = _rvol(50)
        if rv50 == 0:
            return "insufficient_data"
        ratio = rv10 / rv50
        if ratio < 0.7:
            return "low"
        elif ratio < 1.4:
            return "normal"
        elif ratio < 2.5:
            return "high"
        else:
            return "extreme"

    async def generate(self) -> List:
        """
        Fetch BTC data, compute indicators, and return 0 or 1 SignalMessages.
        Never raises — returns [] on any error.
        """
        from ep_schema import SignalMessage

        signals: List[SignalMessage] = []

        # Parallel fetch: candles + spot + sentiment (all independent)
        candle_source = (
            self._polygon.get_candles(self.candle_min, "minute", self.candle_cnt)
            if self._polygon
            else self._cb_ohlc.get_candles(self.candle_min, self.candle_cnt)
        )
        try:
            candles_result, spot_result, fg_result, fr_result = await asyncio.gather(
                candle_source,
                self._coinbase.get_spot(),
                _fetch_fear_greed(),
                _fetch_btc_funding_rate(),
                return_exceptions=True,
            )
        except Exception as exc:
            log.error("BTC gather failed: %s", exc)
            return signals

        fear_greed   = fg_result   if not isinstance(fg_result,   Exception) else None
        funding_rate = fr_result   if not isinstance(fr_result,   Exception) else None
        if fear_greed:
            log.debug("Fear & Greed: %d (%s)", fear_greed["value"], fear_greed["classification"])
        if funding_rate is not None:
            log.debug("BTC funding rate: %.5f%%", funding_rate * 100)

        if isinstance(candles_result, Exception):
            primary_name = "Polygon" if self._polygon else "Coinbase OHLC"
            log.warning("%s candle fetch failed (%s) — trying Binance backup",
                        primary_name, candles_result)
            try:
                candles_result = await self._binance.get_candles(
                    self.candle_min, self.candle_cnt
                )
                log.info("BTC candles: using Binance fallback (%d candles)", len(candles_result))
            except Exception as binance_exc:
                log.error("All candle sources failed: %s=%s, Binance=%s",
                          primary_name, candles_result, binance_exc)
                # Still update last_spot so ep:prices stays current
                if isinstance(spot_result, float) and spot_result > 0:
                    self.last_spot = spot_result
                return signals

        # Spot price: prefer Coinbase; fall back to Binance if Coinbase fails
        if not isinstance(spot_result, float) or spot_result <= 0:
            spot_result = await self._binance.get_spot()
            if spot_result:
                log.debug("BTC spot: using Binance fallback %.2f", spot_result)

        candles: List[BTCCandle] = candles_result
        min_data = max(self.rsi_period + 1, self.bb_period)
        if len(candles) < min_data:
            log.debug("Insufficient BTC candles: %d (need %d)", len(candles), min_data)
            return signals

        closes     = [c.c for c in candles]
        vol_regime = self._vol_regime(closes)
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

        # ── Volume spike filter ───────────────────────────────────────────────
        # A dip below the lower band on high volume is a breakdown, not
        # exhaustion.  Compute the 20-candle volume MA and flag if the latest
        # candle is a spike.  We skip entry on spikes; the ratio is stored for
        # external monitoring.
        volumes   = [c.v for c in candles if c.v > 0]
        vol_ma    = sum(volumes[-self.bb_period:]) / self.bb_period if len(volumes) >= self.bb_period else 0.0
        last_vol  = candles[-1].v
        vol_ratio = (last_vol / vol_ma) if vol_ma > 0 else 1.0
        self.last_vol_ratio = vol_ratio
        _vol_spike = vol_ma > 0 and vol_ratio > self.vol_spike_mult

        # ── Trend filter ──────────────────────────────────────────────────────
        # Compare 20-SMA (mid_bb) to 50-SMA.  When the short-term average has
        # drifted significantly below/above the medium-term average the market
        # is trending, not oscillating — mean reversion entries are lower quality.
        _in_downtrend = _in_uptrend = False
        if self.trend_filter_thresh > 0 and len(closes) >= self.trend_sma_period:
            sma_50 = sum(closes[-self.trend_sma_period:]) / self.trend_sma_period
            if sma_50 > 0:
                trend_dev = (mid_bb - sma_50) / sma_50   # negative = short-term below medium
                _in_downtrend = trend_dev < -self.trend_filter_thresh
                _in_uptrend   = trend_dev >  self.trend_filter_thresh
        else:
            trend_dev = 0.0
            sma_50    = 0.0

        log.debug(
            "BTC indicators: spot=%.2f  RSI=%.1f  z=%.2f  "
            "BB=[%.2f / %.2f / %.2f]  vol_ratio=%.2f%s  trend_dev=%.3f%s  vol_regime=%s",
            spot_price, rsi, z, lower_bb, mid_bb, upper_bb,
            vol_ratio, " [SPIKE]" if _vol_spike else "",
            trend_dev,
            " [DOWNTREND]" if _in_downtrend else " [UPTREND]" if _in_uptrend else "",
            vol_regime,
        )

        # ── Sentiment adjustments ─────────────────────────────────────────────
        # Fear & Greed:  low = oversold sentiment → LONG conviction up
        #                high = crowded "buy the dip" → LONG conviction down
        # Funding rate:  negative = shorts crowded → mean-reversion LONG stronger
        #                positive = longs crowded   → mean-reversion LONG weaker
        fg_value = fear_greed["value"] if fear_greed else 50   # neutral default

        def _sentiment_conf_adj(side: str) -> float:
            adj = 0.0
            if side == "buy":
                if fg_value <= 20:    adj += 0.10   # extreme fear: high conviction
                elif fg_value <= 35:  adj += 0.05
                elif fg_value >= 80:  adj -= 0.12   # extreme greed: skip or discount
                elif fg_value >= 65:  adj -= 0.06
                if funding_rate is not None:
                    if funding_rate < -0.0005:   adj += 0.06   # shorts crowded
                    elif funding_rate > 0.0010:  adj -= 0.08   # longs crowded
            else:  # sell
                if fg_value >= 80:    adj += 0.10
                elif fg_value >= 65:  adj += 0.05
                elif fg_value <= 20:  adj -= 0.12
                elif fg_value <= 35:  adj -= 0.06
                if funding_rate is not None:
                    if funding_rate > 0.0010:    adj += 0.06
                    elif funding_rate < -0.0005: adj -= 0.08
            return adj

        def _should_skip_on_sentiment(side: str) -> bool:
            """Skip signal if sentiment strongly contradicts direction."""
            if side == "buy"  and fg_value >= 75 and (funding_rate or 0) > 0.0015:
                log.info("BTC LONG skipped: extreme greed (F&G=%d) + crowded longs (fr=%.5f)",
                         fg_value, funding_rate or 0)
                return True
            if side == "sell" and fg_value <= 25 and (funding_rate or 0) < -0.0015:
                log.info("BTC SHORT skipped: extreme fear (F&G=%d) + crowded shorts (fr=%.5f)",
                         fg_value, funding_rate or 0)
                return True
            return False

        # ── LONG entry ────────────────────────────────────────────────────────
        if rsi < self.rsi_os and spot_price < lower_bb and z < -self.z_thresh:
            if _vol_spike:
                log.info(
                    "BTC LONG skipped: volume spike (vol_ratio=%.2f > %.2f) — "
                    "likely breakdown, not exhaustion",
                    vol_ratio, self.vol_spike_mult,
                )
            elif _in_downtrend:
                log.info(
                    "BTC LONG skipped: sustained downtrend (20-SMA %.2f vs 50-SMA %.2f, "
                    "dev=%.3f < -%.3f threshold)",
                    mid_bb, sma_50, trend_dev, self.trend_filter_thresh,
                )
            elif not _should_skip_on_sentiment("buy"):
                edge       = (mid_bb - spot_price) / spot_price
                conf_adj   = _sentiment_conf_adj("buy")
                # Vol regime: extreme vol = breakdown risk, scale confidence down
                # insufficient_data penalized to 0.5 — a sparse price buffer
                # produces noisy z-scores; don't let them fire at full confidence.
                _vol_mult = {"low": 1.10, "normal": 1.0, "high": 0.85, "extreme": 0.40, "insufficient_data": 0.5}.get(vol_regime, 1.0)
                # Floor applied AFTER vol_mult so extreme-vol regime (×0.40) + a
                # strongly-opposed sentiment adj can't push confidence below 0.10.
                confidence = max(0.10, min(0.99, (abs(z) / 3.0 + conf_adj) * _vol_mult))
                fee_adj_edge = max(0.0, edge - 2 * COINBASE_TAKER_FEE)
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
                    fee_adjusted_edge = fee_adj_edge,
                    confidence        = confidence,
                    suggested_size    = 1,
                    btc_price         = spot_price,
                    btc_z_score       = z,
                    btc_lookback_m    = self.candle_min * self.candle_cnt,
                    ttl_ms            = self.ttl_ms,
                )
                if fee_adj_edge <= 0:
                    log.warning("BTC buy edge=%.4f below round-trip fee (%.1f%%) — skipping",
                                edge, 2 * COINBASE_TAKER_FEE * 100)
                elif edge > 0.15:
                    log.warning("BTC buy edge=%.4f exceeds 15%% — skipping (likely data error)", edge)
                else:
                    signals.append(sig)
                log.info(
                    "BTC LONG  signal: spot=%.2f  z=%.2f  RSI=%.1f  "
                    "edge=%.3f  conf=%.2f  F&G=%d  fr=%s",
                    spot_price, z, rsi, edge, confidence, fg_value,
                    f"{funding_rate:.5f}" if funding_rate is not None else "n/a",
                )

        # ── SHORT entry ───────────────────────────────────────────────────────
        elif rsi > self.rsi_ob and spot_price > upper_bb and z > self.z_thresh:
            if _vol_spike:
                log.info(
                    "BTC SHORT skipped: volume spike (vol_ratio=%.2f > %.2f) — "
                    "likely breakout, not exhaustion",
                    vol_ratio, self.vol_spike_mult,
                )
            elif _in_uptrend:
                log.info(
                    "BTC SHORT skipped: sustained uptrend (20-SMA %.2f vs 50-SMA %.2f, "
                    "dev=%.3f > %.3f threshold)",
                    mid_bb, sma_50, trend_dev, self.trend_filter_thresh,
                )
            elif not _should_skip_on_sentiment("sell"):
                edge       = (spot_price - mid_bb) / spot_price
                conf_adj   = _sentiment_conf_adj("sell")
                # Vol regime: extreme vol = breakdown risk, scale confidence down
                # insufficient_data penalized to 0.5 — a sparse price buffer
                # produces noisy z-scores; don't let them fire at full confidence.
                _vol_mult = {"low": 1.10, "normal": 1.0, "high": 0.85, "extreme": 0.40, "insufficient_data": 0.5}.get(vol_regime, 1.0)
                # Floor applied AFTER vol_mult so extreme-vol regime (×0.40) + a
                # strongly-opposed sentiment adj can't push confidence below 0.10.
                confidence = max(0.10, min(0.99, (abs(z) / 3.0 + conf_adj) * _vol_mult))
                fee_adj_edge = max(0.0, edge - 2 * COINBASE_TAKER_FEE)
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
                    fee_adjusted_edge = fee_adj_edge,
                    confidence        = confidence,
                    suggested_size    = 1,
                    btc_price         = spot_price,
                    btc_z_score       = z,
                    btc_lookback_m    = self.candle_min * self.candle_cnt,
                    ttl_ms            = self.ttl_ms,
                )
                if fee_adj_edge <= 0:
                    log.warning("BTC sell edge=%.4f below round-trip fee (%.1f%%) — skipping",
                                edge, 2 * COINBASE_TAKER_FEE * 100)
                elif edge > 0.15:
                    log.warning("BTC sell edge=%.4f exceeds 15%% — skipping (likely data error)", edge)
                else:
                    signals.append(sig)
                log.info(
                    "BTC SHORT signal: spot=%.2f  z=%.2f  RSI=%.1f  "
                    "edge=%.3f  conf=%.2f  F&G=%d  fr=%s",
                    spot_price, z, rsi, edge, confidence, fg_value,
                    f"{funding_rate:.5f}" if funding_rate is not None else "n/a",
                )

        return signals
