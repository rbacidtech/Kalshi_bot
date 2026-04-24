"""
ep_datasources.py — 8 independent market data feeds, Redis-cached with per-source TTLs.

Each fetcher is fully self-contained: one network call, one Redis write, graceful
failure (logs WARNING, skips write, keeps stale cache until TTL expires naturally).

Redis keys and TTLs:
  ep:sofr:sr1             5 min    1-month SOFR futures implied rate (Yahoo Finance SR1=F — verify still listed)
  ep:sofr:sr3             5 min    3-month SOFR futures implied rate (Yahoo Finance SR3=F — DELISTED ~2024; returns None)
  ep:treasury_auctions    24 h     Upcoming 10Y/30Y auction within 24 h
  ep:econ_consensus       1 h      CPI/NFP/PCE/GDP consensus forecasts (TradingEconomics)
  ep:deribit:skew         10 min   DVOL + 25d put-call skew (Deribit public API)
  ep:btc:cross_exchange   2 min    BTC prices across Coinbase/Binance/Kraken/Bitstamp
  ep:macro:walcl          24 h     Fed balance sheet (WALCL), 4-week trend (FRED)
  ep:macro:baa10y         1 h      Moody's Baa-10Y credit spread (FRED)
  ep:predictit:markets    5 min    PredictIt: Fed rate + economic markets

Required env vars:
  REDIS_URL               (required)
  FRED_API_KEY            (required for WALCL, BAA10Y)
  TE_API_KEY              (optional — Trading Economics; econ_consensus skipped without it)

Usage:
  python3 ep_datasources.py          # refresh all stale sources once, then exit
  python3 ep_datasources.py --loop   # continuous daemon (checks every 60 s)
"""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from dotenv import load_dotenv
load_dotenv()

from ep_config import log

# ── Config ─────────────────────────────────────────────────────────────────────
REDIS_URL        = os.getenv("REDIS_URL",      "redis://localhost:6379/0")
FRED_KEY         = os.getenv("FRED_API_KEY",   "")
TE_KEY           = os.getenv("TE_API_KEY",     "")
LOOP_INTERVAL_S  = int(os.getenv("DATASOURCES_INTERVAL_S", "60"))
# Back-off window after a FRED 429/403. Demo keys are 120 req/day so a hit
# means we're saturating; 1h cool-off prevents hammering and restores after.
FRED_BACKOFF_S   = int(os.getenv("FRED_BACKOFF_S", "3600"))

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

# Shared FRED back-off state. Mutable dict so _fred can update from any
# enclosing coroutine. `ts` is the wall-clock second until which FRED calls
# should be suppressed. 0 = not in back-off.
_FRED_BACKOFF_UNTIL: Dict[str, float] = {"ts": 0.0}

# ── Redis keys & TTLs ──────────────────────────────────────────────────────────
_SOURCES: List[tuple] = [
    # (redis_key,               ttl_seconds, fetcher_name)
    ("ep:sofr:sr1",             300,   "sofr_sr1"),
    ("ep:sofr:sr3",             300,   "sofr_sr3"),
    ("ep:treasury_auctions",    86400, "treasury_auctions"),
    ("ep:econ_consensus",       3600,  "econ_consensus"),
    ("ep:deribit:skew",         600,   "deribit"),
    ("ep:btc:cross_exchange",   120,   "btc_cross"),
    ("ep:macro:walcl",          86400, "walcl"),
    ("ep:macro:baa10y",         3600,  "baa10y"),
    ("ep:predictit:markets",    300,   "predictit"),
]

_KEY_BY_NAME = {name: key for key, _, name in _SOURCES}
_TTL_BY_KEY  = {key: ttl  for key, ttl, _ in _SOURCES}


# ── DataSourceManager ──────────────────────────────────────────────────────────

