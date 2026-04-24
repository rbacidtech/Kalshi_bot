"""
ep_intel.py — Intel main loop (runs on DO Droplet NYC3).

Responsibilities:
  1. Maintain WebSocket price feed → BotState
  2. Every POLL_INTERVAL seconds:
     a. Publish price snapshot to Redis  (Exec uses this for exit checks)
     b. Call fetch_signals_async() directly  (no asyncio.run() wrapper)
     c. Filter out tickers already held in Redis positions
     d. Publish new SignalMessages to ep:signals
     e. Drain execution reports → update stats / dashboard
"""

import asyncio
import json
import math
import os
import statistics
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from ep_config import cfg, NODE_ID, REDIS_URL, EP_PRICES, log, sd_notify
from kalshi_bot.auth      import KalshiAuth, NoAuth
from kalshi_bot.client    import KalshiClient
from kalshi_bot.state     import BotState
from kalshi_bot.websocket import KalshiWebSocket
from kalshi_bot.strategy  import fetch_signals_async, scan_all_markets, Signal, fetch_treasury_2y_yield
from kalshi_bot.logger    import setup_logging, DailySummary
from ep_schema import PriceSnapshot, SignalMessage
from ep_bus import RedisBus
from ep_adapters import kalshi_signal_to_message
from ep_btc import BTCMeanReversionStrategy
from ep_metrics import metrics
from ep_behavioral import record_volume, is_late_money_spike, recency_bias_adj
from ep_polymarket import polymarket
from kalshi_bot.models.fomc import inject_kalshi_prices as _fomc_inject_prices
from ep_health import health as _src_health
from ep_coinbase import CoinbaseTradeClient
from ep_pg_audit import init_audit_writer, stop_audit_writer, audit as _intel_audit
from ep_telegram import telegram as _telegram
from ep_resolution_db import get_performance_summary
from ep_fed_sentiment import get_fed_sentiment
from kalshi_bot.alerts import AlertManager
import os as _os


async def _fetch_fed_rate(fred_api_key: str, fallback: float) -> float:
    """
    Fetch the current Fed Funds upper target rate from FRED (DFEDTARU).
    Daily series — updated same day as each FOMC decision.
    Returns fallback on any error.
    """
    if not fred_api_key:
        return fallback
    try:
        url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DFEDTARU&api_key={fred_api_key}"
            "&file_type=json&sort_order=desc&limit=1"
        )
        async with __import__("httpx").AsyncClient(timeout=8.0) as http:
            resp = await http.get(url)
        if resp.status_code == 200:
            obs = [o for o in resp.json().get("observations", []) if o.get("value") != "."]
            if obs:
                rate = float(obs[0]["value"])
                log.info("FRED DFEDTARU: current fed funds upper target = %.2f%%", rate)
                return rate
    except Exception as exc:
        log.warning("FRED rate fetch failed: %s — using fallback %.2f%%", exc, fallback)
    return fallback


async def _fetch_vix() -> Optional[float]:
    """Fetch CBOE VIX from FRED (VIXCLS). Returns latest value or None.

    VIX is a key macro uncertainty indicator — elevated VIX correlates with
    heightened Fed policy uncertainty and wider Kalshi bid/ask spreads.
    Published daily; cached implicitly via the once-per-day refresh cadence.
    """
    import httpx as _httpx
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=VIXCLS&api_key={fred_key}&sort_order=desc&limit=5&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    vix = float(o["value"])
                    log.info("FRED VIXCLS: VIX = %.2f", vix)
                    return vix
    except Exception as exc:
        log.debug("VIX fetch failed: %s", exc)
    return None


async def _fetch_dgs10() -> Optional[float]:
    """Fetch the 10-year Treasury yield from FRED (DGS10). Returns value or None.

    DGS10 - DGS2 = yield curve spread.  Negative spread (inverted curve) signals
    recession risk / rate cuts; steep positive spread signals growth/hike expectations.
    """
    import httpx as _httpx
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=DGS10&api_key={fred_key}&sort_order=desc&limit=5&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    dgs10 = float(o["value"])
                    log.info("FRED DGS10: 10Y Treasury yield = %.3f%%", dgs10)
                    return dgs10
    except Exception as exc:
        log.debug("DGS10 fetch failed: %s", exc)
    return None


async def _fetch_dgs5() -> Optional[float]:
    """Fetch 5-year Treasury yield from FRED (DGS5)."""
    import httpx as _httpx
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=DGS5&api_key={fred_key}&sort_order=desc&limit=5&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    val = float(o["value"])
                    log.info("FRED DGS5: 5Y Treasury yield = %.3f%%", val)
                    return val
    except Exception as exc:
        log.debug("DGS5 fetch failed: %s", exc)
    return None


async def _fetch_dgs30() -> Optional[float]:
    """Fetch 30-year Treasury yield from FRED (DGS30)."""
    import httpx as _httpx
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=DGS30&api_key={fred_key}&sort_order=desc&limit=5&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    val = float(o["value"])
                    log.info("FRED DGS30: 30Y Treasury yield = %.3f%%", val)
                    return val
    except Exception as exc:
        log.debug("DGS30 fetch failed: %s", exc)
    return None


async def _fetch_move_index() -> Optional[float]:
    """Fetch ICE BofA MOVE Index (bond market volatility) from FRED (MOVE).

    The MOVE Index is the bond-market equivalent of VIX — elevated readings
    signal uncertainty about Fed policy and often precede vol spikes in
    rate-sensitive Kalshi markets.
    """
    import httpx as _httpx
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=MOVE&api_key={fred_key}&sort_order=desc&limit=5&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    move = float(o["value"])
                    log.info("FRED MOVE: bond vol index = %.2f", move)
                    return move
    except Exception as exc:
        log.debug("MOVE fetch failed: %s", exc)
    return None


async def _fetch_credit_spread() -> Optional[float]:
    """Fetch HYG/LQD ratio as a credit spread proxy via Yahoo Finance.

    A falling HYG/LQD ratio signals widening credit spreads (risk-off) —
    high-yield bonds underperform investment-grade, often preceding Fed pauses.
    Result is cached for 3600s.
    """
    global _credit_spread_cache
    import httpx as _httpx
    now = time.time()
    if _credit_spread_cache is not None and (now - _credit_spread_cache[1]) < 3600:
        return _credit_spread_cache[0]

    prices: dict = {}
    for ticker in ("HYG", "LQD"):
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            "?interval=1d&range=5d"
        )
        try:
            async with _httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                result = r.json().get("chart", {}).get("result", [])
                if result:
                    closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [c for c in closes if c is not None]
                    if closes:
                        prices[ticker] = closes[-1]
        except Exception as exc:
            log.debug("Yahoo Finance %s fetch failed: %s", ticker, exc)

    if "HYG" in prices and "LQD" in prices and prices["LQD"] > 0:
        ratio = prices["HYG"] / prices["LQD"]
        log.info("Credit spread proxy (HYG/LQD): %.4f", ratio)
        _credit_spread_cache = (ratio, now)
        return ratio
    return None


# ── Module-level FRED cache vars ─────────────────────────────────────────────
_credit_spread_cache: "tuple[float, float] | None" = None  # (value, timestamp)
_last_core_cpi: Optional[float] = None
_last_core_cpi_ts: float = 0.0
_last_pce: Optional[float] = None
_last_pce_ts: float = 0.0
_last_icsa: Optional[float] = None
_last_icsa_ts: float = 0.0
_icsa_4wma: Optional[float] = None
_last_t10y2y: Optional[float] = None
_last_t10y2y_ts: float = 0.0
_last_t5yifr: Optional[float] = None
_last_t5yifr_ts: float = 0.0
_last_unrate: Optional[float] = None
_last_unrate_ts: float = 0.0

# ── Daily summary tracker ────────────────────────────────────────────────────
# Stores the date (YYYY-MM-DD UTC) of the last successfully sent daily summary
# so we send at most once per calendar day even if the 22:00 UTC hour fires
# multiple cycles.
_last_daily_summary_day: str = ""


async def _fetch_core_cpi() -> Optional[float]:
    """Fetch Core CPI (CPILFESL) from FRED — YoY %, the sticky inflation measure the Fed watches most.

    Daily series; cached for 3600s.
    """
    global _last_core_cpi, _last_core_cpi_ts
    import httpx as _httpx
    if _last_core_cpi is not None and (time.time() - _last_core_cpi_ts) < 3600:
        return _last_core_cpi
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    # Fetch 14 months so we can compute current vs 12-months-ago (YoY)
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=CPILFESL&api_key={fred_key}&sort_order=desc&limit=14&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = [o for o in r.json().get("observations", []) if o.get("value", ".") != "."]
            if len(obs) >= 13:
                current   = float(obs[0]["value"])
                year_ago  = float(obs[12]["value"])
                yoy = (current / year_ago - 1.0) * 100.0
                if not (0.0 <= yoy <= 15.0):
                    log.warning("FRED CPILFESL: YoY=%.2f%% out of range — ignored", yoy)
                    return None
                log.info("FRED CPILFESL: Core CPI YoY = %.2f%%", yoy)
                _last_core_cpi    = yoy
                _last_core_cpi_ts = time.time()
                return yoy
    except Exception as exc:
        log.debug("CPILFESL fetch failed: %s", exc)
    return None


async def _fetch_pce() -> Optional[float]:
    """Fetch PCE Price Index (PCEPI) from FRED and compute YoY %.

    Fetches 13 months, computes (latest / year_ago - 1) * 100.
    Sanity check: returns None if computed YoY < 0 or > 15.
    Cached for 3600s.
    """
    global _last_pce, _last_pce_ts
    import httpx as _httpx
    if _last_pce is not None and (time.time() - _last_pce_ts) < 3600:
        return _last_pce
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=PCEPI&api_key={fred_key}&sort_order=desc&limit=13&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = [o for o in r.json().get("observations", []) if o.get("value", ".") != "."]
            if len(obs) >= 13:
                latest   = float(obs[0]["value"])
                year_ago = float(obs[12]["value"])
                pce_yoy  = (latest / year_ago - 1.0) * 100
                if pce_yoy < 0 or pce_yoy > 15:
                    log.warning(
                        "FRED PCEPI: PCE YoY sanity check failed (%.2f%%) — returning None",
                        pce_yoy,
                    )
                    return None
                log.info("FRED PCEPI: PCE inflation YoY = %.2f%%", pce_yoy)
                _last_pce = pce_yoy
                _last_pce_ts = time.time()
                return pce_yoy
    except Exception as exc:
        log.debug("PCEPI fetch failed: %s", exc)
    return None


async def _fetch_icsa() -> Optional[float]:
    """Fetch Initial Jobless Claims (ICSA) from FRED — weekly level.

    Fetches 4 weeks; returns latest value and stores 4-week moving average in
    module-level _icsa_4wma.  Sanity check: returns None if < 100_000 or > 1_500_000.
    Cached for 3600s.
    """
    global _last_icsa, _last_icsa_ts, _icsa_4wma
    import httpx as _httpx
    if _last_icsa is not None and (time.time() - _last_icsa_ts) < 3600:
        return _last_icsa
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=ICSA&api_key={fred_key}&sort_order=desc&limit=4&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = [o for o in r.json().get("observations", []) if o.get("value", ".") != "."]
            if not obs:
                return None
            latest = float(obs[0]["value"])
            if latest < 100_000 or latest > 1_500_000:
                log.warning(
                    "FRED ICSA: sanity check failed (%.0f) — returning None", latest
                )
                return None
            # 4-week moving average
            values = [float(o["value"]) for o in obs if o.get("value", ".") != "."]
            if len(values) >= 4:
                _icsa_4wma = sum(values[:4]) / 4.0
            else:
                _icsa_4wma = sum(values) / len(values)
            log.info(
                "FRED ICSA: Initial jobless claims = {:,.0f} (4wma: {:,.0f})".format(
                    latest, _icsa_4wma or latest
                )
            )
            _last_icsa = latest
            _last_icsa_ts = time.time()
            return latest
    except Exception as exc:
        log.debug("ICSA fetch failed: %s", exc)
    return None


async def _fetch_t10y2y() -> Optional[float]:
    """Fetch 10Y-2Y Treasury spread (T10Y2Y) from FRED — canonical yield curve inversion signal.

    Cached for 3600s.
    """
    global _last_t10y2y, _last_t10y2y_ts
    import httpx as _httpx
    if _last_t10y2y is not None and (time.time() - _last_t10y2y_ts) < 3600:
        return _last_t10y2y
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=T10Y2Y&api_key={fred_key}&sort_order=desc&limit=5&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    val = float(o["value"])
                    if val < -0.50:
                        regime_label = "DEEPLY INVERTED (strong recession signal)"
                    elif val < 0:
                        regime_label = "INVERTED"
                    elif val < 0.50:
                        regime_label = "flat"
                    else:
                        regime_label = "normal"
                    log.info(
                        "FRED T10Y2Y: yield curve spread = %.3f%%  [%s]",
                        val, regime_label,
                    )
                    _last_t10y2y = val
                    _last_t10y2y_ts = time.time()
                    return val
    except Exception as exc:
        log.debug("T10Y2Y fetch failed: %s", exc)
    return None


async def _fetch_t5yifr() -> Optional[float]:
    """Fetch 5-Year Forward Inflation Rate (T5YIFR) from FRED — inflation expectations anchor.

    Logs a WARNING if > 2.5% (deanchoring signal).
    Cached for 3600s.
    """
    global _last_t5yifr, _last_t5yifr_ts
    import httpx as _httpx
    if _last_t5yifr is not None and (time.time() - _last_t5yifr_ts) < 3600:
        return _last_t5yifr
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=T5YIFR&api_key={fred_key}&sort_order=desc&limit=5&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    val = float(o["value"])
                    if val > 2.5:
                        log.warning(
                            "Inflation expectations deanchoring (T5YIFR=%.2f%%)", val
                        )
                    log.info("FRED T5YIFR: 5Y5Y inflation forward = %.2f%%", val)
                    _last_t5yifr = val
                    _last_t5yifr_ts = time.time()
                    return val
    except Exception as exc:
        log.debug("T5YIFR fetch failed: %s", exc)
    return None


async def _fetch_unrate() -> Optional[float]:
    """Fetch Unemployment Rate (UNRATE) from FRED — monthly.

    Cached for 3600s.
    """
    global _last_unrate, _last_unrate_ts
    import httpx as _httpx
    if _last_unrate is not None and (time.time() - _last_unrate_ts) < 3600:
        return _last_unrate
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return None
    url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=UNRATE&api_key={fred_key}&sort_order=desc&limit=3&file_type=json"
    )
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            for o in obs:
                if o.get("value", ".") != ".":
                    val = float(o["value"])
                    log.info("FRED UNRATE: unemployment rate = %.1f%%", val)
                    _last_unrate = val
                    _last_unrate_ts = time.time()
                    return val
    except Exception as exc:
        log.debug("UNRATE fetch failed: %s", exc)
    return None


