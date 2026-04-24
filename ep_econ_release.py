"""
ep_econ_release.py — Economic release fast-reaction.

Runs exclusively on Exec (Chicago) node.

Playbook:
  T-2h:   Place YES + NO bracket limit orders around consensus on KXCPI / KXGDP
  T-30s:  Begin high-frequency polling of BLS / BEA release endpoints (200ms)
  T+0:    Parse actual value — confirm same reading 3 consecutive polls
  T+2–5s: Cancel wrong-direction brackets; reinforce correct side; add momentum
  T+30s:  Stop reacting; enter 2h cooldown

Window of opportunity: retail takes 30–60 s to reprice; Kalshi institutions sparse.
At 50 ms order-placement latency from Chicago, 10–30 s of edge is capturable.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
load_dotenv()

from ep_config import cfg, log
from kalshi_bot.auth   import KalshiAuth
from kalshi_bot.client import KalshiClient

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")
FRED_API_KEY         = os.getenv("FRED_API_KEY", "")
BEA_API_KEY          = os.getenv("BEA_API_KEY", "")
EP_ECON_CONSENSUS    = "ep:econ_consensus"
EP_CONFIG            = "ep:config"
EP_ECON_STATUS       = "ep:econ_release:status"

PRE_POSITION_H       = float(os.getenv("ECON_PRE_POSITION_H",  "2.0"))
WATCH_LEAD_S         = int(os.getenv("ECON_WATCH_LEAD_S",      "30"))
POLL_MS              = int(os.getenv("ECON_POLL_MS",           "200"))
CONFIRM_N            = int(os.getenv("ECON_CONFIRM_N",         "3"))
SURPRISE_SIGMA_MIN   = float(os.getenv("ECON_SIGMA_MIN",       "0.5"))
MAX_BRACKET_LOTS     = int(os.getenv("ECON_MAX_LOTS",          "3"))
REACT_WINDOW_S       = int(os.getenv("ECON_REACT_WINDOW_S",    "30"))
COOLDOWN_H           = float(os.getenv("ECON_COOLDOWN_H",       "2.0"))

# BLS press release URLs — updated at exact release time (8:30 AM ET = 12:30 UTC)
BLS_CPI_URL = "https://www.bls.gov/news.release/cpi.nr0.htm"
# BEA GDP API — updated at release
BEA_NIPA_URL = "https://apps.bea.gov/api/data"
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"

# Known default consensus σ (used when TE doesn't provide spread)
_DEFAULT_SIGMA: Dict[str, float] = {
    "KXCPI": 0.12,  # ±0.12% MoM is ≈1σ for CPI surprises
    "KXGDP": 0.50,  # ±0.5 pp annualized GDP is ≈1σ
}

# Mapping from TE event name keywords → Kalshi series + data source
_RELEASE_MAP = [
    {
        "keywords":    ("cpi", "consumer price index"),
        "series":      "KXCPI",
        "unit":        "pct_mom",          # month-over-month %
        "fetch_fn":    "_fetch_bls_cpi",
        "fred_series": "CPIAUCSL",         # CPI-U seasonally adjusted
    },
    {
        "keywords":    ("gdp", "gross domestic product"),
        "series":      "KXGDP",
        "unit":        "pct_saar",         # annualized % change
        "fetch_fn":    "_fetch_bea_gdp",
        "fred_series": "A191RL1Q225SBEA",
    },
]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ScheduledRelease:
    event_name:    str
    release_time:  datetime       # UTC
    consensus:     float          # expected value
    sigma:         float          # 1σ surprise magnitude
    kalshi_series: str
    fetch_fn:      str            # method name on engine
    fred_series:   str
    unit:          str


@dataclass
class Bracket:
    ticker:         str
    strike:         float
    yes_order_id:   Optional[str] = None
    no_order_id:    Optional[str] = None
    yes_limit_c:    int  = 0
    no_limit_c:     int  = 0


# ── Engine ────────────────────────────────────────────────────────────────────

class EconReleaseEngine:

    def __init__(self) -> None:
        self._auth = KalshiAuth(
            api_key_id       = cfg.API_KEY_ID or "",
            private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        ) if cfg.API_KEY_ID else None
        self._client: Optional[KalshiClient] = KalshiClient(
            base_url = cfg.BASE_URL,
            auth     = self._auth,
        ) if self._auth else None

        self._redis           = None
        self._halt            = False
        self._brackets:       Dict[str, Bracket] = {}
        self._last_release:   Optional[str]      = None   # event_name of most recent release
        self._last_release_ts: Optional[str]     = None   # ISO timestamp of last print
        self._stats = {"releases_watched": 0, "brackets_placed": 0,
                       "brackets_cancelled": 0, "momentum_orders": 0}

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _check_halt(self) -> bool:
        try:
            if self._redis:
                val = await self._redis.hget(EP_CONFIG, "HALT_TRADING")
                return val in ("1", b"1")
        except Exception:
            pass
        return False

    def _cancel_order_sync(self, order_id: str) -> bool:
        """Cancel a Kalshi order synchronously (runs in thread pool)."""
        try:
            self._client._request("DELETE", f"/portfolio/orders/{order_id}")
            return True
        except Exception as exc:
            log.debug("econ: cancel %s failed: %s", order_id, exc)
            return False

    async def _cancel_order(self, order_id: str, label: str) -> bool:
        ok = await asyncio.to_thread(self._cancel_order_sync, order_id)
        if ok:
            log.info("econ: cancelled order %s  (%s)", order_id[:12], label)
            self._stats["brackets_cancelled"] += 1
        return ok

    # ── Release schedule ──────────────────────────────────────────────────────

    async def _load_next_release(self) -> Optional[ScheduledRelease]:
        """Read ep:econ_consensus and find the next upcoming release we track."""
        if not self._redis:
            return None
        try:
            raw = await self._redis.get(EP_ECON_CONSENSUS)
            if not raw:
                return None
            data = json.loads(raw)
        except Exception as exc:
            log.warning("econ: Redis read error: %s", exc)
            return None

        now = datetime.now(timezone.utc)
        candidates: List[ScheduledRelease] = []

        for ev in data.get("events", []):
            ev_name = (ev.get("event") or "").lower()
            ev_date = ev.get("date", "")
            forecast_raw = ev.get("forecast") or ev.get("Forecast") or ""
            if not ev_date or not forecast_raw:
                continue

            # Parse release time
            try:
                ev_dt = datetime.fromisoformat(ev_date.replace("Z", "+00:00"))
                if not ev_dt.tzinfo:
                    ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue

            # DST sanity check: BLS/BEA releases are 8:30 ET (winter=13:30 UTC,
            # summer=12:30 UTC). If the parsed time lands far outside that band,
            # the upstream source (ep:econ_consensus writer) may have fed us
            # naive ET without UTC conversion. Log — don't reject, since the
            # check may be wrong (non-8:30 releases exist), but surface it.
            _hr = ev_dt.hour
            if _hr < 11 or _hr > 15:
                log.warning(
                    "econ: %s release_time %s has unusual hour %d (expect 12-14 UTC "
                    "for 8:30 ET); check for DST/TZ mis-conversion upstream",
                    ev_name, ev_dt.isoformat(), _hr,
                )

            if ev_dt <= now:
                continue  # already past

            # Match to a release spec
            spec = None
            for s in _RELEASE_MAP:
                if any(kw in ev_name for kw in s["keywords"]):
                    spec = s
                    break
            if spec is None:
                continue

            # Parse consensus value (strip % and commas)
            try:
                consensus = float(str(forecast_raw).replace("%", "").replace(",", "").strip())
            except ValueError:
                continue

            sigma = _DEFAULT_SIGMA.get(spec["series"], 0.20)

            candidates.append(ScheduledRelease(
                event_name    = ev.get("event", "unknown"),
                release_time  = ev_dt,
                consensus     = consensus,
                sigma         = sigma,
                kalshi_series = spec["series"],
                fetch_fn      = spec["fetch_fn"],
                fred_series   = spec["fred_series"],
                unit          = spec["unit"],
            ))

        if not candidates:
            return None
        # Return soonest upcoming
        return min(candidates, key=lambda r: r.release_time)

    # ── Kalshi market discovery ───────────────────────────────────────────────

    async def _find_boundary_market(self, series: str, consensus: float) -> Optional[dict]:
        """
        Find the KXCPI / KXGDP market whose YES mid is nearest to 50¢
        (the most price-sensitive market at the consensus boundary).
        Also returns markets 1–2 steps away for momentum adds.
        """
        try:
            resp = await asyncio.to_thread(
                self._client.get, "/markets",
                {"status": "open", "series_ticker": series, "limit": 50},
            )
            markets = (resp or {}).get("markets", [])
        except Exception as exc:
            log.warning("econ: market fetch failed for %s: %s", series, exc)
            return None

        # Filter: expiration within the next 120 days
        now = datetime.now(timezone.utc)
        valid = []
        for m in markets:
            try:
                exp_dt = datetime.fromisoformat(
                    m.get("expiration_time", "").replace("Z", "+00:00")
                )
                if now < exp_dt < now + timedelta(days=120):
                    valid.append(m)
            except (ValueError, AttributeError):
                continue

        if not valid:
            log.warning("econ: no near-expiry %s markets", series)
            return None

        # Pick the market with YES mid nearest to 50¢ among valid markets
        def _mid(m: dict) -> float:
            bid = float(m.get("yes_bid_dollars") or 0) * 100
            ask = float(m.get("yes_ask_dollars") or 0) * 100
            return (bid + ask) / 2.0 if bid > 0 else ask

        best = min(valid, key=lambda m: abs(_mid(m) - 50.0))
        m_mid = _mid(best)
        if m_mid < 2.0 or m_mid > 98.0:
            # All markets are near certainty — consensus is way off our strikes
            log.warning("econ: %s boundary market %s at extreme mid=%.0f¢ — skip bracket",
                        series, best.get("ticker", ""), m_mid)
            return best   # still return it for momentum-only use

        return best

    # ── Pre-positioning ───────────────────────────────────────────────────────

    async def _place_brackets(self, release: ScheduledRelease) -> None:
        """Place YES + NO limit orders on the boundary market 2h before release."""
        if not self._client:
            log.warning("econ: no Kalshi client — skip bracket placement")
            return
        if await self._check_halt():
            log.info("econ: HALT_TRADING active — skip bracket placement")
            return

        market = await self._find_boundary_market(release.kalshi_series, release.consensus)
        if not market:
            return

        ticker = market.get("ticker", "")
        strike = float(market.get("floor_strike") or 0)
        bid_y  = float(market.get("yes_bid_dollars") or 0) * 100
        ask_y  = float(market.get("yes_ask_dollars") or 0) * 100
        mid_y  = (bid_y + ask_y) / 2.0 if bid_y > 0 else ask_y

        # YES limit: 1¢ below current ask (passive — captures pre-release drift)
        # NO limit:  1¢ below current NO ask = 1¢ above YES bid
        yes_limit = max(1, int(ask_y) - 1)
        no_limit  = max(1, min(98, int(100 - bid_y) - 1))

        bracket = Bracket(ticker=ticker, strike=strike,
                          yes_limit_c=yes_limit, no_limit_c=no_limit)

        log.info("econ: placing bracket  %s  strike=%.2f  YES@%d¢  NO@%d¢  mid=%.0f¢",
                 ticker, strike, yes_limit, no_limit, mid_y)

        # YES bracket
        try:
            resp = await asyncio.to_thread(
                self._client.post, "/portfolio/orders",
                {"action": "buy", "type": "limit", "ticker": ticker,
                 "side": "yes", "count": MAX_BRACKET_LOTS, "yes_price": yes_limit},
            )
            bracket.yes_order_id = (resp or {}).get("order", {}).get("order_id")
            if bracket.yes_order_id:
                log.info("econ: YES bracket placed  order=%s", bracket.yes_order_id[:12])
                self._stats["brackets_placed"] += 1
        except Exception as exc:
            log.warning("econ: YES bracket failed for %s: %s", ticker, exc)

        # NO bracket
        try:
            resp = await asyncio.to_thread(
                self._client.post, "/portfolio/orders",
                {"action": "buy", "type": "limit", "ticker": ticker,
                 "side": "no", "count": MAX_BRACKET_LOTS, "no_price": no_limit},
            )
            bracket.no_order_id = (resp or {}).get("order", {}).get("order_id")
            if bracket.no_order_id:
                log.info("econ: NO bracket placed  order=%s", bracket.no_order_id[:12])
                self._stats["brackets_placed"] += 1
        except Exception as exc:
            log.warning("econ: NO bracket failed for %s: %s", ticker, exc)

        # Only track bracket if at least one leg actually placed. If both legs
        # failed (Kalshi 409 / capacity / auth), storing an all-None bracket
        # would leave an orphan entry that cancel_all_brackets can't act on.
        if not bracket.yes_order_id and not bracket.no_order_id:
            log.warning(
                "econ: bracket abandoned for %s — both legs failed to place",
                ticker,
            )
            return
        self._brackets[ticker] = bracket

    # ── Release data fetchers ─────────────────────────────────────────────────

    async def _fetch_bls_cpi(
        self, http: httpx.AsyncClient, expected_month: str, expected_year: int
    ) -> Optional[float]:
        """
        Poll BLS CPI press-release HTML.
        Returns MoM SA CPI % change when the page shows the expected month/year.
        """
        try:
            r = await http.get(BLS_CPI_URL, timeout=3.0)
            if r.status_code != 200:
                return None
            html = r.text

            # Guard: page must mention the expected reporting period
            if expected_month.lower() not in html.lower():
                return None
            if str(expected_year) not in html:
                return None

            # "CPI-U (increased|decreased|was unchanged) X.X percent in [Month Year]
            #  on a seasonally adjusted basis"
            m = re.search(
                r'CPI-U\s+(increased|decreased|rose|fell|was unchanged)\s+'
                r'([\d.]+)?\s*percent\s+in\s+\w+\s+\d{4}[^.]*?'
                r'seasonally adjusted',
                html, re.IGNORECASE | re.DOTALL,
            )
            if m:
                direction = m.group(1).lower()
                val_str   = m.group(2) or "0"
                val       = float(val_str)
                return val if "decr" not in direction and "fell" not in direction else -val

            # Fallback: "unchanged" means 0.0
            if re.search(r'CPI-U was unchanged', html, re.IGNORECASE):
                return 0.0

            return None
        except Exception as exc:
            log.debug("econ: CPI poll error: %s", exc)
            return None

    async def _fetch_bea_gdp(
        self, http: httpx.AsyncClient, expected_month: str, expected_year: int
    ) -> Optional[float]:
        """
        Poll BEA NIPA API for real GDP percent change SAAR.
        Returns the Q1 advance estimate value when it first appears.
        """
        # Determine the quarter from the release month
        # Advance Q1 GDP ≈ late April; Q2 ≈ late July; etc.
        month_to_quarter = {
            "april": 1, "may": 1, "july": 2, "august": 2,
            "october": 3, "november": 3, "january": 4, "february": 4,
        }
        quarter = month_to_quarter.get(expected_month.lower())
        if quarter is None:
            return None

        year = expected_year
        if expected_month.lower() in ("january", "february"):
            year -= 1   # Q4 of prior year

        try:
            params: dict = {
                "method":      "GetData",
                "datasetname": "NIPA",
                "TableName":   "T10101",
                "Frequency":   "Q",
                "Year":        str(year),
                "ResultFormat": "JSON",
            }
            if BEA_API_KEY:
                params["UserID"] = BEA_API_KEY

            r = await http.get(BEA_NIPA_URL, params=params, timeout=5.0)
            if r.status_code != 200:
                return None

            rows = (r.json()
                    .get("BEAAPI", {})
                    .get("Results", {})
                    .get("Data", []))

            quarter_str = f"Q{quarter}"
            gdp_pct_key = "1"    # LineNumber 1 in T10101 = "Gross domestic product"
            for row in rows:
                if (row.get("TimePeriod", "").endswith(quarter_str) and
                        row.get("LineNumber") == gdp_pct_key):
                    val_raw = row.get("DataValue", "")
                    try:
                        return float(str(val_raw).replace(",", ""))
                    except ValueError:
                        pass
            return None
        except Exception as exc:
            log.debug("econ: GDP poll error: %s", exc)
            return None

    # ── Release watch loop ────────────────────────────────────────────────────

    async def _watch_and_react(self, release: ScheduledRelease) -> None:
        """
        High-frequency polling from T-30s until confirmed release + 30s reaction window.
        """
        self._stats["releases_watched"] += 1
        interval  = POLL_MS / 1000.0
        fetch_fn  = getattr(self, release.fetch_fn)

        # Determine expected month/year (KXCPI-26APR = April data released ~May)
        # The BLS release is for the PRIOR month's data — derive from release_time
        report_dt = release.release_time - timedelta(days=30)
        expected_month = report_dt.strftime("%B")      # "April"
        expected_year  = report_dt.year                # 2026

        log.info("econ: watching %s  consensus=%.2f  expecting %s %d",
                 release.event_name, release.consensus, expected_month, expected_year)

        confirmed_readings: List[float] = []
        reacted  = False
        deadline = release.release_time + timedelta(seconds=REACT_WINDOW_S)

        async with httpx.AsyncClient(timeout=4.0) as http:
            while datetime.now(timezone.utc) < deadline:
                t0 = time.monotonic()

                if await self._check_halt():
                    log.warning("econ: HALT_TRADING — aborting watch")
                    await self._cancel_all_brackets()
                    return

                val = await fetch_fn(http, expected_month, expected_year)

                if val is not None:
                    confirmed_readings.append(val)
                    # Keep only the last N; all must agree (within 0.05 tolerance)
                    if len(confirmed_readings) >= CONFIRM_N:
                        recent  = confirmed_readings[-CONFIRM_N:]
                        spread  = max(recent) - min(recent)
                        if spread <= 0.05:
                            confirmed_val = recent[-1]
                            if not reacted:
                                reacted = True
                                log.info(
                                    "econ: CONFIRMED  %s  actual=%.2f  "
                                    "consensus=%.2f  confirmed_after=%d_polls",
                                    release.event_name, confirmed_val,
                                    release.consensus, len(confirmed_readings),
                                )
                                await self._react(confirmed_val, release, http)
                        else:
                            log.debug("econ: unstable readings %s — waiting", recent)
                            confirmed_readings = confirmed_readings[-2:]  # reset partial
                else:
                    confirmed_readings.clear()

                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0.0, interval - elapsed))

        if not reacted:
            log.info("econ: %s — no confirmed release in watch window; cancelling brackets",
                     release.event_name)
            await self._cancel_all_brackets()

    # ── React logic ───────────────────────────────────────────────────────────

    async def _react(
        self,
        actual:  float,
        release: ScheduledRelease,
        http:    httpx.AsyncClient,
    ) -> None:
        surprise      = actual - release.consensus
        surprise_sigma = surprise / release.sigma if release.sigma else surprise

        log.info(
            "econ: REACT  actual=%.3f  consensus=%.3f  surprise=%.3f  σ=%.2f",
            actual, release.consensus, surprise, surprise_sigma,
        )

        # Record the release timestamp regardless of surprise size so vol_multiplier
        # in ep_exec.py can correctly identify the post-release window.
        self._last_release_ts = datetime.now(timezone.utc).isoformat()

        if abs(surprise_sigma) < SURPRISE_SIGMA_MIN:
            log.info("econ: surprise |%.2fσ| < %.1fσ — no edge; cancel all",
                     surprise_sigma, SURPRISE_SIGMA_MIN)
            await self._cancel_all_brackets()
            return

        # Direction: higher actual → YES on "above X" strike; lower → NO
        long_yes = surprise > 0

        tasks = []
        for ticker, bracket in list(self._brackets.items()):
            if long_yes:
                # Keep YES — cancel NO
                if bracket.no_order_id:
                    tasks.append(self._cancel_order(bracket.no_order_id,
                                                    f"NO-bracket {ticker}"))
                    bracket.no_order_id = None
            else:
                # Keep NO — cancel YES
                if bracket.yes_order_id:
                    tasks.append(self._cancel_order(bracket.yes_order_id,
                                                    f"YES-bracket {ticker}"))
                    bracket.yes_order_id = None

        if tasks:
            await asyncio.gather(*tasks)

        # Momentum: add 1 extra contract in surprise direction
        await self._add_momentum(release, long_yes, abs(surprise_sigma), http)

    async def _add_momentum(
        self,
        release:    ScheduledRelease,
        long_yes:   bool,
        sigma:      float,
        http:       httpx.AsyncClient,
    ) -> None:
        """Place an aggressive limit entry in the surprise direction."""
        if not self._client:
            return

        # Respect operator's EV floor (override_edge_threshold in ep:config).
        # The main intel pipeline applies this to every signal; momentum orders
        # placed here bypass the pipeline, so we must apply it ourselves.
        # Rough edge proxy: σ × 5¢ (calibrated to CPI where 1σ ≈ 5¢ move on
        # nearest-strike markets). Conservative on purpose — an operator who
        # tightens the EV floor for directional trades usually also wants
        # event-driven momentum to slow down.
        if self._redis is not None:
            try:
                _ov_raw = await self._redis.hget(EP_CONFIG, "override_edge_threshold")
                if _ov_raw is not None:
                    _ov = float(_ov_raw.decode() if isinstance(_ov_raw, bytes) else _ov_raw)
                    _est_edge = sigma * 0.05
                    if _est_edge < _ov:
                        log.info(
                            "econ: MOMENTUM skipped %s — est_edge≈%.3f (σ=%.1f) "
                            "< override_edge_threshold=%.3f",
                            release.kalshi_series, _est_edge, sigma, _ov,
                        )
                        return
            except (ValueError, TypeError, AttributeError) as _ov_exc:
                log.debug("econ: override_edge_threshold read skipped: %s", _ov_exc)

        market = await self._find_boundary_market(release.kalshi_series, release.consensus)
        if not market:
            return

        ticker   = market.get("ticker", "")
        bid_y    = float(market.get("yes_bid_dollars") or 0) * 100
        ask_y    = float(market.get("yes_ask_dollars") or 0) * 100

        # Scale lots by sigma magnitude (cap at MAX_BRACKET_LOTS)
        lots = min(MAX_BRACKET_LOTS, max(1, int(sigma)))

        if long_yes:
            side      = "yes"
            price_key = "yes_price"
            # Post-release ask has already moved; pay current ask to guarantee fill
            limit_c   = int(ask_y) + 1
        else:
            side      = "no"
            price_key = "no_price"
            limit_c   = int(100 - bid_y) + 1  # current NO ask + 1¢

        limit_c = max(1, min(99, limit_c))

        log.info(
            "econ: MOMENTUM  %s  side=%s  limit=%d¢  lots=%d  σ=%.1f",
            ticker, side, limit_c, lots, sigma,
        )
        try:
            resp = await asyncio.to_thread(
                self._client.post, "/portfolio/orders",
                {"action": "buy", "type": "limit", "ticker": ticker,
                 "side": side, "count": lots, price_key: limit_c},
            )
            order_id = (resp or {}).get("order", {}).get("order_id")
            if order_id:
                log.info("econ: MOMENTUM fill  order=%s", order_id[:12])
                self._stats["momentum_orders"] += 1
            else:
                log.info("econ: MOMENTUM rejected  resp=%s", str(resp)[:80])
        except Exception as exc:
            log.warning("econ: momentum order error: %s", exc)

    async def _cancel_all_brackets(self) -> None:
        tasks = []
        for ticker, bracket in list(self._brackets.items()):
            if bracket.yes_order_id:
                tasks.append(self._cancel_order(bracket.yes_order_id,
                                                f"YES-bracket {ticker}"))
            if bracket.no_order_id:
                tasks.append(self._cancel_order(bracket.no_order_id,
                                                f"NO-bracket {ticker}"))
        self._brackets.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Status writer ─────────────────────────────────────────────────────────

    async def _write_status(self, release: Optional[ScheduledRelease]) -> None:
        if not self._redis:
            return
        try:
            await self._redis.set(EP_ECON_STATUS, json.dumps({
                "ts":               datetime.now(timezone.utc).isoformat(),
                "next_release":     release.event_name if release else None,
                "next_time_utc":    release.release_time.isoformat() if release else None,
                "last_release_ts":  self._last_release_ts,
                "consensus":        release.consensus if release else None,
                "brackets":         len(self._brackets),
                "stats":            self._stats,
                "halt":             self._halt,
            }, default=str), ex=86400)
        except Exception:
            pass

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        import redis.asyncio as aioredis
        self._redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=False,
            socket_connect_timeout=5,
        )

        if not self._client:
            log.warning("econ: no Kalshi credentials — monitor-only mode")

        while True:
            release = await self._load_next_release()
            await self._write_status(release)

            if release is None:
                log.info("econ: no upcoming tracked releases in ep:econ_consensus — sleep 1h")
                await asyncio.sleep(3600)
                continue

            log.info(
                "econ: next release  %s  consensus=%.3f  time=%s",
                release.event_name, release.consensus,
                release.release_time.strftime("%Y-%m-%d %H:%M UTC"),
            )

            now = datetime.now(timezone.utc)

            # Sleep until T-2h (pre-position window)
            pre_pos_time = release.release_time - timedelta(hours=PRE_POSITION_H)
            if now < pre_pos_time:
                wait = (pre_pos_time - now).total_seconds()
                log.info("econ: sleeping %.0f s until pre-position window", wait)
                await asyncio.sleep(wait)

            # Place bracket orders
            await self._place_brackets(release)

            # Sleep until T-30s (watch window)
            watch_time = release.release_time - timedelta(seconds=WATCH_LEAD_S)
            now = datetime.now(timezone.utc)
            if now < watch_time:
                wait = (watch_time - now).total_seconds()
                log.info("econ: sleeping %.0f s until watch window (T-%.0fs)",
                         wait, WATCH_LEAD_S)
                await asyncio.sleep(wait)

            # Watch and react
            await self._watch_and_react(release)

            # Log final stats
            log.info(
                "econ: release cycle complete  "
                "brackets=%d  cancelled=%d  momentum=%d",
                self._stats["brackets_placed"],
                self._stats["brackets_cancelled"],
                self._stats["momentum_orders"],
            )

            # Cooldown before looking for next release
            log.info("econ: entering %.1fh cooldown", COOLDOWN_H)
            await asyncio.sleep(COOLDOWN_H * 3600)

        await self._redis.aclose()


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    engine = EconReleaseEngine()
    try:
        await engine.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("econ: shutdown")


if __name__ == "__main__":
    asyncio.run(main())