class DataSourceManager:

    def __init__(self, redis_url: str = REDIS_URL):
        self._url = redis_url
        self._r   = None

    async def connect(self) -> None:
        import redis.asyncio as aioredis
        self._r = await aioredis.from_url(
            self._url,
            encoding               = "utf-8",
            decode_responses       = True,
            socket_connect_timeout = 5,
            socket_timeout         = 10,
            retry_on_timeout       = True,
        )

    async def close(self) -> None:
        if self._r:
            await self._r.aclose()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _is_stale(self, key: str) -> bool:
        """True when the Redis key is absent (expired or never written)."""
        try:
            return not bool(await self._r.exists(key))
        except Exception:
            return True

    async def _write(self, key: str, data: dict, ttl: int) -> None:
        payload = json.dumps({
            **data,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "ts":         int(time.time()),
        })
        await self._r.set(key, payload, ex=ttl)

    async def _fred(self, series_id: str, limit: int = 5) -> Optional[List[dict]]:
        """Fetch FRED observations, newest first. Returns None on any error.

        FRED demo keys are rate-limited to ~120 req/day. Production keys get
        ~1000/day. On 429 / 403 we back off (in-memory) so a rate-limit event
        doesn't silently skip writes forever — the back-off ends at the next
        `refresh_all` cycle after FRED_BACKOFF_S seconds.
        """
        if not FRED_KEY:
            log.debug("FRED_API_KEY not set — skipping %s", series_id)
            return None
        # Module-level back-off timestamp — if we got a 429/403 recently, skip.
        _now = time.time()
        if _now < _FRED_BACKOFF_UNTIL["ts"]:
            log.debug("FRED %s skipped — in back-off for %.0fs",
                      series_id, _FRED_BACKOFF_UNTIL["ts"] - _now)
            return None
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={FRED_KEY}"
            f"&sort_order=desc&limit={limit}&file_type=json"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url)
            if r.status_code == 200:
                return [o for o in r.json().get("observations", [])
                        if o.get("value", ".") != "."]
            if r.status_code in (429, 403):
                _FRED_BACKOFF_UNTIL["ts"] = _now + FRED_BACKOFF_S
                log.warning(
                    "FRED rate-limit / forbidden (status=%d) on %s — "
                    "backing off %ds for all FRED series",
                    r.status_code, series_id, FRED_BACKOFF_S,
                )
        except Exception as exc:
            log.warning("FRED %s error: %s", series_id, exc)
        return None

    # ── Fetchers ──────────────────────────────────────────────────────────────

    async def _fetch_sofr_sr1(self) -> Optional[dict]:
        """1-month SOFR futures (SR1=F via Yahoo Finance). Implied rate = 100 - price. Returns None on fetch failure."""
        try:
            async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": _UA}) as c:
                r = await c.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/SR1=F",
                    params={"interval": "1d", "range": "5d"},
                )
            if r.status_code != 200:
                return None
            meta  = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev  = meta.get("previousClose")
            if not price:
                return None
            return {
                "symbol":           "SR1=F",
                "price":            round(float(price), 4),
                "prev_close":       round(float(prev), 4) if prev else None,
                "implied_rate_pct": round(100.0 - float(price), 4),
                "day_change_bp":    round((float(price) - float(prev)) * 100, 1) if prev else None,
            }
        except Exception as exc:
            log.warning("SOFR SR1 fetch error: %s", exc)
            return None

    async def _fetch_sofr_sr3(self) -> Optional[dict]:
        """3-month SOFR futures (SR3=F via Yahoo Finance).

        SR3=F was delisted from Yahoo Finance ~2024. Previously this fetcher
        ran every cycle, hit the Yahoo endpoint, got an empty response, and
        returned None — burning ~8s of timeout and adding noise to logs. Now
        returns None immediately without any network call. Slot preserved so
        the name still appears in the refresh-all summary; flip ENABLE_SR3
        to true and add the fetcher body back when/if the symbol is relisted.
        """
        if os.getenv("ENABLE_SR3", "false").lower() not in ("1", "true", "yes"):
            return None
        # Guarded-off path below retained for forward compatibility.
        try:
            async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": _UA}) as c:
                r = await c.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/SR3=F",
                    params={"interval": "1d", "range": "5d"},
                )
            if r.status_code != 200:
                return None
            meta  = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev  = meta.get("previousClose")
            if not price:
                return None
            return {
                "symbol":           "SR3=F",
                "price":            round(float(price), 4),
                "prev_close":       round(float(prev), 4) if prev else None,
                "implied_rate_pct": round(100.0 - float(price), 4),
                "day_change_bp":    round((float(price) - float(prev)) * 100, 1) if prev else None,
            }
        except Exception as exc:
            log.warning("SOFR SR3 fetch error: %s", exc)
            return None

    async def _fetch_treasury_auctions(self) -> Optional[dict]:
        """
        TreasuryDirect upcoming auction calendar.
        Flags any 10Y Note or 30Y Bond auction within the next 24 hours.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://www.treasurydirect.gov/TA_WS/securities/upcoming",
                    params={"format": "json"},
                )
            if r.status_code != 200:
                return None

            now       = datetime.now(timezone.utc)
            horizon   = now + timedelta(hours=24)
            upcoming  = []
            all_items = r.json() if isinstance(r.json(), list) else []

            for item in all_items:
                # auctionDate format: "2025-04-23T00:00:00"
                raw_date = item.get("auctionDate", "")
                if not raw_date:
                    continue
                try:
                    auction_dt = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if auction_dt < now or auction_dt > horizon:
                    continue

                term   = item.get("term", "").upper()
                s_type = item.get("securityType", "").upper()
                # Keep 10Y notes, 30Y bonds, and 2Y/5Y notes (rate-sensitive)
                if not any(t in term for t in ("10-YEAR", "30-YEAR", "2-YEAR", "5-YEAR")):
                    continue
                upcoming.append({
                    "term":            item.get("term"),
                    "type":            s_type,
                    "auction_date":    raw_date,
                    "offering_amount": item.get("offeringAmount"),
                    "cusip":           item.get("cusip", ""),
                    "hours_away":      round((auction_dt - now).total_seconds() / 3600, 1),
                })

            return {
                "auctions_within_24h": upcoming,
                "has_10y_or_30y":      any(
                    "10-YEAR" in a["term"].upper() or "30-YEAR" in a["term"].upper()
                    for a in upcoming
                ),
                "count": len(upcoming),
            }
        except Exception as exc:
            log.warning("Treasury auctions fetch error: %s", exc)
            return None

    async def _fetch_econ_consensus(self) -> Optional[dict]:
        """
        Trading Economics economic calendar with consensus forecasts.
        Requires TE_API_KEY env var.  Falls back to guest:guest (limited quota).
        Filters: US events with forecasts, next 7 days, high-importance only.
        """
        api_key = TE_KEY or "guest:guest"
        if not TE_KEY:
            # Log the fallback once per instance so operators notice the quota
            # degradation. Guest is 500–1000 req/month; easy to exhaust.
            if not getattr(self, "_te_guest_warned", False):
                log.info(
                    "TE_API_KEY unset — falling back to guest:guest (limited quota). "
                    "Set TE_API_KEY to restore full quota."
                )
                self._te_guest_warned = True
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get(
                    "https://api.tradingeconomics.com/calendar/country/united%20states",
                    params={"c": api_key, "f": "json"},
                )
            if r.status_code == 401:
                if not TE_KEY:
                    log.debug("TE econ_consensus: TE_API_KEY not set — skipping")
                else:
                    log.warning("TE econ_consensus: API key rejected (401)")
                return None
            if r.status_code != 200:
                return None

            now     = datetime.now(timezone.utc)
            horizon = now + timedelta(days=7)
            events  = []
            # Indicators we care about
            targets = {"cpi", "nfp", "pce", "gdp", "ppi", "retail", "payroll",
                       "unemployment", "jobs", "fomc", "fed", "rate decision"}

            for ev in r.json():
                ev_date_raw = ev.get("Date", "")
                try:
                    ev_dt = datetime.fromisoformat(ev_date_raw.replace("Z", "+00:00"))
                    if not ev_dt.tzinfo:
                        ev_dt = ev_dt.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    continue
                if ev_dt < now or ev_dt > horizon:
                    continue
                # Keep only events matching target indicators
                name_lower = (ev.get("Event") or "").lower()
                if not any(t in name_lower for t in targets):
                    continue

                events.append({
                    "event":      ev.get("Event"),
                    "date":       ev_date_raw,
                    "actual":     ev.get("Actual"),
                    "previous":   ev.get("Previous"),
                    "forecast":   ev.get("Forecast") or ev.get("TEForecast"),
                    "importance": ev.get("Importance"),
                    "unit":       ev.get("Unit"),
                })

            return {"events": events, "count": len(events), "source": "trading_economics"}
        except Exception as exc:
            log.warning("Econ consensus fetch error: %s", exc)
            return None

    async def _fetch_deribit(self) -> Optional[dict]:
        """
        Deribit public API:
          - DVOL: BTC implied volatility index
          - 25-delta skew: avg OTM put mark_iv − avg OTM call mark_iv (7-60 DTE window)

        Positive skew = puts bid up vs calls → bearish/hedging demand.
        """
        def _parse_expiry(instrument_name: str) -> Optional[datetime]:
            parts = instrument_name.split("-")
            if len(parts) < 3:
                return None
            try:
                return datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc)
            except ValueError:
                return None

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                dvol_r, spot_r, opts_r = await asyncio.gather(
                    client.get("https://www.deribit.com/api/v2/public/get_index_price",
                               params={"index_name": "dvol_btc"}),
                    client.get("https://www.deribit.com/api/v2/public/get_index_price",
                               params={"index_name": "btc_usd"}),
                    client.get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
                               params={"currency": "BTC", "kind": "option"}),
                )

            dvol  = (dvol_r.json().get("result") or {}).get("index_price")
            spot  = (spot_r.json().get("result") or {}).get("index_price")
            opts  = (opts_r.json().get("result") or [])

        except Exception as exc:
            log.warning("Deribit fetch error: %s", exc)
            return None

        # ── Skew computation ─────────────────────────────────────────────────
        put_ivs:  List[float] = []
        call_ivs: List[float] = []
        now = datetime.now(timezone.utc)

        for o in opts:
            name     = o.get("instrument_name", "")
            mark_iv  = o.get("mark_iv", 0.0) or 0.0
            strike_s = name.split("-")[2] if len(name.split("-")) >= 4 else ""
            opt_type = name[-1] if name else ""

            if mark_iv < 0.5:                            # skip illiquid / zero-iv
                continue
            expiry = _parse_expiry(name)
            if expiry is None:
                continue
            dte = (expiry - now).days
            if not (7 <= dte <= 60):
                continue
            try:
                strike = float(strike_s)
            except ValueError:
                continue
            if spot and spot > 0:
                moneyness = strike / spot
                if not (0.82 <= moneyness <= 1.18):      # ±18% band → near-OTM
                    continue

            if opt_type == "P":
                put_ivs.append(mark_iv)
            elif opt_type == "C":
                call_ivs.append(mark_iv)

        skew = None
        if put_ivs and call_ivs:
            skew = round(sum(put_ivs) / len(put_ivs) - sum(call_ivs) / len(call_ivs), 3)

        # Rollover blindness guard: Deribit rolls option expiries at midnight
        # UTC. During the ~60s rollover window, the 7-60 DTE band can be empty
        # or heavily biased. Skip the write so the previous cache value (valid
        # up to its 10-min TTL) remains the latest reading. Consumers tolerate
        # a missing payload (ep_intel:2431 already has an age check).
        if len(put_ivs) + len(call_ivs) == 0:
            log.warning(
                "Deribit skew: 0 options in 7-60 DTE band (rollover?) — "
                "skipping write, prior cache persists until TTL"
            )
            return None

        return {
            "dvol":              round(float(dvol), 3) if dvol else None,
            "btc_spot":          round(float(spot), 2) if spot else None,
            "put_call_skew":     skew,   # positive = puts expensive vs calls
            "put_iv_avg":        round(sum(put_ivs)  / len(put_ivs),  3) if put_ivs  else None,
            "call_iv_avg":       round(sum(call_ivs) / len(call_ivs), 3) if call_ivs else None,
            "options_in_window": len(put_ivs) + len(call_ivs),
        }

    async def _fetch_btc_cross_exchange(self) -> Optional[dict]:
        """
        BTC/USD spot prices from Coinbase, Binance, Kraken, Bitstamp.
        Computes min, max, spread_bps, and which exchange is cheapest/most expensive.
        """
        async def _coinbase(c: httpx.AsyncClient) -> Optional[float]:
            r = await c.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            return float(r.json()["data"]["amount"])

        async def _binance(c: httpx.AsyncClient) -> Optional[float]:
            r = await c.get("https://api.binance.com/api/v3/ticker/price",
                            params={"symbol": "BTCUSDT"})
            return float(r.json()["price"])

        async def _kraken(c: httpx.AsyncClient) -> Optional[float]:
            r = await c.get("https://api.kraken.com/0/public/Ticker",
                            params={"pair": "XBTUSD"})
            result = r.json().get("result", {})
            ticker = next(iter(result.values()), {})
            return float(ticker["c"][0])   # last trade price

        async def _bitstamp(c: httpx.AsyncClient) -> Optional[float]:
            r = await c.get("https://www.bitstamp.net/api/v2/ticker/btcusd/")
            return float(r.json()["last"])

        prices: Dict[str, Optional[float]] = {}
        async with httpx.AsyncClient(timeout=8.0) as client:
            results = await asyncio.gather(
                _coinbase(client),
                _binance(client),
                _kraken(client),
                _bitstamp(client),
                return_exceptions=True,
            )
        for exchange, result in zip(
            ("coinbase", "binance", "kraken", "bitstamp"), results
        ):
            prices[exchange] = None if isinstance(result, Exception) else result
            if isinstance(result, Exception):
                log.debug("BTC cross-exchange %s error: %s", exchange, result)

        valid = {k: v for k, v in prices.items() if v is not None}
        if not valid:
            return None
        # Require at least 2 sources for the spread to be meaningful. With 1
        # source the reported spread is 0 and the "mid" is just that one
        # exchange's price, which consumers (ep_advisor / ep_btc spread gate)
        # may interpret as "narrow spread → safe to trade". Surface partial
        # outages explicitly so ops sees when 3 of 4 exchanges are down.
        if len(valid) < 2:
            log.warning(
                "BTC cross-exchange: only %d/4 sources responding — "
                "skipping write to avoid misleading 0-spread reading",
                len(valid),
            )
            return None

        lo_exch = min(valid, key=valid.__getitem__)
        hi_exch = max(valid, key=valid.__getitem__)
        lo      = valid[lo_exch]
        hi      = valid[hi_exch]
        spread_bps = round((hi - lo) / lo * 10_000, 2) if lo > 0 else 0.0

        return {
            "prices":          {k: round(v, 2) for k, v in valid.items()},
            "cheapest":        lo_exch,
            "most_expensive":  hi_exch,
            "spread_bps":      spread_bps,
            "arbitrage_usd":   round(hi - lo, 2),
            "mid":             round(sum(valid.values()) / len(valid), 2),
        }

    async def _fetch_walcl(self) -> Optional[dict]:
        """
        FRED WALCL: Fed total assets (weekly, millions USD).
        Computes 4-week change and expansion/contraction trend.
        """
        obs = await self._fred("WALCL", limit=6)
        if not obs or len(obs) < 2:
            return None
        latest     = float(obs[0]["value"])
        prev_4w    = float(obs[min(4, len(obs) - 1)]["value"])
        change_4w  = latest - prev_4w
        trend      = "expanding" if change_4w > 0 else "contracting"
        return {
            "latest_billions_usd": round(latest / 1_000, 2),
            "change_4w_billions":  round(change_4w / 1_000, 2),
            "trend":               trend,
            "as_of":               obs[0].get("date"),
            "series":              "WALCL",
        }

    async def _fetch_baa10y(self) -> Optional[dict]:
        """
        FRED BAA10Y: Moody's Baa corporate bond yield minus 10Y Treasury.
        Credit spread proxy — widening signals risk-off / recession concern.
        """
        obs = await self._fred("BAA10Y", limit=6)
        if not obs:
            return None
        latest    = float(obs[0]["value"])
        prev      = float(obs[min(3, len(obs) - 1)]["value"])
        change_3w = round(latest - prev, 3)
        if latest >= 4.0:
            regime = "stressed"
        elif latest >= 2.5:
            regime = "elevated"
        else:
            regime = "normal"
        return {
            "spread_pct":    latest,
            "change_3w_pct": change_3w,
            "regime":        regime,
            "as_of":         obs[0].get("date"),
            "series":        "BAA10Y",
        }

    async def _fetch_predictit(self) -> Optional[dict]:
        """
        PredictIt public API.  Filters for Fed rate, BTC, and economic markets
        to provide a second divergence source alongside Polymarket/Kalshi.
        """
        _TERMS = {
            "fed", "federal reserve", "fomc", "rate cut", "rate hike",
            "bitcoin", "btc",
            "cpi", "inflation", "unemployment", "nfp", "gdp", "recession",
        }
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get("https://www.predictit.org/api/marketdata/all/")
            if r.status_code != 200:
                return None

            markets = []
            for m in r.json().get("markets", []):
                name_lower = (m.get("name") or m.get("shortName") or "").lower()
                if not any(t in name_lower for t in _TERMS):
                    continue
                if m.get("status") != "Open":
                    continue

                contracts = []
                for c in (m.get("contracts") or []):
                    last = c.get("lastTradePrice")
                    if last is None:
                        continue
                    contracts.append({
                        "name":        c.get("name") or c.get("shortName"),
                        "yes_price":   last,
                        "best_yes":    c.get("bestBuyYesCost"),
                        "best_no":     c.get("bestBuyNoCost"),
                    })
                if not contracts:
                    continue

                markets.append({
                    "id":        m.get("id"),
                    "name":      m.get("name") or m.get("shortName"),
                    "url":       m.get("url"),
                    "end_date":  m.get("endDate"),
                    "contracts": contracts,
                })

            return {"markets": markets, "count": len(markets)}
        except Exception as exc:
            log.warning("PredictIt fetch error: %s", exc)
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    async def refresh_all(self) -> Dict[str, str]:
        """
        Refresh all stale sources concurrently.
        Returns {"source_name": "refreshed|skipped|failed"} for each source.
        """
        _fetchers = {
            "sofr_sr1":          (self._fetch_sofr_sr1,           "ep:sofr:sr1",           300),
            "sofr_sr3":          (self._fetch_sofr_sr3,           "ep:sofr:sr3",           300),
            "treasury_auctions": (self._fetch_treasury_auctions,  "ep:treasury_auctions",  86400),
            "econ_consensus":    (self._fetch_econ_consensus,      "ep:econ_consensus",     3600),
            "deribit":           (self._fetch_deribit,             "ep:deribit:skew",       600),
            "btc_cross":         (self._fetch_btc_cross_exchange,  "ep:btc:cross_exchange", 120),
            "walcl":             (self._fetch_walcl,               "ep:macro:walcl",        86400),
            "baa10y":            (self._fetch_baa10y,              "ep:macro:baa10y",       3600),
            "predictit":         (self._fetch_predictit,           "ep:predictit:markets",  300),
        }

        # Check staleness for all sources in parallel
        stale_checks = await asyncio.gather(
            *[self._is_stale(key) for _, key, _ in _fetchers.values()],
            return_exceptions=True,
        )
        stale_map = {
            name: (isinstance(stale, bool) and stale)
            for name, stale in zip(_fetchers, stale_checks)
        }

        async def _run_one(name: str, fn, key: str, ttl: int) -> str:
            if not stale_map.get(name):
                return "skipped"
            try:
                data = await fn()
                if data is not None:
                    await self._write(key, data, ttl)
                    log.info("datasources: refreshed %-22s  key=%s", name, key)
                    return "refreshed"
                log.warning("datasources: %-22s returned None — cache untouched", name)
                return "failed"
            except Exception as exc:
                log.warning("datasources: %-22s error: %s", name, exc)
                return "failed"

        results = await asyncio.gather(
            *[_run_one(name, fn, key, ttl) for name, (fn, key, ttl) in _fetchers.items()],
            return_exceptions=True,
        )
        summary = {}
        for name, result in zip(_fetchers, results):
            summary[name] = result if isinstance(result, str) else "error"

        n_ref  = sum(1 for v in summary.values() if v == "refreshed")
        n_skip = sum(1 for v in summary.values() if v == "skipped")
        n_fail = sum(1 for v in summary.values() if v == "failed")
        log.info(
            "datasources: cycle done — refreshed=%d  skipped=%d  failed=%d",
            n_ref, n_skip, n_fail,
        )
        return summary

    async def run_loop(self, interval_s: int = LOOP_INTERVAL_S) -> None:
        log.info("datasources: loop started  interval=%ds", interval_s)
        while True:
            t0 = time.monotonic()
            try:
                await self.refresh_all()
            except Exception as exc:
                log.warning("datasources: refresh_all top-level error: %s", exc)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval_s - elapsed))


# ── Convenience readers (for ep_intel.py / ep_advisor.py) ─────────────────────

async def read_source(redis_url: str, key: str) -> Optional[dict]:
    """Read a single cached source by Redis key.  Returns None if absent or stale."""
    import redis.asyncio as aioredis
    r = await aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True,
                                 socket_connect_timeout=3)
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None
    finally:
        await r.aclose()


# ── Standalone entry point ─────────────────────────────────────────────────────

async def main(loop: bool) -> None:
    import sys
    if not REDIS_URL:
        print("[ep_datasources] ERROR: REDIS_URL not set", flush=True)
        sys.exit(1)

    ds = DataSourceManager(REDIS_URL)
    await ds.connect()
    log.info(
        "datasources: started  fred_key=%s  te_key=%s  loop=%s",
        "yes" if FRED_KEY else "no",
        "yes" if TE_KEY   else "no",
        loop,
    )
    try:
        if loop:
            await ds.run_loop()
        else:
            summary = await ds.refresh_all()
            for name, status in summary.items():
                print(f"  {status:<10} {name}", flush=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("datasources: shutdown")
    finally:
        await ds.close()


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt = "%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="EdgePulse data source daemon")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously every DATASOURCES_INTERVAL_S seconds (default: 60)")
    args = parser.parse_args()
    asyncio.run(main(loop=args.loop))