# ── BLS Release Calendar (2026) ───────────────────────────────────────────────
# Format: (month, day, series_type, bls_series_id)
_RELEASE_CALENDAR_2026 = [
    # CPI releases (BLS)
    (1,  15, "CPI", "CUUR0000SA0"),
    (2,  12, "CPI", "CUUR0000SA0"),
    (3,  12, "CPI", "CUUR0000SA0"),
    (4,  10, "CPI", "CUUR0000SA0"),   # upcoming
    (5,  13, "CPI", "CUUR0000SA0"),
    (6,  11, "CPI", "CUUR0000SA0"),
    (7,  15, "CPI", "CUUR0000SA0"),
    (8,  12, "CPI", "CUUR0000SA0"),
    (9,  11, "CPI", "CUUR0000SA0"),
    (10, 13, "CPI", "CUUR0000SA0"),
    (11, 12, "CPI", "CUUR0000SA0"),
    (12, 10, "CPI", "CUUR0000SA0"),
    # NFP releases (BLS) — first Friday of each month
    (1,  10, "NFP", "CES0000000001"),
    (2,   7, "NFP", "CES0000000001"),
    (3,   7, "NFP", "CES0000000001"),
    (4,   4, "NFP", "CES0000000001"),
    (5,   1, "NFP", "CES0000000001"),
    (6,   6, "NFP", "CES0000000001"),
    (7,   4, "NFP", "CES0000000001"),
    (8,   1, "NFP", "CES0000000001"),
    (9,   5, "NFP", "CES0000000001"),
    (10,  3, "NFP", "CES0000000001"),
    (11,  7, "NFP", "CES0000000001"),
    (12,  5, "NFP", "CES0000000001"),
]
_RELEASE_TIME_ET        = (8, 30)   # 8:30 AM Eastern
_RELEASE_WINDOW_MINUTES = 10        # Monitor from 8:28 to 8:40 ET


async def _poll_bls_release(series_id: str, last_period: Optional[str]) -> Optional[dict]:
    """Poll BLS API for a series. Returns new data dict if a new period is available."""
    import httpx as _httpx
    bls_key = os.getenv("BLS_API_KEY", "")
    payload: dict = {
        "seriesid":  [series_id],
        "startyear": str(datetime.now().year - 1),
        "endyear":   str(datetime.now().year),
    }
    if bls_key:
        payload["registrationkey"] = bls_key

    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.bls.gov/publicAPI/v2/timeseries/data/",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            return None
        series_data  = data.get("Results", {}).get("series", [{}])[0]
        observations = series_data.get("data", [])
        if not observations:
            return None
        latest     = observations[0]   # BLS returns newest first
        period_key = f"{latest['year']}-{latest['period']}"
        if period_key == last_period:
            return None   # No new data
        try:
            val = float(latest["value"])
            if val <= 0:
                return None
        except (ValueError, KeyError):
            return None
        return {"period": period_key, "value": val, "series_id": series_id}
    except Exception:
        return None


async def _release_monitor_loop(bus: RedisBus) -> None:
    """Monitor for BLS economic releases.

    On release day, poll BLS API starting at 8:28 AM ET and trigger a forced
    intel cycle when new data is detected.

    Uses BLS public API v2 (no key required for basic queries):
      POST https://api.bls.gov/publicAPI/v2/timeseries/data/
    BLS API key (optional, higher rate limits): BLS_API_KEY env var
    """
    _last_known: dict = {}   # series_id → latest period string

    # Seed health from existing ep:releases data so sources don't show age=None at startup
    try:
        _rel_existing = await bus._r.hgetall("ep:releases")
        if _rel_existing.get("CPI"):
            _src_health.mark_ok("bls_cpi")
        if _rel_existing.get("NFP"):
            _src_health.mark_ok("bls_nfp")
    except Exception:
        pass

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            # Convert to ET (UTC-4 in summer DST, UTC-5 in winter)
            et_offset = -4 if 3 < now_utc.month < 11 else -5
            now_et    = now_utc.replace(tzinfo=None) + timedelta(hours=et_offset)

            # Check if today has a release
            today_releases = [
                r for r in _RELEASE_CALENDAR_2026
                if r[0] == now_et.month and r[1] == now_et.day
            ]

            if today_releases:
                rh, rm           = _RELEASE_TIME_ET
                release_minute   = rh * 60 + rm
                current_minute   = now_et.hour * 60 + now_et.minute

                if release_minute - 2 <= current_minute <= release_minute + _RELEASE_WINDOW_MINUTES:
                    # We're in the release window — poll BLS for new data
                    for _, _, series_type, bls_id in today_releases:
                        new_data = await _poll_bls_release(bls_id, _last_known.get(bls_id))
                        if new_data:
                            _last_known[bls_id] = new_data["period"]
                            log.info(
                                "BLS RELEASE DETECTED: %s  period=%s  value=%s  "
                                "(triggering forced intel cycle)",
                                series_type, new_data["period"], new_data["value"],
                            )
                            # Store in Redis for strategy to use
                            await bus._r.hset("ep:releases", mapping={
                                series_type:              str(new_data["value"]),
                                f"{series_type}_period":  new_data["period"],
                                f"{series_type}_ts":      str(time.time()),
                            })
                            _src_health.mark_ok(f"bls_{series_type.lower()}")
                            # Signal that a forced cycle is needed
                            await bus._r.set("ep:forced_cycle", "1", ex=300)
                    await asyncio.sleep(30)   # Poll every 30s during window
                    continue

            # Not in release window — sleep until next check
            next_release_today = any(
                r[0] == now_et.month and r[1] == now_et.day
                for r in _RELEASE_CALENDAR_2026
            )
            if next_release_today and now_et.hour < 8:
                current_minute      = now_et.hour * 60 + now_et.minute
                seconds_to_window   = max(0, (8 * 60 + 26 - current_minute) * 60)
                await asyncio.sleep(min(seconds_to_window, 900))
            else:
                await asyncio.sleep(900)   # Check every 15 minutes otherwise

        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Release monitor error: %s", exc)
            await asyncio.sleep(60)


# ── Macro refresh + regime classification ─────────────────────────────────────

def _classify_regime(regime: dict) -> str:
    """Return human-readable macro regime label."""
    t10y2y        = regime.get("t10y2y", 0)
    pce           = regime.get("pce_yoy", 0)
    core_cpi      = regime.get("core_cpi_yoy", 0)
    vix           = regime.get("vix", 15)
    baa10y_regime = regime.get("baa10y_regime", "normal")

    # Stressed credit spreads override other indicators — systemic risk signal
    if baa10y_regime == "stressed":
        return "RISK_OFF"
    if t10y2y < -0.50 and pce < 2.5:
        return "EASING_STRONGLY"
    elif t10y2y < 0 or pce < 2.0:
        return "EASING"
    elif pce > 3.0 or core_cpi > 3.5:
        return "TIGHTENING"
    elif vix > 30 or baa10y_regime == "elevated":
        return "RISK_OFF"
    else:
        return "NEUTRAL"


async def _refresh_macro_data(bus: RedisBus) -> None:
    """Fetch all macro indicators concurrently, publish to ep:macro, and update fomc model."""
    # Run all fetches in parallel
    results = await asyncio.gather(
        _fetch_fed_rate(os.getenv("FRED_API_KEY", ""), float(os.getenv("CURRENT_FED_RATE", "4.25"))),
        _fetch_vix(),
        _fetch_dgs10(),
        _fetch_core_cpi(),
        _fetch_pce(),
        _fetch_icsa(),
        _fetch_t10y2y(),
        _fetch_t5yifr(),
        _fetch_unrate(),
        _fetch_move_index(),
        _fetch_credit_spread(),
        _fetch_dgs5(),    # NEW
        _fetch_dgs30(),   # NEW
        return_exceptions=True,
    )
    # Unpack (handle exceptions gracefully)
    (fed_rate, vix, dgs10, core_cpi, pce_yoy, icsa, t10y2y, t5yifr, unrate, move, cs, dgs5, dgs30) = [
        r if not isinstance(r, Exception) else None
        for r in results
    ]

    # Update health for each FRED source based on gather results
    _fred_sources = [
        ("fred_dfedtaru", results[0]),
        ("fred_vix",      results[1]),
        ("fred_dgs10",    results[2]),
        ("fred_core_cpi", results[3]),
        ("fred_pce",      results[4]),
        ("fred_icsa",     results[5]),
        ("fred_t10y2y",   results[6]),
        ("fred_t5yifr",   results[7]),
        ("fred_unrate",   results[8]),
        ("spd",           results[10]),  # credit spread (HYG/LQD)
    ]
    for _sname, _sresult in _fred_sources:
        if isinstance(_sresult, Exception):
            _src_health.mark_fail(_sname, str(_sresult)[:80])
        elif _sresult is not None:
            _src_health.mark_ok(_sname)

    # DGS2 is fetched by strategy, pull from existing Redis state
    try:
        _dgs2_raw = await bus._r.hget("ep:macro", "dgs2")
        dgs2 = float(_dgs2_raw or 0) or None
    except Exception:
        dgs2 = None

    # Build regime dict — only include values that passed their own sanity checks
    regime: dict = {}
    if t10y2y   is not None: regime["t10y2y"]       = t10y2y
    if core_cpi is not None: regime["core_cpi_yoy"] = core_cpi
    if pce_yoy  is not None: regime["pce_yoy"]      = pce_yoy
    if icsa     is not None: regime["icsa"]          = icsa
    if t5yifr   is not None: regime["t5yifr"]        = t5yifr
    if vix      is not None: regime["vix"]           = vix
    if t10y2y   is not None: regime["yield_curve_spread"] = t10y2y
    if move     is not None: regime["move_index"]          = move
    if cs       is not None: regime["credit_spread_hyg_lqd"] = cs
    if dgs5     is not None: regime["dgs5"]  = dgs5
    if dgs30    is not None: regime["dgs30"] = dgs30

    # Update fomc module
    from kalshi_bot.models.fomc import set_macro_regime, set_current_fed_rate
    if fed_rate:
        set_current_fed_rate(fed_rate)
        log.info("FRED DFEDTARU: current fed funds upper target = %.2f%%", fed_rate)
    if regime:
        set_macro_regime(regime)

    # LLM Fed sentiment
    try:
        fed_score = await get_fed_sentiment(bus._r)  # r is the Redis client
        if fed_score is not None:
            regime["fed_sentiment"] = fed_score
            log.info("Fed sentiment score: %.2f", fed_score)
    except Exception:
        pass

    # BAA10Y credit spread from ep_datasources Redis cache
    try:
        _baa10y_raw = await bus._r.get("ep:macro:baa10y")
        if _baa10y_raw:
            _baa10y_data = json.loads(_baa10y_raw)
            regime["baa10y_spread"] = _baa10y_data.get("spread_pct", 0)
            regime["baa10y_regime"] = _baa10y_data.get("regime", "normal")
            log.info(
                "BAA10Y: %.2f%%  regime=%s  3w_change=%.2f%%",
                _baa10y_data.get("spread_pct", 0),
                _baa10y_data.get("regime", "normal"),
                _baa10y_data.get("change_3w_pct", 0),
            )
    except Exception as _b10y_exc:
        log.debug("BAA10Y Redis read skipped: %s", _b10y_exc)

    # WALCL Fed balance sheet — log + publish to ep:macro; no trading decisions yet
    try:
        _walcl_raw = await bus._r.get("ep:macro:walcl")
        if _walcl_raw:
            _walcl_data = json.loads(_walcl_raw)
            regime["walcl_trend"]          = _walcl_data.get("trend", "")
            regime["walcl_4w_change_bill"] = _walcl_data.get("change_4w_billions", 0)
            log.info(
                "WALCL: $%.1fT  4wk_chg=%+.1fB  trend=%s",
                _walcl_data.get("latest_billions_usd", 0) / 1000,
                _walcl_data.get("change_4w_billions", 0),
                _walcl_data.get("trend", "unknown"),
            )
    except Exception as _walcl_exc:
        log.debug("WALCL Redis read skipped: %s", _walcl_exc)

    # Publish to Redis ep:macro for exec node access
    macro_hash: dict = {"ts": str(time.time())}
    for k, v in regime.items():
        if v is not None:
            macro_hash[k] = str(v)
    if dgs10  is not None: macro_hash["dgs10"]    = str(dgs10)
    if dgs5   is not None: macro_hash["dgs5"]     = str(dgs5)
    if dgs30  is not None: macro_hash["dgs30"]    = str(dgs30)
    if dgs2:               macro_hash["dgs2"]     = str(dgs2)
    if unrate is not None: macro_hash["unrate"]   = str(unrate)
    if fed_rate is not None: macro_hash["fed_rate"] = str(fed_rate)

    if macro_hash:
        await bus._r.hset("ep:macro", mapping=macro_hash)

    # Log summary
    if regime:
        regime_label = _classify_regime(regime)
        log.info(
            "Macro regime: %s | T10Y2Y=%.3f%% PCE=%.1f%% CoreCPI=%.1f%% ICSA=%s VIX=%.1f",
            regime_label,
            regime.get("t10y2y", 0),
            regime.get("pce_yoy", 0),
            regime.get("core_cpi_yoy", 0),
            f"{regime.get('icsa', 0):,.0f}" if regime.get("icsa") else "N/A",
            regime.get("vix", 0),
        )


# ── Order-book imbalance filter + confidence scaler ───────────────────────────
# Imbalance = directional_depth / opposing_depth.
# YES signal: yes_bid_depth / no_bid_depth
# NO  signal: no_bid_depth  / yes_bid_depth
#
# Hard floor: signals below _MIN_OB_IMBALANCE are dropped (book actively
# opposes the trade direction).  Set to 0.0 via env to disable entirely.
#
# Continuous scaling: passing signals get a confidence multiplier derived from
# how far imbalance is above/below neutral (1.0):
#   imbalance = _MIN_OB_IMBALANCE → × _OB_CONF_FLOOR_MULT (mild penalty)
#   imbalance = 1.0               → × 1.0 (neutral)
#   imbalance = _OB_CONF_PEAK_IMB → × _OB_CONF_PEAK_MULT (capped boost)
_MIN_OB_IMBALANCE   = float(os.getenv("MIN_OB_IMBALANCE",    "0.70"))
_OB_CONF_FLOOR_MULT = float(os.getenv("OB_CONF_FLOOR_MULT",  "0.90"))  # mult at hard floor
_OB_CONF_PEAK_MULT  = float(os.getenv("OB_CONF_PEAK_MULT",   "1.15"))  # cap for strong book
_OB_CONF_PEAK_IMB   = float(os.getenv("OB_CONF_PEAK_IMB",    "3.00"))  # imbalance at cap

# ── BTC realized-vol threshold adjuster ────────────────────────────────────────
# Rolling in-memory price buffer — no Redis read needed; populated each cycle.
# 240 entries × ~60 s cycle = ~4 h of history.
_btc_price_buf: deque = deque(maxlen=240)


async def _enrich_orderbook_imbalance(
    signals: List[Signal],
    client:  KalshiClient,
    min_imb: float = _MIN_OB_IMBALANCE,
) -> List[Signal]:
    """
    Fetch Kalshi orderbooks for candidate signals.  Two effects:

    1. Hard drop: signals where imbalance < min_imb are removed (book actively
       opposes the trade — the opposing side has materially more depth).

    2. Confidence scaling: passing signals have sig.confidence multiplied by a
       factor derived from how strongly the book supports the trade direction.
       The multiplier is piecewise-linear:
         imbalance = min_imb  →  × _OB_CONF_FLOOR_MULT  (mild penalty)
         imbalance = 1.0      →  × 1.0                  (neutral)
         imbalance ≥ peak_imb →  × _OB_CONF_PEAK_MULT   (capped boost)
       Because confidence feeds directly into Kelly sizing, a book imbalance of
       3:1 in your favour meaningfully increases position size; one at the floor
       slightly reduces it.

    Arb-pair signals (arb_partner set) bypass both effects — balance-neutral.
    Signals with no orderbook data (API error) pass through unchanged.
    """
    if not signals or min_imb <= 0.0:
        return signals

    # Arb signals bypass the filter; non-arb signals get enriched
    arb      = [s for s in signals if getattr(s, "arb_partner", None)]
    to_check = [s for s in signals if not getattr(s, "arb_partner", None)]

    if not to_check:
        return signals

    paths = [f"/markets/{s.ticker}/orderbook" for s in to_check]
    try:
        # Use per_request_timeout=5.0 so each httpx request fails naturally
        # (via ReadTimeout) before the outer asyncio.wait_for (6.0s) would need
        # to cancel them.  asyncio cancellation of in-flight httpx connections
        # can leave stale sockets registered in the event loop, blocking all
        # subsequent async I/O in the cycle.
        books = await asyncio.wait_for(
            client.get_many(paths, per_request_timeout=5.0), timeout=6.0
        )
    except Exception as exc:
        log.warning("Orderbook batch fetch failed — skipping imbalance filter: %s", exc)
        return signals

    kept = []
    for sig, ob in zip(to_check, books):
        if ob is None:
            kept.append(sig)   # no data — pass through unchanged
            continue

        # API returns orderbook_fp (fixed-point decimals) or legacy orderbook (ints).
        book_fp    = ob.get("orderbook_fp") or ob.get("orderbook") or {}
        yes_levels = book_fp.get("yes_dollars") or book_fp.get("yes") or []
        no_levels  = book_fp.get("no_dollars")  or book_fp.get("no")  or []

        yes_depth = int(sum(float(row[1]) for row in yes_levels[:5])) if yes_levels else 0
        no_depth  = int(sum(float(row[1]) for row in no_levels[:5]))  if no_levels  else 0

        if yes_depth == 0 and no_depth == 0:
            kept.append(sig)   # empty book — leave book_depth=None so exec skips gate
            continue

        sig.book_depth = yes_depth + no_depth

        imbalance = (
            yes_depth / max(no_depth,  1) if sig.side == "yes"
            else no_depth  / max(yes_depth, 1)
        )

        if imbalance < min_imb:
            log.info(
                "OB dropped  %-38s  side=%-3s  imbalance=%.2f < %.2f"
                "  (yes=%d  no=%d)",
                sig.ticker[:38], sig.side, imbalance, min_imb, yes_depth, no_depth,
            )
            continue

        # Piecewise-linear confidence multiplier
        if imbalance <= 1.0:
            t    = (imbalance - min_imb) / max(1.0 - min_imb, 1e-9)
            mult = _OB_CONF_FLOOR_MULT + t * (1.0 - _OB_CONF_FLOOR_MULT)
        else:
            t    = min(1.0, (imbalance - 1.0) / max(_OB_CONF_PEAK_IMB - 1.0, 1e-9))
            mult = 1.0 + t * (_OB_CONF_PEAK_MULT - 1.0)

        old_conf       = sig.confidence
        sig.confidence = max(0.10, min(0.99, sig.confidence * mult))

        log.debug(
            "OB scale    %-38s  side=%-3s  imbalance=%.2f  mult=%.3f"
            "  conf %.2f→%.2f  (yes=%d  no=%d)",
            sig.ticker[:38], sig.side, imbalance, mult,
            old_conf, sig.confidence, yes_depth, no_depth,
        )
        kept.append(sig)

    return kept + arb


def _compute_vol_mult(buf: deque) -> tuple:
    """
    Compute a threshold multiplier from recent BTC realized volatility.

    Returns (multiplier: float, regime: str).

    Calibration (per-sample log-return std for a 60-120 s cycle):
      std = annualized_vol / sqrt(525_600 / cycle_seconds)

      calm    std < 0.0004  (< ~30 % annualized) → mult 0.85  (calm market, accept smaller edges)
      normal  std < 0.0013  (30-95 % annualized) → mult 1.00  (default — covers typical BTC)
      high    std < 0.0020  (95-145 % annualized)→ mult 1.30  (require bigger edge)
      extreme std >= 0.0020 (> 145 % annualized) → mult 1.65  (very selective)

    Falls back to (1.0, "insufficient_data") when the buffer has < 10 prices.
    """
    arr = list(buf)
    if len(arr) < 10:
        return 1.0, "insufficient_data"

    returns = [
        math.log(arr[i] / arr[i - 1])
        for i in range(1, len(arr))
        if arr[i - 1] > 0
    ]
    if len(returns) < 5:
        return 1.0, "insufficient_returns"

    try:
        std = statistics.stdev(returns)
    except statistics.StatisticsError:
        return 1.0, "stdev_error"

    if std < 0.0004:
        return 0.85, "calm"
    elif std < 0.0013:
        return 1.00, "normal"
    elif std < 0.0020:
        return 1.30, "high"
    else:
        return 1.65, "extreme"


async def _write_pnl_snapshot(bus: "RedisBus") -> None:
    """Read live metrics from Redis and persist a P&L snapshot to Postgres."""
    import json as _json
    try:
        # Balance from ep:balance
        balance_cents: int | None = None
        raw_bal = await bus._r.hgetall("ep:balance")
        for k, v in raw_bal.items():
            key = k if isinstance(k, str) else k.decode()
            if "intel" in key.lower() or "kalshi" in key.lower():
                try:
                    balance_cents = int(_json.loads(v).get("balance_cents", 0))
                    break
                except Exception:
                    pass

        # Deployed / unrealized / count from ep:positions
        deployed_cents = 0
        unrealized_pnl_cents = 0
        position_count = 0
        raw_pos = await bus._r.hgetall("ep:positions")
        for _, pv in raw_pos.items():
            try:
                pos = _json.loads(pv if isinstance(pv, str) else pv.decode())
                entry = int(pos.get("entry_cents", 0))
                contracts = int(pos.get("contracts", 0))
                side = pos.get("side", "yes")
                cost = (entry * contracts // 100) if side == "yes" else ((100 - entry) * contracts // 100)
                deployed_cents += cost
                pnl = pos.get("unrealized_pnl_cents")
                if pnl is not None:
                    unrealized_pnl_cents += int(pnl)
                position_count += 1
            except Exception:
                pass

        # Realized P&L from ep:performance (string key, not hash)
        realized_pnl_cents: int | None = None
        try:
            raw_perf_str = await bus._r.get("ep:performance")
            if raw_perf_str:
                perf_data = _json.loads(raw_perf_str)
                realized_pnl_cents = int(float(perf_data.get("total_pnl_cents", 0) or 0))
        except Exception:
            pass

        from ep_pnl_snapshots import write_snapshot
        await write_snapshot(
            balance_cents=balance_cents,
            deployed_cents=deployed_cents or None,
            unrealized_pnl_cents=unrealized_pnl_cents,
            realized_pnl_cents=realized_pnl_cents,
            position_count=position_count or None,
        )

        if balance_cents is not None:
            try:
                import time as _time
                audit().write("balance_snapshots", {
                    "ts_us":          int(_time.time() * 1_000_000),
                    "asset_class":    "kalshi",
                    "balance_cents":  balance_cents,
                    "open_pos_count": position_count,
                    "exposure_cents": deployed_cents,
                    "daily_pnl_cents": realized_pnl_cents,
                })
            except Exception:
                pass
    except Exception as exc:
        log.debug("_write_pnl_snapshot: %s", exc)


async def _heartbeat_loop(bus: RedisBus, interval: int = 60) -> None:
    """Publish a HEARTBEAT event to ep:system every `interval` seconds."""
    from ep_health import get_health_summary
    from ep_pnl_snapshots import ensure_table
    await ensure_table()
    _last_cycle_ts: float = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        sd_notify("WATCHDOG=1")
        await bus.publish_system_event("HEARTBEAT")
        await bus.publish_health(get_health_summary())
        await _write_pnl_snapshot(bus)

        # Update /health endpoint state
        try:
            from ep_pg_audit import audit as _audit
            redis_ok = await bus.ping()
            try:
                q_size = _audit()._queue.qsize()
                pg_status = "ok" if q_size < 5_000 else "degraded"
            except RuntimeError:
                pg_status = "disabled"
                q_size = 0
            last_cycle_s = time.monotonic() - _last_cycle_ts
            _last_cycle_ts = time.monotonic()
            metrics.set_health({
                "status":           "ok" if (redis_ok and last_cycle_s < 300) else "fail",
                "redis":            "ok" if redis_ok else "fail",
                "postgres":         pg_status,
                "postgres_queue":   q_size,
                "last_cycle_s":     round(last_cycle_s, 1),
                "cycle_fresh":      last_cycle_s < 300,
            })
        except Exception:
            pass


async def _exec_watchdog_loop(bus: RedisBus, interval: int = 60) -> None:
    """Watch for stalled exec node by monitoring ep:executions stream freshness.

    Only fires an alert when Intel is actively sending signals (ep:signals is
    fresh) but no EXECUTION_REPORT has appeared within EXEC_WATCHDOG_TIMEOUT_S
    seconds — distinguishing a genuine exec stall from a quiet market period.
    """
    timeout_s = int(os.getenv("EXEC_WATCHDOG_TIMEOUT_S", "600"))
    stale_count = 0
    last_alert_at = 0.0
    log.info("Exec watchdog started (timeout=%ds, check_interval=%ds)", timeout_s, interval)

    await asyncio.sleep(120)  # grace period on startup — let exec initialise

    while True:
        try:
            now_us = time.time() * 1_000_000

            # Check last execution report
            exec_entries   = await bus._r.xrevrange("ep:executions", count=1)
            signal_entries = await bus._r.xrevrange("ep:signals",    count=1)

            last_exec_us   = int(exec_entries[0][1].get("ts_us", 0))   if exec_entries   else 0
            last_signal_us = int(signal_entries[0][1].get("ts_us", 0)) if signal_entries else 0

            exec_age_s   = (now_us - last_exec_us)   / 1_000_000 if last_exec_us   else float("inf")
            signal_age_s = (now_us - last_signal_us) / 1_000_000 if last_signal_us else float("inf")

            exec_stale   = exec_age_s   > timeout_s
            signal_fresh = signal_age_s < timeout_s  # Intel IS sending signals

            if exec_stale and signal_fresh:
                stale_count += 1
                log.warning(
                    "Exec watchdog: last EXECUTION_REPORT %.0fs ago (threshold %ds) — stale_count=%d",
                    exec_age_s, timeout_s, stale_count,
                )
                if stale_count >= 3 and (time.time() - last_alert_at) > 1800:
                    msg = (
                        f"\U0001f6a8 EdgePulse EXEC STALL \u2014 no EXECUTION_REPORT in {exec_age_s/60:.0f}m. "
                        f"Intel is sending signals but Exec appears down."
                    )
                    log.critical(msg)
                    try:
                        await _telegram.send_alert(msg)
                    except Exception:
                        pass
                    last_alert_at = time.time()
            else:
                if stale_count > 0:
                    log.info("Exec watchdog: exec recovered (exec_age=%.0fs)", exec_age_s)
                stale_count = 0

            # Mark exec_heartbeat OK if exec node published a HEARTBEAT recently.
            try:
                _sys_entries = await bus._r.xrevrange("ep:system", count=20)
                for _eid, _fields in _sys_entries:
                    _raw = _fields.get("payload") or _fields.get(b"payload")
                    if not _raw:
                        continue
                    _ev = json.loads(_raw)
                    if (_ev.get("event_type") == "HEARTBEAT"
                            and str(_ev.get("node", "")).startswith("exec")):
                        _hb_age = (time.time() * 1_000_000 - float(_ev.get("ts_us", 0))) / 1_000_000
                        if _hb_age < 180:
                            from ep_health import health as _wdg_health
                            _wdg_health.mark_ok("exec_heartbeat", f"hb {_hb_age:.0f}s ago")
                        break
            except Exception:
                pass

        except Exception as exc:
            log.debug("Exec watchdog error: %s", exc)

        await asyncio.sleep(interval)


async def _price_gap_fill_loop(
    bus: RedisBus, client, interval: int = 30
) -> None:
    """
    Every `interval` seconds, ensure every ticker in ep:positions has a fresh
    price in ep:prices.  Decoupled from the main Intel cycle so stale-price
    skips in the exit checker can't persist longer than ~30s regardless of
    cycle timing.

    A position without a fresh price means stop-losses and pre-expiry exits
    are silently skipped until Intel's next cycle — this task closes that gap.
    """
    log.info("Price gap-fill loop started (interval=%ds)", interval)
    _stale_threshold_us = 60 * 1_000_000   # 60s in microseconds

    while True:
        await asyncio.sleep(interval)
        try:
            all_pos   = await bus.get_all_positions()
            if not all_pos:
                continue
            now_us    = int(time.time() * 1_000_000)
            all_prices = await bus._r.hgetall(EP_PRICES)

            stale_tickers = []
            for ticker in all_pos:
                raw = all_prices.get(ticker) or all_prices.get(
                    ticker.encode() if isinstance(ticker, str) else ticker
                )
                if raw:
                    try:
                        ts_us = json.loads(raw).get("ts_us", 0)
                        if now_us - ts_us < _stale_threshold_us:
                            continue  # fresh enough
                    except Exception:
                        pass
                stale_tickers.append(ticker)

            if not stale_tickers:
                continue

            log.debug("price_gap_fill: %d stale tickers — fetching from REST", len(stale_tickers))
            _patch: dict = {}
            for ticker in stale_tickers:
                try:
                    resp = await asyncio.to_thread(
                        client.get, f"/markets/{ticker}"
                    )
                    market = (resp or {}).get("market", {}) if isinstance(resp, dict) else {}
                    _yp_raw = (
                        market.get("last_price_dollars")
                        or market.get("yes_bid_dollars")
                        or market.get("last_price")
                        or market.get("yes_bid")
                        or market.get("market_price")
                    )
                    if _yp_raw is not None:
                        _yp = round(float(_yp_raw) * 100)
                        _patch[ticker] = json.dumps({
                            "yes_price":  _yp,
                            "no_price":   100 - _yp,
                            "spread":     0,
                            "last_price": _yp,
                            "ts_us":      int(time.time() * 1_000_000),
                        })
                except Exception as exc:
                    log.debug("price_gap_fill: REST fetch failed for %s: %s", ticker, exc)

            if _patch:
                await bus._r.hset(EP_PRICES, mapping=_patch)
                log.info("price_gap_fill: refreshed %d stale prices via REST", len(_patch))

        except Exception as exc:
            log.debug("price_gap_fill_loop error: %s", exc)


async def _wait_for_dependencies() -> None:
    """Retry Redis and Postgres until reachable before marking the service ready."""
    import redis.asyncio as _aioredis
    import asyncpg as _asyncpg

    _r = await _aioredis.from_url(REDIS_URL, socket_connect_timeout=3)
    try:
        for attempt in range(30):
            try:
                await _r.ping()
                log.info("Redis ready")
                break
            except Exception as exc:
                log.info("Waiting for Redis (%d/30): %s", attempt + 1, exc)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("Redis unreachable after 60s")
    finally:
        await _r.aclose()

    dsn = _os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if dsn:
        for attempt in range(15):
            try:
                _pg = await _asyncpg.connect(dsn, timeout=5)
                await _pg.execute("SELECT 1")
                await _pg.close()
                log.info("Postgres ready")
                break
            except Exception as exc:
                log.info("Waiting for Postgres (%d/15): %s", attempt + 1, exc)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("Postgres unreachable after 30s")


async def intel_main() -> None:
    """Main entry point for the Intel node.

    Initializes Kalshi auth, Redis bus, and strategy scanner, then runs the
    signal-generation loop: fetches market data, scores all enabled strategies, and
    publishes signals to Exec via Redis. Does not return (runs until SIGTERM).
    """
    setup_logging(cfg.OUTPUT_DIR / "logs")
    cfg.validate()
    await _wait_for_dependencies()

    mode_label = "PAPER" if cfg.PAPER_TRADE else "LIVE"
    log.info("=" * 60)
    log.info("EdgePulse Intel  node=%s  mode=%s", NODE_ID, mode_label)
    log.info("=" * 60)

    # ── Auth + clients ────────────────────────────────────────────────────────
    auth   = NoAuth() if (cfg.PAPER_TRADE and not cfg.API_KEY_ID) else \
             KalshiAuth(api_key_id=cfg.API_KEY_ID, private_key_path=cfg.PRIVATE_KEY_PATH)
    client = KalshiClient(
        base_url    = cfg.BASE_URL,
        auth        = auth,
        timeout     = cfg.HTTP_TIMEOUT,
        max_retries = cfg.MAX_RETRIES,
        backoff     = cfg.RETRY_BACKOFF,
        concurrency = cfg.CONCURRENCY,
    )

    # ── Shared in-process state (dashboard + WebSocket) ───────────────────────
    state      = BotState()
    state.mode = "paper" if cfg.PAPER_TRADE else "live"

    # ── Email alert manager ───────────────────────────────────────────────────
    _alert_manager = AlertManager(
        state            = state,
        smtp_host        = _os.getenv("ALERT_SMTP_HOST"),
        smtp_port        = int(_os.getenv("ALERT_SMTP_PORT", "587")),
        smtp_user        = _os.getenv("ALERT_SMTP_USER"),
        smtp_password    = _os.getenv("ALERT_SMTP_PASSWORD"),
        alert_from_email = _os.getenv("ALERT_FROM_EMAIL"),
        alert_to_email   = _os.getenv("ALERT_TO_EMAIL"),
        min_edge_cents   = float(_os.getenv("KALSHI_EDGE_THRESHOLD", "0.10")) * 100,
    )

    # ── Redis ─────────────────────────────────────────────────────────────────
    bus = RedisBus(REDIS_URL, NODE_ID)
    await bus.connect()
    await init_audit_writer()
    await bus.publish_system_event("INTEL_START", f"mode={mode_label}")
    sd_notify("READY=1")
    heartbeat_task       = asyncio.create_task(_heartbeat_loop(bus))
    release_monitor_task = asyncio.create_task(_release_monitor_loop(bus))
    exec_watchdog_task   = asyncio.create_task(_exec_watchdog_loop(bus))
    price_gap_fill_task  = asyncio.create_task(_price_gap_fill_loop(bus, client))

    # ── BTC mean-reversion strategy ───────────────────────────────────────────
    # Candle data: uses Polygon if POLYGON_API_KEY is set, otherwise falls back
    # to the free Coinbase Exchange public OHLC API (no key required).
    polygon_key  = os.getenv("POLYGON_API_KEY", "")
    btc_strategy: Optional[BTCMeanReversionStrategy] = BTCMeanReversionStrategy(
        polygon_api_key = polygon_key,
        source_node     = NODE_ID,
    )
    if polygon_key:
        log.info("BTC mean-reversion enabled (Polygon candles).")
    else:
        log.info("BTC mean-reversion enabled (Coinbase Exchange candles — free tier).")

    # ── Prometheus metrics server ─────────────────────────────────────────────
    metrics_port = int(os.getenv("METRICS_PORT", "9091"))
    metrics.start(port=metrics_port)

    # ── WebSocket price feed (daemon thread — does NOT use asyncio) ───────────
    # WS endpoint follows BASE_URL, not PAPER_TRADE — the two are independent:
    #   PAPER_TRADE=true  → simulates orders (no real money at risk)
    #   BASE_URL=live     → always reads real market data via live WebSocket
    # If BASE_URL is the demo endpoint the WS also uses demo; otherwise live.
    ws_paper = "demo" in cfg.BASE_URL
    ws = KalshiWebSocket(state=state, auth=auth, paper=ws_paper)
    ws.start()

    # Dashboard runs as a separate Streamlit process (start.sh screen dash).

    summary:           DailySummary   = DailySummary()
    markets_cache:     List[dict]     = []
    fomc_cache:        List[dict]     = []   # KXFED markets — refreshed with markets_cache
    markets_last_scan: float          = 0.0

    # Per-scanner consecutive zero-signal cycle counter.
    # Logs a warning when a scanner has been silent for _ZERO_SIG_WARN cycles.
    _zero_sig_counts: dict[str, int] = {
        "fomc": 0, "weather": 0, "economic": 0, "sports": 0,
        "crypto_price": 0, "gdp": 0, "arb": 0,
    }
    _ZERO_SIG_WARN = 20  # ~40 min at 120s/cycle

    # Baseline BTC z-threshold from env (before LLM/vol overrides are applied)
    _btc_z_base: float = btc_strategy.z_thresh if btc_strategy else 1.5

    # ── Full macro refresh on startup (FRED + fomc model update) ─────────────
    _fred_key      = os.getenv("FRED_API_KEY", "")
    _rate_fallback = float(os.getenv("CURRENT_FED_RATE", "4.25"))
    # Fetch DGS2 separately (used by signal generation) alongside full macro refresh
    current_treasury_2y, _ = await asyncio.gather(
        fetch_treasury_2y_yield(_fred_key),
        _refresh_macro_data(bus),
    )
    # Seed local vars from Redis ep:macro (populated by _refresh_macro_data above)
    current_fed_rate: float = _rate_fallback
    try:
        _fr_raw = await bus._r.hget("ep:macro", "fed_rate")
        current_fed_rate = float(_fr_raw) if _fr_raw else _rate_fallback
    except Exception:
        current_fed_rate = _rate_fallback
    try:
        _vix_raw = await bus._r.hget("ep:macro", "vix")
        current_vix = float(_vix_raw) if _vix_raw else None
    except Exception:
        current_vix = None
    try:
        _dgs10_raw = await bus._r.hget("ep:macro", "dgs10")
        current_dgs10 = float(_dgs10_raw) if _dgs10_raw else None
    except Exception:
        current_dgs10 = None
    try:
        _move_raw = await bus._r.hget("ep:macro", "move_index")
        current_move: Optional[float] = float(_move_raw) if _move_raw else None
    except Exception:
        current_move = None
    _rate_last_day: str = __import__("datetime").date.today().isoformat()
    intel_consumer        = f"{NODE_ID}-intel"
    _last_perf_publish:     float        = 0.0
    _last_balance_cents:    Optional[int] = None   # persists last successful Kalshi fetch
    _last_cb_balance_cents: Optional[int] = None   # persists last successful Coinbase fetch

    # Coinbase client for balance reporting (paper-safe — only fetches, never places orders)
    _cb_client = CoinbaseTradeClient() if os.getenv("COINBASE_API_KEY_NAME") else None

    # ── Startup GDP risk check ────────────────────────────────────────────────
    # Startup GDP risk check: warn and queue cut-loss signals for offside positions.
    # Threshold: >0.75pp gap for both YES (GDPNow < strike) and NO (GDPNow > strike).
    if _fred_key:
        try:
            import httpx as _httpx
            import re as _re
            async with _httpx.AsyncClient(timeout=8.0) as _gdp_http:
                _gdp_url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
                    "https://api.stlouisfed.org/fred/series/observations"
                    f"?series_id=GDPNOW&api_key={_fred_key}"
                    "&file_type=json&sort_order=desc&limit=2"
                )
                _gdp_resp = await _gdp_http.get(_gdp_url)
            _gdp_now_val: Optional[float] = None
            if _gdp_resp.status_code == 200:
                _gdp_obs = [
                    o for o in _gdp_resp.json().get("observations", [])
                    if o.get("value", ".") != "."
                ]
                if _gdp_obs:
                    _gdp_now_val = float(_gdp_obs[0]["value"])
                    log.info("Startup GDP risk check: GDPNow = %.2f%%", _gdp_now_val)
                    await bus._r.hset("ep:macro", "gdpnow", str(_gdp_now_val))

            if _gdp_now_val is not None:
                _open_positions = await bus.get_all_positions()
                for _pos_ticker, _pos_data in _open_positions.items():
                    if not _re.match(r"^KXGDP-\d{2}[A-Z]{3}\d{2}-T[\d.]+$", _pos_ticker):
                        continue
                    if _pos_data.get("contracts", 1) == 0:
                        continue
                    _strike_match = _re.search(r"-T(\d+\.?\d*)$", _pos_ticker)
                    if not _strike_match:
                        continue
                    _strike   = float(_strike_match.group(1))
                    _pos_side = _pos_data.get("side", "yes").lower()
                    # gap > 0 means position is offside vs GDPNow
                    _gap = (_strike - _gdp_now_val) if _pos_side == "yes" else (_gdp_now_val - _strike)
                    if _gap > 0.50:
                        log.warning(
                            "GDP RISK WARNING: %s %s — GDPNow %.2f%% is %.2f%% against strike %.1f%%.",
                            _pos_ticker, _pos_side.upper(), _gdp_now_val, _gap, _strike,
                        )
                    if _gap > 0.75:
                        await bus._r.set(
                            f"ep:cut_loss:{_pos_ticker}",
                            f"startup: GDPNow={_gdp_now_val:.2f}% vs strike={_strike:.1f}% ({_pos_side})",
                            ex=300,
                        )
                        log.warning("GDP CUT-LOSS signal written at startup: %s", _pos_ticker)
        except Exception as _gdp_exc:
            log.debug("Startup GDP risk check failed (non-fatal): %s", _gdp_exc)

    try:
        while True:
            cycle_start = time.monotonic()
            state.record_cycle()
            summary.record_cycle()

            # ── Refresh FRED data once per calendar day ───────────────────────
            _today = __import__("datetime").date.today().isoformat()
            if _today != _rate_last_day:
                current_treasury_2y, _ = await asyncio.gather(
                    fetch_treasury_2y_yield(_fred_key),
                    _refresh_macro_data(bus),
                )
                _rate_last_day = _today
                # Re-read authoritative values from Redis (set by _refresh_macro_data)
                try:
                    _fr_raw   = await bus._r.hget("ep:macro", "fed_rate")
                    _vix_raw  = await bus._r.hget("ep:macro", "vix")
                    _d10_raw  = await bus._r.hget("ep:macro", "dgs10")
                    _mv_raw   = await bus._r.hget("ep:macro", "move_index")
                    if _fr_raw:  current_fed_rate = float(_fr_raw)
                    if _vix_raw: current_vix      = float(_vix_raw)
                    if _d10_raw: current_dgs10    = float(_d10_raw)
                    if _mv_raw:  current_move     = float(_mv_raw)
                except Exception as _mac_exc:
                    log.debug("ep:macro re-read failed (non-fatal): %s", _mac_exc)

            # ── Check for forced cycle (e.g. BLS release detected) ───────────
            forced = await bus._r.getdel("ep:forced_cycle")
            if forced:
                log.info("Forced intel cycle triggered (BLS release detected)")

            # ── Check ops halt flag ───────────────────────────────────────────
            if await bus.is_halted():
                log.warning("HALT_TRADING flag set in Redis — sleeping 60s.")
                await asyncio.sleep(60)
                continue

            # ── Balance ───────────────────────────────────────────────────────
            # Fetch the real account balance even in paper mode so that risk
            # gates and Kelly sizing reflect the actual capital on hand.
            # Only skip the network call when there are no real credentials.
            balance_cents = 100_000   # fallback ($1,000) if fetch not possible
            _portfolio_value_cents = 0
            if cfg.API_KEY_ID:
                try:
                    bal                       = client.get("/portfolio/balance")
                    balance_cents             = bal.get("balance", 0)
                    _portfolio_value_cents    = bal.get("portfolio_value", 0)
                    _last_balance_cents       = balance_cents   # save for fallback
                except Exception:
                    if _last_balance_cents is not None:
                        balance_cents = _last_balance_cents
                        log.warning("Balance fetch failed — using last known value (%d¢).",
                                    _last_balance_cents)
                    else:
                        log.warning("Balance fetch failed — no prior value; using paper default.")
                        # keep balance_cents = 100_000 as safe fallback
            if balance_cents is not None:
                state.set_balance(balance_cents)
                await bus.set_balance(balance_cents, state.mode, _portfolio_value_cents)
                metrics.update_balance(balance_cents)

            # ── Coinbase balance (USD + BTC holdings) ─────────────────────────
            # Refresh every cycle so the dashboard shows current portfolio value.
            # Uses the most recent BTC spot price from the price buffer; falls back
            # to the Redis ep:prices hash if the buffer is empty (early in cycle).
            if _cb_client is not None:
                try:
                    btc_spot = 0.0
                    if _btc_price_buf:
                        btc_spot = _btc_price_buf[-1]
                    else:
                        # Try Redis ep:prices for BTC-USD price (set by BTC strategy)
                        _btc_raw = await bus._r.hget(EP_PRICES, "BTC-USD")
                        if _btc_raw:
                            import json as _json
                            _btc_snap = _json.loads(_btc_raw)
                            btc_spot = float(_btc_snap.get("last_price", 0))
                    cb_total = await _cb_client.get_total_balance_cents(btc_spot)
                    if cb_total is not None:
                        _last_cb_balance_cents = cb_total
                    elif _last_cb_balance_cents is not None:
                        cb_total = _last_cb_balance_cents
                    if cb_total is not None:
                        import time as _time
                        await bus._r.hset("ep:balance", "coinbase", __import__("json").dumps({
                            "balance_cents": cb_total,
                            "mode":          state.mode,
                            "ts_us":         int(_time.time() * 1_000_000),
                        }))
                        log.debug(
                            "Coinbase balance: $%.2f  (BTC spot $%.0f)",
                            cb_total / 100, btc_spot,
                        )
                except Exception:
                    log.debug("Coinbase balance refresh skipped (non-critical).")

            # ── Market cache (full rescan every 20 min) ───────────────────────
            if time.monotonic() - markets_last_scan > 1200:
                # Always update markets_last_scan even on failure, so we don't
                # retry scan_all_markets at the full cycle cadence (every 120s)
                # on a persistent failure — that burns API quota and floods
                # logs. Next retry after the normal 20-min cooldown.
                markets_last_scan = time.monotonic()
                try:
                    markets_cache = scan_all_markets(client)
                    # Only subscribe WebSocket to tradeable markets (skip sports/novelty
                    # series that generate millions of sub-penny ticks we never trade)
                    _WS_PREFIXES = ("KXFED", "KXBTC", "KXETH", "INX", "NASDAQ", "CPI", "JOBS")
                    ws_tickers = [
                        m["ticker"] for m in markets_cache
                        if any(m["ticker"].startswith(p) for p in _WS_PREFIXES)
                    ]
                    ws.subscribe_tickers(ws_tickers)
                    log.info("Market rescan: %d markets (%d WS subscriptions)",
                             len(markets_cache), len(ws_tickers))
                except Exception:
                    log.exception("Market scan failed.")
                try:
                    kxfed_resp = client.get(
                        "/markets",
                        params={"status": "open", "series_ticker": "KXFED", "limit": 200},
                    )
                    fomc_cache = kxfed_resp.get("markets", [])
                    log.debug("FOMC cache refreshed: %d markets", len(fomc_cache))
                except Exception:
                    log.debug("FOMC cache refresh failed — close_time may be null")

            # ── Publish price snapshot to Redis (Exec uses this for exits) ────
            snapshot = PriceSnapshot(source_node=NODE_ID)
            with state._lock:
                for ticker, mkt in state.markets.items():
                    snapshot.prices[ticker] = {
                        "yes_price":  mkt.yes_price,
                        "no_price":   mkt.no_price,
                        "spread":     mkt.spread,
                        "last_price": mkt.last_price,
                    }
            await bus.publish_prices(snapshot)

            # ── Inject live KXFED prices into FOMC model ──────────────────────
            # Primary: WebSocket snapshot (real-time ticks)
            # Fallback: fomc_cache REST prices (refreshed every 20 min) — covers
            #           thin markets that rarely trade and thus never get WS ticks.
            _kxfed_snap: dict[str, int] = {}
            for _t, _p in snapshot.prices.items():
                if _t.startswith("KXFED-") and isinstance(_p, dict):
                    _yp = _p.get("yes_price")
                    if isinstance(_yp, (int, float)) and _yp > 0:
                        _kxfed_snap[_t] = int(_yp)
            # Augment from REST fomc_cache for tickers not in WS snapshot.
            # Kalshi REST API v2 uses _dollars suffix for price fields.
            for _m in fomc_cache:
                _ft = _m.get("ticker", "")
                if not _ft.startswith("KXFED-") or _ft in _kxfed_snap:
                    continue
                # Try _dollars fields (REST API v2) then legacy field names
                _mp = (
                    _m.get("last_price_dollars")
                    or _m.get("yes_bid_dollars")
                    or _m.get("last_price")
                    or _m.get("yes_bid")
                    or _m.get("market_price")
                )
                if _mp and float(_mp or 0) > 0:
                    _kxfed_snap[_ft] = round(float(_mp) * 100)
            if _kxfed_snap:
                _fomc_inject_prices(_kxfed_snap)
            else:
                _src_health.mark_fail("kalshi_implied",
                                      "no KXFED prices in WS snapshot or fomc_cache")

            # ── Health tracking for core infrastructure ───────────────────────
            # kalshi_ws: mark OK if WS is alive (connected) OR if ep:prices has
            # data — WS snapshot is empty for thin prediction markets that don't
            # trade every minute, but that is normal and expected behaviour.
            _ws_has_prices = bool(snapshot.prices)
            _redis_has_prices = bool(_kxfed_snap)
            if _ws_has_prices or _redis_has_prices:
                _src_health.mark_ok("kalshi_ws",
                                    f"ws={len(snapshot.prices)} rest={len(_kxfed_snap)}")
            else:
                _src_health.mark_fail("kalshi_ws", "no prices from WS or REST")
            if _redis_has_prices:
                _src_health.mark_ok("kalshi_rest", f"{len(_kxfed_snap)} tickers via REST")
            _src_health.mark_ok("redis")
            # Mark exec_heartbeat OK if exec node published a fresh HEARTBEAT.
            try:
                _exec_sys = await bus._r.xrevrange("ep:system", count=20)
                for _eid, _eflds in _exec_sys:
                    _epayload = _eflds.get("payload") or _eflds.get(b"payload")
                    if not _epayload:
                        continue
                    _eev = json.loads(_epayload)
                    if (_eev.get("event_type") == "HEARTBEAT"
                            and str(_eev.get("node", "")).startswith("exec")):
                        _ehb_age = (time.time() * 1_000_000 - float(_eev.get("ts_us", 0))) / 1_000_000
                        if _ehb_age < 180:
                            _src_health.mark_ok("exec_heartbeat", f"hb {_ehb_age:.0f}s ago")
                        break
            except Exception:
                pass
            _src_health.log_cycle_summary()

            # ── Redis config overrides (dashboard writes these to ep:config) ────
            _ov_edge   = await bus.get_config_override("override_edge_threshold")
            _ov_maxc   = await bus.get_config_override("override_max_contracts")
            _ov_conf   = await bus.get_config_override("override_min_confidence")
            _ov_hbc    = await bus.get_config_override("override_hours_before_close")
            _ov_rate   = await bus.get_config_override("CURRENT_FED_RATE")
            _ov_myep   = await bus.get_config_override("override_min_yes_entry_price")

            try:
                edge_threshold = float(_ov_edge) if _ov_edge else cfg.EDGE_THRESHOLD
            except (ValueError, TypeError):
                log.warning("Malformed override_edge_threshold=%r — using default", _ov_edge)
                edge_threshold = cfg.EDGE_THRESHOLD
            try:
                max_contracts = int(float(_ov_maxc)) if _ov_maxc else cfg.MAX_CONTRACTS
            except (ValueError, TypeError):
                log.warning("Malformed override_max_contracts=%r — using default", _ov_maxc)
                max_contracts = cfg.MAX_CONTRACTS
            try:
                min_confidence = float(_ov_conf) if _ov_conf else cfg.MIN_CONFIDENCE
            except (ValueError, TypeError):
                log.warning("Malformed override_min_confidence=%r — using default", _ov_conf)
                min_confidence = cfg.MIN_CONFIDENCE
            # Only override the FRED-fetched rate if the key is explicitly set
            if _ov_rate:
                try:
                    current_fed_rate = float(_ov_rate)
                except (ValueError, TypeError):
                    log.warning("Malformed CURRENT_FED_RATE=%r — keeping FRED value", _ov_rate)

            # ── Vol-adjusted Kalshi edge threshold ────────────────────────────
            # Scale edge_threshold up during high BTC realized vol — serves as a
            # macro-uncertainty proxy (high crypto vol → require larger Kalshi edge).
            # vol_mult / vol_regime are also used later for BTC z_thresh.
            vol_mult, vol_regime = _compute_vol_mult(_btc_price_buf)
            if vol_mult != 1.0:
                _pre_vol_edge = edge_threshold
                edge_threshold = round(edge_threshold * vol_mult, 4)
                log.debug(
                    "Vol-adj edge_threshold %.4f → %.4f (regime=%s)",
                    _pre_vol_edge, edge_threshold, vol_regime,
                )

            # ── Volume recording (behavioral late-money detector) ─────────────
            # Build a per-ticker volume map for late-money spike detection below.
            # record_volume() updates the in-memory ring buffer; no Redis I/O.
            _market_vol_map: dict = {}
            for _m in markets_cache:
                _vol = float(_m.get("volume", 0) or 0)
                record_volume(_m["ticker"], _vol)
                _market_vol_map[_m["ticker"]] = _vol

            # ── FOMC meeting day orderflow detection ───────────────────────────
            # On FOMC decision days, large single trades signal informed flow.
            # Detect by checking if any KXFED market had volume spike in last
            # 5 minutes compared to its rolling average. If so, force re-scan
            # by setting ep:forced_cycle to skip the next sleep.
            _now_h_utc = datetime.utcnow().hour
            _is_fomc_day = False
            try:
                _fomc_day_raw = await bus._r.hget("ep:macro", "next_fomc_date")
                if _fomc_day_raw:
                    _fomc_day_str = _fomc_day_raw.decode() if isinstance(_fomc_day_raw, bytes) else str(_fomc_day_raw)
                    _fomc_date = datetime.strptime(_fomc_day_str, "%Y-%m-%d").date()
                    _is_fomc_day = (_fomc_date == datetime.utcnow().date())
            except Exception:
                pass

            if _is_fomc_day and 17 <= _now_h_utc <= 19:
                # Check KXFED near-term market volumes for spikes
                try:
                    _fomc_near = [
                        m for m in markets_cache
                        if m.get("ticker", "").startswith("KXFED-")
                    ][:3]
                    for _fmkt in _fomc_near:
                        _vol_now = int(_fmkt.get("volume", 0) or 0)
                        _vol_key = f"ep:vol_prev:{_fmkt.get('ticker','')}"
                        _vol_prev_raw = await bus._r.get(_vol_key)
                        _vol_prev = int(_vol_prev_raw or 0)
                        if _vol_prev and _vol_now > _vol_prev * 3:
                            log.info(
                                "FOMC orderflow spike: %s vol=%d (+%d×) — forcing rescan",
                                _fmkt.get("ticker",""), _vol_now, _vol_now // max(_vol_prev, 1)
                            )
                            await bus._r.set("ep:forced_cycle", "1", ex=120)
                            break
                        await bus._r.set(_vol_key, str(_vol_now), ex=600)
                except Exception as _of_exc:
                    log.debug("FOMC orderflow check failed: %s", _of_exc)

            # ── Signal generation ─────────────────────────────────────────────
            # Direct await — intel_main() IS the running event loop;
            # no asyncio.run() wrapper needed (or allowed) here.
            signals: List[Signal] = []
            try:
                _min_yep: Optional[float] = None
                if _ov_myep:
                    try:
                        _min_yep = float(_ov_myep)
                    except (ValueError, TypeError):
                        log.warning("Malformed override_min_yes_entry_price=%r — using default", _ov_myep)

                signals = await asyncio.wait_for(
                    fetch_signals_async(
                        client               = client,
                        edge_threshold       = edge_threshold,
                        max_contracts        = max_contracts,
                        min_confidence       = min_confidence,
                        fred_api_key         = _fred_key,
                        current_rate         = current_fed_rate,
                        treasury_2y          = current_treasury_2y,
                        enable_fomc          = True,
                        enable_weather       = os.getenv("ENABLE_WEATHER", "true") == "true",
                        enable_economic      = os.getenv("ENABLE_ECONOMIC", "true") == "true",
                        enable_sports        = os.getenv("ENABLE_SPORTS", "true") == "true",
                        enable_crypto_price  = os.getenv("ENABLE_CRYPTO_PRICE", "true") == "true",
                        enable_gdp           = os.getenv("ENABLE_GDP", "true") == "true",
                        markets_cache        = markets_cache,
                        btc_spot             = btc_strategy.last_spot if btc_strategy else None,
                        min_yes_entry_price  = _min_yep,
                    ),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                log.warning("Signal generation timeout (>90s) — using partial results (signals=[])")
                signals = []
            except Exception:
                log.exception("Signal generation failed.")

            # ── Orderbook imbalance filter ─────────────────────────────────
            # Drop signals where the live order book contradicts the direction
            # (e.g., a YES signal when NO buyers outnumber YES buyers ≥ 1.4×).
            # Runs after strategy filtering so we only hit the orderbook API
            # for the small set of already-qualified candidates.
            if signals:
                _before = len(signals)
                signals = await _enrich_orderbook_imbalance(signals, client)
                _dropped = _before - len(signals)
                if _dropped:
                    log.info("OB filter: dropped %d/%d signals (imbalance)", _dropped, _before)

            # ── Behavioral adjustments (late-money + recency bias) ─────────
            # Applied after OB filter so adjustments only hit candidate signals.
            for _sig in signals:
                # Late-money spike: accelerating volume → market may be crowded
                _cur_vol = _market_vol_map.get(_sig.ticker, 0.0)
                if is_late_money_spike(_sig.ticker, _cur_vol):
                    _sig.confidence = max(0.10, _sig.confidence * 0.90)
                    log.info(
                        "Late-money spike: %-38s  confidence → %.2f",
                        _sig.ticker[:38], _sig.confidence,
                    )
                # Recency bias: recent surprise outcome → temper fair value
                _series = _sig.ticker.split("-")[0] if "-" in _sig.ticker else _sig.ticker
                _bias   = await recency_bias_adj(_series, bus)
                if _bias != 0.0:
                    _sig.fair_value = max(0.01, min(0.99, _sig.fair_value + _bias))
                    _sig.edge       = _sig.fair_value - _sig.market_price

            state.set_signals([{
                "ticker":       s.ticker,       "side":        s.side,
                "fair_value":   s.fair_value,   "market_price": s.market_price,
                "edge":         s.edge,         "confidence":  s.confidence,
                "contracts":    s.contracts,    "model_source": s.model_source,
                "spread_cents": s.spread_cents,
            } for s in signals])
            for s in signals:
                state.update_fair_value(s.ticker, s.fair_value, s.edge, s.confidence)

            # ── Publish REST-derived Kalshi prices to Redis ───────────────────
            # The WebSocket only delivers ticks when a trade occurs.  On thin
            # FOMC markets (few trades per day) ep:prices never gets populated,
            # so exec's exit checker skips every Kalshi position.  Backfill with
            # the REST market_price (bid/ask mid) from each signal — good enough
            # for take-profit / stop-loss decisions.
            if signals:
                ts_now = int(time.time() * 1_000_000)
                kalshi_price_patch: dict = {}
                for _s in signals:
                    if _s.market_price and _s.ticker:
                        # market_price is 0–1 scale; ep:prices uses 0–100 integer
                        # cents to match BotState (mkt.yes_price is int cents).
                        mp_cents = round(_s.market_price * 100)
                        kalshi_price_patch[_s.ticker] = json.dumps({
                            "yes_price":  mp_cents,
                            "no_price":   100 - mp_cents,
                            "spread":     _s.spread_cents or 0,
                            "last_price": mp_cents,
                            "ts_us":      ts_now,
                        })
                if kalshi_price_patch:
                    await bus._r.hset(EP_PRICES, mapping=kalshi_price_patch)
                    log.debug("Published REST prices for %d Kalshi tickers to ep:prices",
                              len(kalshi_price_patch))

            # ── Price backfill for held positions below signal edge threshold ──
            # Positions can have stale prices if their edge falls below MIN_EDGE_GROSS
            # (e.g. KXGDP YES at 3¢ won't appear in signals). The exit checker skips
            # tickers whose ep:prices entry is >5 min old. Patch from markets_cache
            # (already in memory) to keep all held positions fresh.
            _held_set    = set(await bus.get_all_positions())
            _sig_tickers = {s.ticker for s in signals}
            _unheld_miss = _held_set - _sig_tickers
            if _unheld_miss:
                _markets_by_ticker = {
                    m["ticker"]: m for m in markets_cache if "ticker" in m
                }
                _ts_now = int(time.time() * 1_000_000)
                _gap_patch: dict = {}
                for _t in _unheld_miss:
                    _m = _markets_by_ticker.get(_t)
                    if not _m:
                        continue
                    _mp = (
                        _m.get("last_price_dollars")
                        or _m.get("yes_bid_dollars")
                        or _m.get("last_price")
                        or _m.get("yes_bid")
                        or _m.get("market_price")
                    )
                    if _mp and float(_mp or 0) > 0:
                        _yp = round(float(_mp) * 100)
                        _gap_patch[_t] = json.dumps({
                            "yes_price":  _yp,
                            "no_price":   100 - _yp,
                            "spread":     0,
                            "last_price": _yp,
                            "ts_us":      _ts_now,
                        })
                if _gap_patch:
                    await bus._r.hset(EP_PRICES, mapping=_gap_patch)
                    log.debug(
                        "Price backfill: %d held tickers not in signals → ep:prices",
                        len(_gap_patch),
                    )

            # ── Market snapshot (backtest dataset) ───────────────────────────
            # Record every market Intel sees this cycle.  bid/ask/mid from the
            # WebSocket snapshot where available (real-time); REST market_cache
            # for volume/OI/close_time (refreshed every 20 min — acceptable staleness).
            # Signal overlay is null for the ~99% of markets with no edge this cycle.
            try:
                _snap_ts_us   = int(time.time() * 1_000_000)
                _sig_by_tick  = {s.ticker: s for s in signals}
                _ws_snap      = snapshot.prices   # dict[ticker, {yes_price, no_price, spread, ...}]
                _snap_writer  = _intel_audit()

                for _sm in markets_cache:
                    _st = _sm.get("ticker")
                    if not _st:
                        continue

                    # REST bid/ask (20-min cadence)
                    _rb = _sm.get("yes_bid_dollars")
                    _ra = _sm.get("yes_ask_dollars")
                    _yes_bid = round(float(_rb) * 100) if _rb else None
                    _yes_ask = round(float(_ra) * 100) if _ra else None

                    # WS mid + spread override (per-cycle, real-time)
                    _ws = _ws_snap.get(_st)
                    _ws = _ws if isinstance(_ws, dict) else None
                    if _ws and _ws.get("yes_price") is not None:
                        _yes_price = int(_ws["yes_price"])
                        _spread    = int(_ws["spread"]) if _ws.get("spread") is not None else (
                            (_yes_ask - _yes_bid) if (_yes_bid and _yes_ask) else None
                        )
                    elif _yes_bid is not None and _yes_ask is not None:
                        _yes_price = (_yes_bid + _yes_ask) // 2
                        _spread    = _yes_ask - _yes_bid
                    else:
                        _mp = (_sm.get("last_price_dollars") or _sm.get("market_price"))
                        _yes_price = round(float(_mp) * 100) if _mp else None
                        _spread    = None

                    _sig = _sig_by_tick.get(_st)
                    _snap_writer.write("market_snapshots", {
                        "ts_us":         _snap_ts_us,
                        "ticker":        _st,
                        "series_ticker": _sm.get("series_ticker") or (_st.split("-")[0] if "-" in _st else _st),
                        "yes_bid":       _yes_bid,
                        "yes_ask":       _yes_ask,
                        "yes_price":     _yes_price,
                        "spread":        _spread,
                        "volume":        int(float(_sm.get("volume", 0) or 0)),
                        "open_interest": int(float(_sm.get("open_interest", 0) or 0)),
                        "close_time":    _sm.get("close_time"),
                        "signal_edge":   round(_sig.edge, 4) if _sig else None,
                        "signal_side":   _sig.side if _sig else None,
                        "signal_fv":     round(_sig.fair_value, 4) if _sig else None,
                        "signal_conf":   round(_sig.confidence, 4) if _sig else None,
                    })
                log.debug("market_snapshots: queued %d rows (ts=%d)", len(markets_cache), _snap_ts_us)
            except Exception:
                log.debug("market_snapshot write failed", exc_info=True)

            # ── Dedup: skip tickers already held in Redis positions ───────────
            # For arb signals both legs must be held to consider the pair complete.
            # If only the primary is held (partner failed), re-publish so Exec can
            # place the missing partner leg.
            current_positions = await bus.get_all_positions()

            # ── GDP cut-loss: write ep:cut_loss when signal has reversed ─────────
            # Triggers for both YES (GDPNow < strike - 0.75pp) and NO (GDPNow > strike + 0.75pp)
            # within 14 days of expiry.  Exec _exit_checker consumes the key: sells
            # filled positions via the normal exit path; cancels resting orders via tombstone.
            _gdp_now_cached: Optional[float] = None
            for _at_ticker, _at_data in list(current_positions.items()):
                if not _at_ticker.startswith("KXGDP-"):
                    continue
                if _at_data.get("contracts", 1) == 0:
                    continue
                _at_strike_m = _re.search(r"-T(\d+\.?\d*)$", _at_ticker)
                if not _at_strike_m:
                    continue
                _at_strike = float(_at_strike_m.group(1))
                _at_side   = _at_data.get("side", "yes").lower()
                try:
                    if _gdp_now_cached is None:
                        _fred_key_cl = os.getenv("FRED_API_KEY", "")
                        if _fred_key_cl:
                            import httpx as _httpx_at
                            async with _httpx_at.AsyncClient(timeout=8.0) as _hc:
                                _gr = await _hc.get(
                                    # FRED requires api_key as query param; no header auth supported — accepted risk
                                    "https://api.stlouisfed.org/fred/series/observations"
                                    f"?series_id=GDPNOW&api_key={_fred_key_cl}"
                                    "&file_type=json&sort_order=desc&limit=1"
                                )
                            if _gr.status_code == 200:
                                _g_obs = [o for o in _gr.json().get("observations", [])
                                          if o.get("value", ".") != "."]
                                if _g_obs:
                                    _gdp_now_cached = float(_g_obs[0]["value"])
                    if _gdp_now_cached is None:
                        continue
                    # Keep ep:macro fresh so Exec can read GDPNow at exit time for logging
                    await bus._r.hset("ep:macro", "gdpnow", str(_gdp_now_cached))
                    _src_health.mark_ok("gdpnow")
                    _at_gap = (_at_strike - _gdp_now_cached) if _at_side == "yes" \
                              else (_gdp_now_cached - _at_strike)
                    if _at_gap <= 0.75:
                        continue
                    _at_date_m = _re.search(r"KXGDP-(\d{2})([A-Z]{3})(\d{2})", _at_ticker)
                    if not _at_date_m:
                        continue
                    _mo_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                    _at_mo = _mo_map.get(_at_date_m.group(2))
                    if not _at_mo:
                        continue
                    from datetime import datetime as _dt, timezone as _tz
                    _at_exp  = _dt(2000 + int(_at_date_m.group(1)),
                                   _at_mo, int(_at_date_m.group(3)), tzinfo=_tz.utc)
                    _at_days = (_at_exp - _dt.now(_tz.utc)).days
                    if 0 <= _at_days <= 14:
                        if not await bus._r.exists(f"ep:cut_loss:{_at_ticker}"):
                            log.warning(
                                "GDP CUT-LOSS: %s %s — GDPNow=%.2f%% is %.2f%% against "
                                "strike=%.1f%% with %d days left",
                                _at_ticker, _at_side.upper(),
                                _gdp_now_cached, _at_gap, _at_strike, _at_days,
                            )
                            await bus._r.set(
                                f"ep:cut_loss:{_at_ticker}",
                                f"GDPNow={_gdp_now_cached:.2f}% vs strike={_at_strike:.1f}% ({_at_side})",
                                ex=300,
                            )
                except Exception as _at_exc:
                    log.debug("GDP cut-loss check failed for %s: %s", _at_ticker, _at_exc)

            # ── VIX-based confidence gating for FOMC directional signals ─────────
            # High equity vol → wider uncertainty around Fed policy path → FOMC
            # model probabilities are less reliable.  Arb signals (monotonicity /
            # butterfly) are model-agnostic so they are exempted.
            _vix_now = float(current_vix or 0)
            if _vix_now >= 35:
                _vix_mult = 0.80
            elif _vix_now >= 25:
                _vix_mult = 0.90
            else:
                _vix_mult = 1.00

            if _vix_mult < 1.0:
                _vix_fomc_count = sum(
                    1 for s in signals
                    if s.category == "fomc"
                    and not s.model_source.endswith("_arb")
                )
                for s in signals:
                    if s.category == "fomc" and not s.model_source.endswith("_arb"):
                        s.confidence = round(s.confidence * _vix_mult, 3)
                log.info(
                    "VIX=%.1f → FOMC directional confidence scaled by %.2f× (%d signal(s))",
                    _vix_now, _vix_mult, _vix_fomc_count,
                )

            # ── MOVE bond-vol penalty (additive, stacks on top of VIX scaling) ──
            # MOVE > 120 reflects elevated rate uncertainty beyond what VIX captures;
            # apply an additional −0.05 confidence haircut on FOMC directional signals.
            _move_now = float(current_move or 0)
            if _move_now > 120:
                _move_fomc_count = 0
                for s in signals:
                    if s.category == "fomc" and not s.model_source.endswith("_arb"):
                        s.confidence = round(max(0.10, s.confidence - 0.05), 3)
                        _move_fomc_count += 1
                log.info(
                    "MOVE=%.1f > 120 → additional −0.05 FOMC directional confidence penalty (%d signal(s))",
                    _move_now, _move_fomc_count,
                )

            # ── SOFR rate fusion for FOMC directional signals ─────────────────
            # SOFR SR1 (1-month futures) implies near-term Fed policy.  When
            # the futures-implied rate diverges ≥ 15 bps from current fed_rate,
            # it corroborates the directional bias → cap-boost to 0.85 confidence.
            # Alignment: SOFR-implied higher → market expects hike → boosts YES-above.
            try:
                _sofr_raw = await bus._r.get("ep:sofr:sr1")
                if _sofr_raw:
                    _src_health.mark_ok("cme_sofr_sr1")
                    _sofr_data    = json.loads(_sofr_raw)
                    _sofr_implied = float(_sofr_data.get("implied_rate_pct", 0))
                    _sofr_delta   = _sofr_implied - current_fed_rate
                    _sofr_count   = 0
                    for s in signals:
                        if s.category != "fomc" or s.model_source.endswith("_arb"):
                            continue
                        _tup = s.ticker.upper()
                        if "ABOVE" in _tup and _sofr_delta >= 0.15:
                            s.confidence = round(min(0.85, s.confidence * 1.08), 3)
                            _sofr_count += 1
                        elif "BELOW" in _tup and _sofr_delta <= -0.15:
                            s.confidence = round(min(0.85, s.confidence * 1.08), 3)
                            _sofr_count += 1
                    if _sofr_count:
                        log.info(
                            "SOFR SR1 implied %.2f%% (Δ%+.2f vs fed_rate %.2f%%) "
                            "→ fused into %d FOMC directional signal(s)",
                            _sofr_implied, _sofr_delta, current_fed_rate, _sofr_count,
                        )
            except Exception as _sofr_exc:
                log.debug("SOFR fusion skipped: %s", _sofr_exc)

            # ── Treasury auction proximity suppression ────────────────────────
            # 10Y/30Y auctions create large duration supply that disrupts rate
            # markets; skip new FOMC directional signals within 24 h of such an
            # auction.  Arb and coherence signals are structural and are always kept.
            def _is_structural_signal(s) -> bool:
                ms = getattr(s, "model_source", "") or ""
                return ms.endswith("_arb") or "coherence" in ms
            try:
                _auct_raw = await bus._r.get("ep:treasury_auctions")
                if _auct_raw:
                    _auct_data  = json.loads(_auct_raw)
                    _suppressed = [
                        a for a in _auct_data.get("upcoming_24h", [])
                        if a.get("tenor") in ("10-Year", "30-Year")
                    ]
                    if _suppressed:
                        _auc_count = sum(
                            1 for s in signals
                            if s.category == "fomc" and not _is_structural_signal(s)
                        )
                        signals = [
                            s for s in signals
                            if not (s.category == "fomc"
                                    and not _is_structural_signal(s))
                        ]
                        _tickers_str = ", ".join(a.get("tenor", "?") for a in _suppressed)
                        log.warning(
                            "Treasury auction proximity: %s within 24h "
                            "— dropped %d FOMC directional signal(s), arb+coherence kept",
                            _tickers_str, _auc_count,
                        )
            except Exception as _auct_exc:
                log.debug("Treasury auction suppression skipped: %s", _auct_exc)

            # ── FOMC announcement day blackout ────────────────────────────────
            # On meeting days, Kalshi prices whipsaw around the 2pm ET announcement.
            # Suppress new FOMC directional signals for ±N hours around 18:00 UTC.
            # Arb and coherence signals are structural and are always kept.
            _blackout_hours = int(os.getenv("FOMC_ANNOUNCE_BLACKOUT_HOURS", "2"))
            if _blackout_hours > 0:
                import kalshi_bot.models.fomc as _fomc_mod
                _now_utc = datetime.now(timezone.utc)
                _today_str = _now_utc.strftime("%Y-%m-%d")
                _fomc_cal = _fomc_mod._FOMC_UPCOMING_LIVE if _fomc_mod._FOMC_UPCOMING_LIVE else _fomc_mod._FOMC_UPCOMING
                _ANNOUNCE_HOUR = 18  # 2pm ET/EDT = 18:00 UTC
                _in_blackout = (
                    _today_str in _fomc_cal
                    and abs(_now_utc.hour - _ANNOUNCE_HOUR) <= _blackout_hours
                )
                if _in_blackout:
                    _bl_count = sum(
                        1 for s in signals
                        if s.category == "fomc" and not _is_structural_signal(s)
                    )
                    signals = [
                        s for s in signals
                        if not (s.category == "fomc" and not _is_structural_signal(s))
                    ]
                    log.warning(
                        "FOMC announcement blackout active (%s ±%dh of %02d:00 UTC)"
                        " — suppressed %d directional signal(s), arb+coherence kept",
                        _today_str, _blackout_hours, _ANNOUNCE_HOUR, _bl_count,
                    )

            new_signals = [
                s for s in signals
                if s.ticker not in current_positions
                or (
                    getattr(s, "arb_partner", None)
                    and s.arb_partner not in current_positions
                )
            ]

            # ── Polymarket divergence signals ─────────────────────────────────
            # Refresh Polymarket cache (no-op if within CACHE_TTL=60s).
            # Generates Signal objects for Kalshi markets that diverge >4¢ from
            # their Polymarket peer — these flow through the same publish path below.
            await polymarket.refresh()
            _poly_sigs = polymarket.divergence_signals(signals)
            for _ps in _poly_sigs:
                if _ps.ticker not in current_positions:
                    new_signals.append(_ps)
            if _poly_sigs:
                log.info("Polymarket: %d divergence signal(s) added", len(_poly_sigs))

            # ── Econ consensus edge filter ─────────────────────────────────────
            # Drop economic signals where the indicator is within 0.3σ of consensus
            # — the market already priced this; no differentiated edge to capture.
            try:
                _cons_raw = await bus._r.get("ep:econ_consensus")
                if _cons_raw:
                    _cons_events = json.loads(_cons_raw).get("events", [])
                    if _cons_events:
                        _cons_pre  = len(new_signals)
                        _kept_sigs = []
                        for _s in new_signals:
                            if _s.category not in ("economic", "fomc_economic"):
                                _kept_sigs.append(_s)
                                continue
                            _t_up = _s.ticker.upper()
                            _match = next(
                                (e for e in _cons_events
                                 if e.get("indicator", "").upper() in _t_up
                                 or _t_up in e.get("indicator", "").upper()),
                                None,
                            )
                            if not _match:
                                _kept_sigs.append(_s)
                                continue
                            _dev_sigma = float(_match.get("deviation_sigma", 1.0))
                            if abs(_dev_sigma) < 0.3:
                                log.info(
                                    "Consensus filter: dropped %s (σ=%.2f, indicator=%s)",
                                    _s.ticker, _dev_sigma, _match.get("indicator"),
                                )
                            else:
                                _kept_sigs.append(_s)
                        new_signals = _kept_sigs
                        _removed = _cons_pre - len(new_signals)
                        if _removed:
                            log.info(
                                "Econ consensus filter: removed %d signal(s) within 0.3σ",
                                _removed,
                            )
            except Exception as _cons_exc:
                log.debug("Consensus filter skipped: %s", _cons_exc)

            # ── Improvement 5: Cross-meeting Bayes coherence arbitrage ────────
            def _market_mid(m: dict) -> float:
                yes = m.get("yes_price", 50)
                no  = m.get("no_price",  50)
                return (yes + (100 - no)) / 200

            try:
                from kalshi_bot.strategy import scan_cross_meeting_coherence
                _prices_for_coherence = {
                    m.get("ticker", ""): _market_mid(m) * 100
                    for m in markets_cache
                    if m.get("ticker", "")
                }
                _coherence_sigs = scan_cross_meeting_coherence(markets_cache, _prices_for_coherence)
                for _cs in _coherence_sigs:
                    if _cs.ticker not in current_positions:
                        await bus.publish_signal(_cs)
                        metrics.signal_published(_cs.asset_class, _cs.strategy, _cs.side)
                if _coherence_sigs:
                    log.info("Cross-meeting coherence: %d signal(s) published", len(_coherence_sigs))
            except Exception as _exc:
                log.warning("cross_meeting_coherence scan failed: %s", _exc)

            # ── Election market ensemble + Metaculus probability boost ───────────
            try:
                from kalshi_bot.strategy import scan_election_markets_with_538 as _scan_election
                _poly_prices_for_election = {
                    slug: float(p.get("probability") or p.get("price") or p.get("yes_price") or 0)
                    for slug, p in (polymarket._cache or {}).items()
                    if isinstance(p, dict)
                } if hasattr(polymarket, "_cache") and polymarket._cache else {}
                _pi_prices_for_election: dict = {}
                try:
                    from ep_predictit import fetch_predictit_fomc as _fetch_pi
                    _pi_raw = await _fetch_pi()
                    for _pi_key, _pi_outcomes in _pi_raw.items():
                        for _outcome, _price in _pi_outcomes.items():
                            _pi_prices_for_election[_pi_key] = float(_price)
                            break
                except Exception:
                    pass
                _election_sigs = await _scan_election(
                    markets_cache, _poly_prices_for_election, _pi_prices_for_election
                )
                for _es in _election_sigs:
                    if _es.ticker not in current_positions:
                        await bus.publish_signal(_es)
                        metrics.signal_published(_es.asset_class, _es.strategy, _es.side)
                if _election_sigs:
                    log.info("Election+metaculus: %d signal(s) published", len(_election_sigs))
            except Exception as _exc:
                log.warning("election_ensemble scan failed: %s", _exc)

            # ── PredictIt divergence scanner (Redis-cached) ───────────────────
            # Compare cached PredictIt prices to live Kalshi prices for Fed/macro
            # markets.  Generates divergence signals where spread ≥ 5¢.
            # Runs in parallel to Polymarket — catches cross-venue mispricings.
            try:
                _pi_cache_raw = await bus._r.get("ep:predictit:markets")
                if _pi_cache_raw:
                    _src_health.mark_ok("predictit")
                    _pi_markets   = json.loads(_pi_cache_raw).get("markets", [])
                    _pi_price_map: dict = {}
                    for _pim in _pi_markets:
                        _pi_yes = float(_pim.get("yes_price", 0) or 0)
                        if _pi_yes > 0:
                            _pi_key = _pim.get("name", "").upper().replace(" ", "_")
                            _pi_price_map[_pi_key] = _pi_yes

                    _pi_div_count = 0
                    _kalshi_mid_map: dict = {
                        m.get("ticker", ""): _market_mid(m) * 100
                        for m in markets_cache if m.get("ticker", "")
                    }
                    for _k_ticker, _k_price in _kalshi_mid_map.items():
                        _k_up = _k_ticker.upper()
                        if not any(kw in _k_up for kw in ("FED", "KXFED")):
                            continue
                        for _pi_slug, _pi_yes in _pi_price_map.items():
                            if not any(kw in _pi_slug for kw in ("FED", "RATE", "FOMC", "HIKE", "CUT")):
                                continue
                            _spread_c = abs(_k_price - _pi_yes)
                            if _spread_c < 5.0 or _k_ticker in current_positions:
                                continue
                            _side = "no" if _k_price > _pi_yes else "yes"
                            _pi_msg = SignalMessage(
                                ticker       = _k_ticker,
                                side         = _side,
                                confidence   = round(min(0.72, 0.60 + _spread_c / 200.0), 3),
                                edge         = round(_spread_c / 100.0, 4),
                                fair_value   = round(_pi_yes / 100.0, 4),
                                market_price = round(_k_price / 100.0, 4),
                                contracts    = 1,
                                strategy     = "predictit_divergence",
                                asset_class  = "fomc",
                                model_source = "predictit_divergence",
                                source_node  = NODE_ID,
                            )
                            new_signals.append(_pi_msg)
                            _pi_div_count += 1
                            break   # one signal per Kalshi ticker
                    if _pi_div_count:
                        log.info("PredictIt divergence: %d signal(s) added", _pi_div_count)
            except Exception as _pi_exc:
                log.debug("PredictIt divergence scanner skipped: %s", _pi_exc)

            # ── Improvement 8: BLS release pre-positioning ────────────────────
            try:
                from kalshi_bot.strategy import scan_bls_preposition
                # Pass current_positions as a best-effort guard for open bls_preposition count.
                # Encode existing bls_preposition positions as "bls_preposition:<ticker>" keys.
                _bls_prices: dict = {
                    m.get("ticker", ""): _market_mid(m) * 100
                    for m in markets_cache
                    if m.get("ticker", "")
                }
                for _pos_t, _pos_d in current_positions.items():
                    if (_pos_d.get("strategy") == "bls_preposition"
                            or "bls_preposition" in str(_pos_d.get("model_source", ""))):
                        _bls_prices[f"bls_preposition:{_pos_t}"] = 1
                _bls_sigs = scan_bls_preposition(markets_cache, _bls_prices)
                for _bs in _bls_sigs:
                    if _bs.ticker not in current_positions:
                        await bus.publish_signal(_bs)
                        metrics.signal_published(_bs.asset_class, _bs.strategy, _bs.side)
                if _bls_sigs:
                    log.info("BLS pre-position: %d strangle leg(s) published", len(_bls_sigs))
            except Exception as _exc:
                log.warning("bls_preposition scan failed: %s", _exc)

            # ── BTC datasource pre-reads (cross-exchange gate + Deribit skew) ──
            _btc_spread_bps    = 0.0
            _btc_spread_gate   = True   # True = signals allowed
            _deribit_skew_pct  = 0.0
            try:
                _cx_raw = await bus._r.get("ep:btc:cross_exchange")
                if _cx_raw:
                    _cx_data         = json.loads(_cx_raw)
                    _btc_spread_bps  = float(_cx_data.get("spread_bps", 0))
                    if _btc_spread_bps > 15.0:
                        _btc_spread_gate = False
                        log.warning(
                            "BTC cross-exchange spread=%.1f bps > 15 "
                            "— mean-reversion signals suppressed (fragmented market)",
                            _btc_spread_bps,
                        )
            except Exception as _cx_exc:
                log.debug("BTC cross-exchange gate read failed: %s", _cx_exc)
            try:
                _deribit_raw = await bus._r.get("ep:deribit:skew")
                if _deribit_raw:
                    _d_payload = json.loads(_deribit_raw)
                    # Freshness check: skew is only useful if recently fetched.
                    # Redis TTL (10 min) caps maximum age, but 10 min is still
                    # old for options-market sentiment; tighten to 5 min here.
                    _d_ts_s = _d_payload.get("ts")
                    if _d_ts_s is not None:
                        _d_age = time.time() - float(_d_ts_s)
                        if _d_age > 300:
                            log.debug(
                                "Deribit skew %.0fs old — skipping adjustment", _d_age
                            )
                        else:
                            _deribit_skew_pct = float(_d_payload.get("skew_pct", 0))
                    else:
                        _deribit_skew_pct = float(_d_payload.get("skew_pct", 0))
            except Exception as _der_exc:
                log.debug("Deribit skew read failed: %s", _der_exc)

            # ── BTC mean-reversion signals ────────────────────────────────────
            if btc_strategy:
                # Read LLM policy overrides from Redis and apply to strategy
                rsi_os_str = await bus.get_config_override("llm_rsi_oversold")
                rsi_ob_str = await bus.get_config_override("llm_rsi_overbought")
                z_str      = await bus.get_config_override("llm_z_threshold")
                if rsi_os_str:
                    btc_strategy.rsi_os  = float(rsi_os_str)
                if rsi_ob_str:
                    btc_strategy.rsi_ob  = float(rsi_ob_str)

                # ── Vol-adjusted BTC z_thresh ──────────────────────────────────
                # LLM policy sets the strategic baseline; vol_mult (computed above
                # from the price buffer) scales it upward in volatile conditions so
                # we only enter mean-reversion trades on truly extreme dislocations.
                _z_base = float(z_str) if z_str else _btc_z_base
                btc_strategy.z_thresh = round(_z_base * vol_mult, 2)
                if vol_regime != "normal" and vol_regime != "insufficient_data":
                    log.debug(
                        "Vol-adj z_thresh: %.2f  (base=%.2f  mult=%.2f  regime=%s)",
                        btc_strategy.z_thresh, _z_base, vol_mult, vol_regime,
                    )

                try:
                    btc_msgs: List[SignalMessage] = await asyncio.wait_for(
                        btc_strategy.generate(), timeout=30.0
                    )
                    # Dedup: skip BTC-USD if already held
                    new_btc = [m for m in btc_msgs if m.ticker not in current_positions]

                    # Gate: suppress signals when cross-exchange spread too wide
                    if not _btc_spread_gate and new_btc:
                        log.info(
                            "BTC spread gate: suppressed %d signal(s) (spread=%.1f bps)",
                            len(new_btc), _btc_spread_bps,
                        )
                        new_btc = []

                    # Deribit skew: adjust BTC signal confidence based on options market
                    # Positive skew (put IV > call IV) → bearish tilt → penalize YES signals
                    if abs(_deribit_skew_pct) >= 3.0 and new_btc:
                        for _bm in new_btc:
                            if _deribit_skew_pct > 5.0 and _bm.side == "yes":
                                _bm.confidence = round(_bm.confidence * 0.90, 3)
                            elif _deribit_skew_pct < -3.0 and _bm.side == "no":
                                _bm.confidence = round(_bm.confidence * 0.95, 3)
                        log.debug(
                            "Deribit skew=%.1f%% applied to %d BTC signal(s)",
                            _deribit_skew_pct, len(new_btc),
                        )

                    # Publish BTC price + indicators to Redis for Exec exit checks
                    if btc_strategy.last_spot:
                        await bus._r.hset(EP_PRICES, "BTC-USD", json.dumps({
                            "last_price":  btc_strategy.last_spot,
                            "yes_price":   btc_strategy.last_spot,
                            "no_price":    btc_strategy.last_spot,
                            "spread":      0,
                            "btc_z_score": btc_strategy.last_z or 0.0,
                            "btc_rsi":     btc_strategy.last_rsi or 50.0,
                            "btc_mid_bb":  btc_strategy.last_bb_mid or 0.0,
                            "ts_us":       int(time.time() * 1_000_000),
                        }))
                        # Rolling history for dashboard price chart
                        await bus.push_btc_history(
                            btc_strategy.last_spot,
                            btc_strategy.last_rsi,
                            btc_strategy.last_z,
                        )
                        metrics.update_btc(
                            price = btc_strategy.last_spot,
                            rsi   = btc_strategy.last_rsi,
                            z     = btc_strategy.last_z,
                        )
                        # Feed the vol-threshold buffer (1-cycle lag is intentional)
                        _btc_price_buf.append(btc_strategy.last_spot)

                    # Publish BTC signals directly (already SignalMessage objects)
                    for msg in new_btc:
                        try:
                            await bus.publish_signal(msg)
                            metrics.signal_published(msg.asset_class, msg.strategy, msg.side)
                        except Exception as exc:
                            log.warning("Failed to publish BTC signal: %s", exc)

                    if new_btc:
                        log.info("Intel: published %d BTC signal(s)", len(new_btc))

                except Exception:
                    log.exception("BTC signal generation failed.")

            # ── Publish Kalshi signals to Redis ───────────────────────────────
            # Pre-build close_time lookup from both the generic market cache and
            # the FOMC-specific cache (KXFED tickers come from a separate targeted
            # fetch, not the generic scan_all_markets page).
            close_time_map = {
                m["ticker"]: m.get("close_time") or m.get("expiration_time")
                for m in (*markets_cache, *fomc_cache)
            }

            # ── Priority ordering: arb first, coherence second, directional last ─
            def _signal_priority(s) -> int:
                ms = getattr(s, "model_source", "") or ""
                if (getattr(s, "category", "") == "arb"
                        or getattr(s, "arb_legs", None) is not None
                        or ms.endswith("_arb")):
                    return 1
                if "coherence" in ms:
                    return 2
                return 3

            new_signals.sort(key=_signal_priority)

            published = 0
            for sig in new_signals:
                try:
                    msg = kalshi_signal_to_message(sig, NODE_ID)
                    msg.close_time = close_time_map.get(sig.ticker)
                    # Fallback: fetch close_time per-market if not in cache
                    if not msg.close_time:
                        try:
                            _mkt_detail = client.get(f"/markets/{sig.ticker}")
                            _mkt = _mkt_detail.get("market", {})
                            msg.close_time = _mkt.get("close_time") or _mkt.get("expiration_time")
                        except Exception:
                            pass

                    msg.priority = _signal_priority(sig)

                    # ── Fix 1: adjust edge to ask price, not mid ───────────────
                    # market_price is the mid; actual fill costs the ask.
                    # Approximation: ask ≈ mid + half_spread, so edge shrinks by
                    # spread_cents / 200 (half the spread converted to 0–1 scale).
                    if msg.spread_cents is not None:
                        half_spread = msg.spread_cents / 200.0
                        if msg.side == "yes":
                            # YES fill at ask (higher than mid) → edge shrinks
                            msg.edge = msg.fair_value - (msg.market_price + half_spread)
                        else:
                            # NO fill at no_ask = 1 - yes_bid; yes_bid ≈ mid - half_spread
                            # edge = (1 - fair_value) - (1 - yes_bid)
                            #      = yes_bid - fair_value
                            #      ≈ (market_price - half_spread) - fair_value
                            msg.edge = (msg.market_price - half_spread) - msg.fair_value

                    # ── Fix 2a: drop negative edge after ask-price adjustment ──
                    # The ask-price adjustment can invert a positive Signal.edge
                    # (e.g. wide spread or stale mid price).  Drop rather than
                    # let schema validation catch it on the exec node.
                    if msg.edge <= 0:
                        log.debug(
                            "Ask-adjust killed edge: %s side=%s fv=%.4f mp=%.4f "
                            "spread=%s → edge=%.4f — dropping",
                            msg.ticker, msg.side, msg.fair_value, msg.market_price,
                            msg.spread_cents, msg.edge,
                        )
                        continue

                    # ── Fix 2b: spread-to-edge filter ─────────────────────────
                    # Skip signals where the spread is wider than the edge —
                    # guaranteed-negative-EV after crossing the spread.
                    if (
                        msg.spread_cents is not None
                        and msg.spread_cents > msg.edge * 100
                    ):
                        log.debug(
                            "Spread>edge filter: %s  spread=%d¢  edge=%.0f¢ — skipping",
                            msg.ticker, msg.spread_cents, msg.edge * 100,
                        )
                        continue

                    # ── Fix 2c: fallback-only data quality gate ────────────────
                    # When fomc.py marks a signal as produced from fallback sources
                    # only (e.g. FRED anchor with no live market data), require a
                    # much higher edge to avoid publishing spurious FRED-disagreement
                    # artefacts.  The field may be absent on signals from other
                    # agents — use .get() so this never raises.
                    _dq = getattr(msg, "data_quality", None) or (
                        msg.__dict__.get("data_quality") if hasattr(msg, "__dict__") else None
                    )
                    if _dq == "fallback_only" and msg.edge < cfg.FALLBACK_ONLY_EDGE_THRESHOLD:
                        log.info(
                            "Fallback-only gate: %s side=%s edge=%.3f < %.2f (FALLBACK_ONLY_EDGE_THRESHOLD) — skipping",
                            msg.ticker, msg.side, msg.edge, cfg.FALLBACK_ONLY_EDGE_THRESHOLD,
                        )
                        continue

                    eid = await bus.publish_signal(msg)
                    if not eid:
                        log.warning(
                            "Signal publish returned no entry ID for %s — Redis may be full",
                            msg.ticker,
                        )
                        continue
                    metrics.signal_published(msg.asset_class, msg.strategy, msg.side)
                    summary.record(sig, executed=False)   # Intel just publishes
                    # ── Fix 3: correlation ID in publish log ──────────────────
                    log.debug(
                        "Signal published: %s side=%s edge=%.3f signal_id=%.8s",
                        msg.ticker, msg.side, msg.edge, msg.signal_id,
                    )
                    published += 1
                except Exception as exc:
                    log.warning("Failed to publish %s: %s", sig.ticker, exc)

            already_held = len(signals) - len(new_signals)
            if published:
                log.info(
                    "Intel: published %d Kalshi signal(s)  (%d total, %d deduped)",
                    published, len(signals), already_held,
                )

            # Zero-signal watchdog: warn when a scanner has been consistently silent.
            # Helps detect dead/misconfigured scanners early without constant log spam.
            _sig_cats = {s.category for s in signals}
            for _cat, _cnt in list(_zero_sig_counts.items()):
                if _cat in _sig_cats:
                    _zero_sig_counts[_cat] = 0
                else:
                    _zero_sig_counts[_cat] += 1
                    if _zero_sig_counts[_cat] == _ZERO_SIG_WARN:
                        log.warning(
                            "SCANNER SILENT: '%s' has produced 0 signals for %d consecutive "
                            "cycles (~%d min) — scanner may be disabled, misconfigured, or "
                            "market has no edge. Check ENABLE_%s env var.",
                            _cat, _ZERO_SIG_WARN, _ZERO_SIG_WARN * 2,
                            _cat.upper().replace("_", "_"),
                        )
                        _zero_sig_counts[_cat] = 0  # reset so warning fires again after another N cycles

            # ── Fix 4: exec peer liveness check ──────────────────────────────
            # The exec node publishes HEARTBEAT to ep:system every 60s.
            # Alert if no heartbeat has been seen in the last 120s (2× interval).
            try:
                _exec_hb_ts = await bus.get_latest_heartbeat("exec")
                if _exec_hb_ts is None:
                    log.warning("EXEC PEER SILENT: no HEARTBEAT from exec node found in ep:system")
                elif (time.time() - _exec_hb_ts) > 120:
                    _exec_age = int(time.time() - _exec_hb_ts)
                    log.warning(
                        "EXEC PEER SILENT: last exec heartbeat was %ds ago (threshold=120s)",
                        _exec_age,
                    )
            except Exception:
                pass  # liveness check is non-fatal

            # ── Open positions count for metrics ──────────────────────────────
            metrics.update_positions(len(current_positions))

            # ── Drain execution reports → log fills + update metrics ──────────
            # Tally rejection reasons for a one-line cycle summary (reduces log spam
            # from dozens of DUPLICATE/RISK_GATE entries).
            reports     = await bus.consume_executions(intel_consumer)
            reject_tally: dict[str, int] = {}
            for r in reports:
                metrics.execution_received(
                    r.status, r.asset_class,
                    reason=r.reject_reason or "",
                )
                if r.status == "filled":
                    metrics.add_pnl(r.edge_captured)
                    metrics.record_fill_latency(
                        r.asset_class,
                        (int(time.time() * 1_000_000) - r.ts_us) / 1_000_000,
                    )
                    log.info("Fill confirmed: %s %s ×%d @ %.4f  order=%s",
                             r.ticker, r.side, r.contracts, r.fill_price, r.order_id)
                elif r.status == "rejected":
                    reason = r.reject_reason or "UNKNOWN"
                    reject_tally[reason] = reject_tally.get(reason, 0) + 1

            if reject_tally:
                # Surface non-trivial rejections (anything except pure DUPLICATE noise)
                non_dup = {k: v for k, v in reject_tally.items() if k != "DUPLICATE"}
                if non_dup:
                    log.info("Exec rejections this cycle: %s", non_dup)
                else:
                    log.debug("Exec rejections this cycle: %s", reject_tally)

            # ── Daily summary at ~22:00 UTC ───────────────────────────────────
            # Send once per calendar day when the UTC hour reaches 22.
            # Reads ep:performance from Redis (written by ep_metrics / exec fills).
            try:
                global _last_daily_summary_day
                _now_utc_ds = datetime.now(timezone.utc)
                _today_str  = _now_utc_ds.date().isoformat()
                if (
                    _now_utc_ds.hour == 22
                    and _last_daily_summary_day != _today_str
                ):
                    _perf: dict = {}
                    try:
                        _perf_raw = await bus._r.hgetall("ep:performance")
                        _perf     = {k: v for k, v in (_perf_raw or {}).items()}
                    except Exception as _perf_exc:
                        log.debug("ep:performance read failed: %s", _perf_exc)

                    _pnl_cents      = int(float(_perf.get("pnl_cents",      0) or 0))
                    _trades         = int(float(_perf.get("trades",         0) or 0))
                    _win_rate       = float(_perf.get("win_rate",           0) or 0)
                    _open_positions = len(current_positions)

                    _ok = await _telegram.send_daily_summary(
                        pnl_cents      = _pnl_cents,
                        trades         = _trades,
                        win_rate       = _win_rate,
                        open_positions = _open_positions,
                    )
                    _alert_manager.send_daily_summary()
                    if _ok:
                        _last_daily_summary_day = _today_str
                        log.info(
                            "Daily summary sent: pnl=%+d¢  trades=%d  "
                            "win_rate=%.1f%%  open=%d",
                            _pnl_cents, _trades, _win_rate, _open_positions,
                        )
                    else:
                        log.debug("Daily summary send returned False (Telegram disabled or error)")
            except Exception as _ds_exc:
                log.debug("Daily summary block failed (non-fatal): %s", _ds_exc)

            # ── Hourly performance summary → Redis ep:performance ─────────────
            # Intel reads from its local trades CSV (which is empty — trades only
            # happen on the exec node).  Only publish if we actually have data;
            # otherwise let the exec node's _performance_publisher_loop own the key.
            _now_epoch = time.time()
            if _now_epoch - _last_perf_publish >= 3600:
                try:
                    _perf = await get_performance_summary(days=30)
                    if _perf["total_trades"] > 0:
                        await bus._r.set("ep:performance", json.dumps(_perf), ex=90000)
                    _win_pct    = _perf["win_rate"] * 100
                    _pnl_dollar = _perf["total_pnl_cents"] / 100
                    _sharpe_str = (
                        f"{_perf['sharpe_daily']:.2f}"
                        if _perf["sharpe_daily"] is not None else "N/A"
                    )
                    log.info(
                        "Performance (30d): win_rate=%.1f%% pnl=%+.2f$ "
                        "trades=%d sharpe=%s",
                        _win_pct, _pnl_dollar, _perf["total_trades"], _sharpe_str,
                    )
                    _last_perf_publish = _now_epoch
                except Exception:
                    log.debug("Performance summary publish failed (non-fatal).")

            # ── Cycle timing ──────────────────────────────────────────────────
            elapsed = time.monotonic() - cycle_start
            metrics.observe_cycle(elapsed)
            sleep_s = max(0.0, cfg.POLL_INTERVAL - elapsed)
            log.info("Intel cycle %.1fs — sleeping %.0fs", elapsed, sleep_s)
            await asyncio.sleep(sleep_s)

    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Intel loop cancelled.")
    finally:
        heartbeat_task.cancel()
        release_monitor_task.cancel()
        exec_watchdog_task.cancel()
        price_gap_fill_task.cancel()
        await asyncio.gather(
            heartbeat_task, release_monitor_task, exec_watchdog_task,
            price_gap_fill_task, return_exceptions=True,
        )
        ws.stop()
        await bus.publish_system_event("INTEL_STOP")
        await bus.close()
        await stop_audit_writer()
        summary.print_summary()
        log.info("Intel node shutdown complete.")


if __name__ == "__main__":
    import signal as _signal

    async def _run():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(intel_main())

        def _handle_sigterm():
            log.info("SIGTERM received — initiating graceful shutdown")
            task.cancel()

        loop.add_signal_handler(_signal.SIGTERM, _handle_sigterm)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
