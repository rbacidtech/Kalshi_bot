"""
ep_arb.py — Real-time Polymarket / Kalshi divergence arbitrage.

Runs exclusively on the Exec (Chicago) node.  Bypasses the
Intel → Redis → Exec signal pipeline entirely.

Two parallel polling tasks (no WebSocket — Polymarket CLOB WS rejected connections):
  Task A  Polymarket Gamma REST  every POLY_POLL_MS ms (default 300 ms)
          Batch-fetches outcomePrices for all paired markets in one call.
  Task B  Kalshi REST poller     every KALSHI_POLL_MS ms (default 200 ms)
          Fetches yes_bid/yes_ask for the KXFED series.

Divergence logic (runs on every price update from A or B):
  |poly_yes_cents − kalshi_mid_cents| > ARB_MIN_CENTS  for > HOLD_MS ms
  → place Kalshi limit order at the current ask, enter per-ticker cooldown.

End-to-end latency:  detection ≤ 300 ms + 500 ms hold = ≤ 800 ms.
Previous pipeline:   Intel poll (2 min) + Redis + Exec = ~5–30 s typical.
Estimated P&L lift:  20 % → 50–60 % of detected divergences captured.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv
load_dotenv()

from ep_config import cfg, log
from kalshi_bot.auth   import KalshiAuth
from kalshi_bot.client import KalshiClient

# ── Config ─────────────────────────────────────────────────────────────────────
ARB_MIN_CENTS    = float(os.getenv("ARB_MIN_CENTS",     "2.0"))   # divergence threshold (¢)
HOLD_MS          = int(os.getenv("ARB_HOLD_MS",         "500"))   # debounce ms above threshold
COOLDOWN_S       = int(os.getenv("ARB_COOLDOWN_S",      "120"))   # per-ticker cooldown
MAX_CONTRACTS    = int(os.getenv("ARB_MAX_CONTRACTS",   "3"))     # contracts per arb trade
# Capital cap — skip firing when adding this trade's cost would push total
# open exposure above this fraction of balance. Parallels ep_exec.py's
# directional exposure gate; arbs were previously uncapped. Fail-open with
# a warning if balance can't be read (preserves latency vs the arb opportunity).
ARB_MAX_TOTAL_EXP_PCT = float(os.getenv("ARB_MAX_TOTAL_EXP_PCT", "0.80"))
# Max age (seconds) of poly + kalshi prices before the arb opportunity is
# considered stale. Polygon polls every ~300ms and Kalshi every ~200ms, so
# normal wall clock age at FIRE time is < 1s. Reject if either price is older
# than this — the divergence may have already closed.
ARB_MAX_PRICE_AGE_S = float(os.getenv("ARB_MAX_PRICE_AGE_S", "2.0"))
POLY_POLL_MS     = int(os.getenv("ARB_POLY_POLL_MS",    "300"))   # Polymarket poll interval (ms)
KALSHI_POLL_MS   = int(os.getenv("ARB_KALSHI_POLL_MS",  "200"))   # Kalshi poll interval (ms)
MAP_REFRESH_S    = int(os.getenv("ARB_MAP_REFRESH_S",   "1800"))  # market remap every 30 min
POLY_GAMMA_URL   = "https://gamma-api.polymarket.com"
REDIS_URL        = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EP_POSITIONS     = "ep:positions"
EP_CONFIG        = "ep:config"
EP_BALANCE       = "ep:balance"
EP_ARB_STATUS    = "ep:arb:status"

# Kalshi meeting code → Polymarket month string
# KXFED-26APR → "april 2026", "april 26"
_MONTH_MAP = {
    "JAN": "january", "FEB": "february", "MAR": "march",  "APR": "april",
    "MAY": "may",     "JUN": "june",     "JUL": "july",   "AUG": "august",
    "SEP": "september","OCT": "october", "NOV": "november","DEC": "december",
}

# Poly keywords that indicate "no change + hike" direction (YES = rate stays ≥ current level)
_POLY_NOCHANGE_KW = ("no change", "pause", "hold")
_POLY_HIKE_KW     = ("increase", "hike", "raise")
_POLY_CUT_KW      = ("decrease", "cut", "lower", "reduction")


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class ArbPair:
    kalshi_ticker:     str
    poly_condition_id: str
    poly_market_id:    int          # numeric Polymarket market ID for /markets/{id}
    poly_title:        str          # for logging
    poly_price:        float = 0.0  # 0–1 scale; YES price from Gamma API
    kalshi_bid:        float = 0.0  # cents; YES bid
    kalshi_ask:        float = 0.0  # cents; YES ask
    divergence_since:  Optional[float] = None   # time.monotonic()
    cooldown_until:    float = 0.0
    poly_ts:           float = 0.0
    kalshi_ts:         float = 0.0

    @property
    def kalshi_mid(self) -> float:
        if self.kalshi_bid > 0 and self.kalshi_ask > 0:
            return (self.kalshi_bid + self.kalshi_ask) / 2.0
        return self.kalshi_bid or self.kalshi_ask


# ── Engine ─────────────────────────────────────────────────────────────────────

class ArbEngine:

    def __init__(self) -> None:
        self._auth = KalshiAuth(
            api_key_id       = cfg.API_KEY_ID or "",
            private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        ) if cfg.API_KEY_ID else None
        self._client = KalshiClient(
            base_url = cfg.BASE_URL,
            auth     = self._auth,
        ) if self._auth else None

        self._pairs:  Dict[str, ArbPair] = {}   # kalshi_ticker → ArbPair
        self._by_cid: Dict[str, str]     = {}   # poly_condition_id → kalshi_ticker
        self._by_pid: Dict[int, str]     = {}   # poly_market_id → kalshi_ticker
        self._redis   = None
        self._halt    = False
        self._stats   = {"detected": 0, "fired": 0, "filled": 0, "rejected": 0}

    # ── Market mapping ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_kxfed_meeting(ticker: str) -> Optional[tuple]:
        """Return (year, month_name) from KXFED-YYMM-TX.XX or None."""
        m = re.match(r"KXFED-(\d{2})([A-Z]{3})-T", ticker)
        if not m:
            return None
        yy, mon = m.group(1), m.group(2)
        name = _MONTH_MAP.get(mon)
        if not name:
            return None
        return (int("20" + yy), name)

    @staticmethod
    def _infer_current_rate(meeting_markets: List[dict]) -> Optional[float]:
        """
        Infer the expected rate entering this meeting from Kalshi prices.
        Returns the first floor_strike (ascending) where YES mid drops below 50¢ —
        i.e., the 50 % crossover point.  For a rate of 3.75 %, YES above 3.75 % is
        rare (≪50 %) while YES above 3.50 % is near-certain (≫50 %).
        """
        by_strike = sorted(
            meeting_markets,
            key=lambda m: float(m.get("floor_strike") or 0),
        )
        for m in by_strike:
            bid = float(m.get("yes_bid_dollars") or 0) * 100
            ask = float(m.get("yes_ask_dollars") or 0) * 100
            mid = (bid + ask) / 2.0 if bid > 0 else ask
            if mid < 50.0:
                return float(m.get("floor_strike") or 0)
        return None

    async def _build_pairs(self, http: httpx.AsyncClient) -> None:
        """Semantic meeting-date pairing: Poly 'no change+hike after M' ↔ Kalshi KXFED-M-T{R-0.25} YES."""

        # 1. Kalshi open KXFED markets
        try:
            resp_k = await asyncio.to_thread(
                self._client.get,
                "/markets",
                {"status": "open", "series_ticker": "KXFED", "limit": 200},
            ) if self._client else {}
            kalshi_markets: List[dict] = (resp_k or {}).get("markets", [])
        except Exception as exc:
            log.warning("arb: Kalshi market fetch failed: %s", exc)
            return

        # Group Kalshi markets by meeting code (e.g. "26APR")
        mtg_groups: Dict[str, List[dict]] = {}
        for km in kalshi_markets:
            ticker = km.get("ticker", "")
            mo = re.match(r"KXFED-(\d{2}[A-Z]{3})-", ticker)
            if mo:
                mtg_groups.setdefault(mo.group(1), []).append(km)

        # 2. Polymarket: collect all active Fed-related markets
        poly_fed: List[dict] = []
        page_size = 500
        last_page = 0
        for page in range(10):
            last_page = page
            try:
                r = await http.get(
                    f"{POLY_GAMMA_URL}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit":  str(page_size),
                        "offset": str(page * page_size),
                    },
                )
                if r.status_code != 200:
                    break
                batch = r.json()
                if not batch:
                    break
                for m in batch:
                    q = m.get("question", "").lower()
                    if any(kw in q for kw in (
                        "fed ", "fomc", "federal reserve", "interest rate",
                        "rate cut", "rate hike", "no change",
                    )):
                        poly_fed.append(m)
                if len(batch) < page_size:
                    break
            except Exception as exc:
                log.warning("arb: Polymarket page %d fetch failed: %s", page, exc)
                break

        if not poly_fed:
            log.warning("arb: no Polymarket Fed markets (fetched %d pages)", last_page + 1)
            return

        # 3. Semantic meeting pairing
        new_pairs:  Dict[str, ArbPair] = {}
        new_by_cid: Dict[str, str]     = {}
        paired_meetings = 0

        for mtg_code, mkts in mtg_groups.items():
            parsed = self._parse_kxfed_meeting("KXFED-" + mtg_code + "-T0.00")
            if not parsed:
                continue
            year, month_name = parsed
            yr2 = str(year)[2:]  # "26"

            # Infer expected current rate for this meeting from Kalshi prices
            current_rate = self._infer_current_rate(mkts)
            if current_rate is None:
                continue

            # Find the Kalshi strike at current_rate - 0.25 (the "no change" boundary)
            target_strike = round(current_rate - 0.25, 2)
            kalshi_match = None
            for km in mkts:
                if abs(float(km.get("floor_strike") or 0) - target_strike) < 0.01:
                    kalshi_match = km
                    break
            if not kalshi_match:
                continue

            k_ticker = kalshi_match.get("ticker", "")
            if not k_ticker:
                continue

            # Find Polymarket "no change" market for this meeting
            # Match patterns like "no change in fed interest rates after the april 2026 meeting"
            # Require BOTH the month name AND the 4-digit year to be in the question.
            best_pm: Optional[dict] = None
            for pm in poly_fed:
                q = pm.get("question", "").lower()
                has_month = month_name in q and str(year) in q
                if not has_month:
                    continue
                if any(kw in q for kw in _POLY_NOCHANGE_KW):
                    best_pm = pm
                    break

            if not best_pm:
                log.debug("arb: no Poly 'no change' market for meeting %s", mtg_code)
                continue

            cid = best_pm.get("conditionId", "")
            if not cid or cid in new_by_cid:
                continue

            poly_id = int(best_pm.get("id") or 0)
            if not poly_id:
                log.debug("arb: Polymarket market has no numeric id for %s", mtg_code)
                continue

            pair = ArbPair(
                kalshi_ticker     = k_ticker,
                poly_condition_id = cid,
                poly_market_id    = poly_id,
                poly_title        = best_pm.get("question", "")[:80],
                # Seed with current Kalshi prices (dollars → cents)
                kalshi_bid = float(kalshi_match.get("yes_bid_dollars") or 0) * 100,
                kalshi_ask = float(kalshi_match.get("yes_ask_dollars") or 0) * 100,
            )
            # Preserve live prices across remaps
            if k_ticker in self._pairs:
                old = self._pairs[k_ticker]
                pair.poly_price       = old.poly_price
                pair.kalshi_bid       = old.kalshi_bid or pair.kalshi_bid
                pair.kalshi_ask       = old.kalshi_ask or pair.kalshi_ask
                pair.cooldown_until   = old.cooldown_until
                pair.divergence_since = old.divergence_since

            new_pairs[k_ticker]  = pair
            new_by_cid[cid]      = k_ticker
            paired_meetings += 1
            log.info(
                "arb: paired  %-30s ↔ %s  (rate=%.2f%%  strike=%.2f)",
                k_ticker, best_pm.get("question", "")[:50], current_rate, target_strike,
            )

        self._pairs   = new_pairs
        self._by_cid  = new_by_cid
        self._by_pid  = {p.poly_market_id: k for k, p in new_pairs.items()}
        log.info(
            "arb: mapped %d pairs  (kalshi_meetings=%d  poly_fed=%d)",
            len(self._pairs), len(mtg_groups), len(poly_fed),
        )

    # ── Task A: Polymarket REST poller ─────────────────────────────────────────

    async def _poll_poly(self) -> None:
        """
        Polls each paired Polymarket market individually via GET /markets/{id}.
        The Gamma API batch endpoint (condition_ids=...) always returns [] so we
        fetch one-by-one; with ≤5 pairs this adds negligible latency.
        """
        interval = POLY_POLL_MS / 1000.0
        async with httpx.AsyncClient(timeout=5.0) as http:
            while True:
                t0 = time.monotonic()
                for pid, ticker in list(self._by_pid.items()):
                    if ticker not in self._pairs:
                        continue
                    try:
                        r = await http.get(f"{POLY_GAMMA_URL}/markets/{pid}")
                        if r.status_code != 200:
                            continue
                        m = r.json()
                        raw_prices = m.get("outcomePrices", "[]")
                        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                        if prices:
                            price = float(prices[0])  # index 0 = YES
                            if 0.0 < price < 1.0:
                                self._pairs[ticker].poly_price = price
                                self._pairs[ticker].poly_ts    = time.monotonic()
                                self._check_divergence(ticker)
                    except Exception as exc:
                        log.debug("arb: Polymarket poll error pid=%d: %s", pid, exc)

                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0.0, interval - elapsed))

    # ── Task B: Kalshi REST poller ─────────────────────────────────────────────

    async def _poll_kalshi(self) -> None:
        interval = KALSHI_POLL_MS / 1000.0
        while True:
            t0 = time.monotonic()
            if self._pairs and self._client:
                try:
                    resp = await asyncio.to_thread(
                        self._client.get,
                        "/markets",
                        {"status": "open", "series_ticker": "KXFED", "limit": 200},
                    )
                    for m in (resp or {}).get("markets", []):
                        t = m.get("ticker", "")
                        if t not in self._pairs:
                            continue
                        pair = self._pairs[t]
                        # API returns dollars (0.0–1.0); multiply by 100 for cents
                        pair.kalshi_bid = float(m.get("yes_bid_dollars") or 0) * 100
                        pair.kalshi_ask = float(m.get("yes_ask_dollars") or 0) * 100
                        pair.kalshi_ts  = time.monotonic()
                        self._check_divergence(t)
                except Exception as exc:
                    log.debug("arb: Kalshi poll error: %s", exc)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    # ── Divergence checker ─────────────────────────────────────────────────────

    def _check_divergence(self, ticker: str) -> None:
        """Called on every price update — must not block."""
        pair = self._pairs.get(ticker)
        if not pair or self._halt:
            return
        if time.monotonic() < pair.cooldown_until:
            return
        if not pair.poly_price or not pair.kalshi_mid:
            return

        poly_cents = pair.poly_price * 100.0
        diff       = abs(poly_cents - pair.kalshi_mid)

        if diff >= ARB_MIN_CENTS:
            if pair.divergence_since is None:
                pair.divergence_since = time.monotonic()
                self._stats["detected"] += 1
                log.debug(
                    "arb: divergence start  %s  poly=%.1f¢  kalshi=%.1f¢  Δ=%.1f¢",
                    ticker, poly_cents, pair.kalshi_mid, diff,
                )
            elif (time.monotonic() - pair.divergence_since) * 1_000 >= HOLD_MS:
                pair.divergence_since = None   # prevent double-fire
                asyncio.get_event_loop().create_task(self._fire(ticker))
        else:
            if pair.divergence_since is not None:
                log.debug("arb: divergence closed  %s  (held <%.0fms)", ticker, HOLD_MS)
            pair.divergence_since = None

    # ── Order placement ────────────────────────────────────────────────────────

    async def _fire(self, ticker: str) -> None:
        """Place a Kalshi limit buy at the current ask.  Goal: < 100 ms wall clock."""
        pair = self._pairs.get(ticker)
        if not pair or self._halt or not self._client:
            return
        if time.monotonic() < pair.cooldown_until:
            return

        # Price staleness gate: reject if either leg's price is older than
        # ARB_MAX_PRICE_AGE_S. The divergence we detected may already have
        # closed; trading on stale quotes is worse than skipping.
        _now_mono = time.monotonic()
        _poly_age = _now_mono - pair.poly_ts
        _kal_age  = _now_mono - pair.kalshi_ts
        if _poly_age > ARB_MAX_PRICE_AGE_S or _kal_age > ARB_MAX_PRICE_AGE_S:
            log.debug(
                "arb: STALE  %s  poly_age=%.2fs  kalshi_age=%.2fs  (max %.1fs) — skip",
                ticker, _poly_age, _kal_age, ARB_MAX_PRICE_AGE_S,
            )
            return

        # Re-verify divergence with freshest prices
        poly_cents = pair.poly_price * 100.0
        diff       = poly_cents - pair.kalshi_mid
        if abs(diff) < ARB_MIN_CENTS:
            return

        # Direction: poly > kalshi → Kalshi YES is cheap → buy YES at kalshi_ask
        #            poly < kalshi → Kalshi YES is rich  → buy NO  at (100 − kalshi_bid)
        if diff > 0:
            side        = "yes"
            price_key   = "yes_price"
            limit_cents = int(pair.kalshi_ask) if pair.kalshi_ask else int(pair.kalshi_mid) + 1
        else:
            side        = "no"
            price_key   = "no_price"
            limit_cents = int(100 - pair.kalshi_bid) if pair.kalshi_bid else int(100 - pair.kalshi_mid) + 1

        limit_cents = max(1, min(99, limit_cents))

        # Check position conflict (single Redis read — only blocking point)
        try:
            if self._redis and await self._redis.hexists(EP_POSITIONS, ticker):
                log.debug("arb: %s already held — skip", ticker)
                pair.cooldown_until = time.monotonic() + 30.0
                return
        except Exception:
            pass

        # ── Capital cap ─────────────────────────────────────────────────────────
        # Skip firing if total open Kalshi exposure + this trade's cost would
        # exceed ARB_MAX_TOTAL_EXP_PCT of balance. Prior versions had no gate —
        # a burst of arb fires could drain the account. Fail-open on balance
        # read error (latency-sensitive — we'd rather trade than miss the window).
        trade_cost_cents = MAX_CONTRACTS * limit_cents
        if self._redis:
            try:
                _bals    = await self._redis.hgetall(EP_BALANCE)
                _intel_v = None
                for _k, _v in _bals.items():
                    _ks = _k.decode() if isinstance(_k, bytes) else _k
                    if "intel" in _ks.lower():
                        _vs = _v.decode() if isinstance(_v, bytes) else _v
                        _intel_v = json.loads(_vs)
                        break
                if _intel_v and _intel_v.get("balance_cents", 0) > 0:
                    _bal = int(_intel_v["balance_cents"])
                    _pos = await self._redis.hgetall(EP_POSITIONS)
                    _exp = 0
                    for _pk, _pv in _pos.items():
                        try:
                            _pvs = _pv.decode() if isinstance(_pv, bytes) else _pv
                            _p   = json.loads(_pvs)
                            if _p.get("user_bet"):
                                continue
                            _e  = int(_p.get("entry_cents", 50))
                            _c  = int(_p.get("contracts_filled") or _p.get("contracts", 1))
                            _exp += (100 - _e) * _c if _p.get("side") == "no" else _e * _c
                        except Exception:
                            continue
                    if (_exp + trade_cost_cents) / _bal > ARB_MAX_TOTAL_EXP_PCT:
                        log.info(
                            "arb: CAP  %s  exposure %.0f¢ + %d¢ > %.0f%% × %d¢ — skip",
                            ticker, _exp, trade_cost_cents,
                            ARB_MAX_TOTAL_EXP_PCT * 100, _bal,
                        )
                        pair.cooldown_until = time.monotonic() + COOLDOWN_S
                        return
            except Exception as _cap_exc:
                log.warning("arb: capital cap read error (firing anyway): %s", _cap_exc)

        self._stats["fired"] += 1
        t_fire = time.monotonic()
        payload = {
            "action":  "buy",
            "type":    "limit",
            "ticker":  ticker,
            "side":    side,
            "count":   MAX_CONTRACTS,
            price_key: limit_cents,
        }
        log.info(
            "arb: FIRE  %-38s  side=%s  poly=%.1f¢  kalshi_mid=%.1f¢  Δ%+.1f¢  limit=%d¢",
            ticker, side, poly_cents, pair.kalshi_mid, diff, limit_cents,
        )

        try:
            resp     = await asyncio.to_thread(self._client.post, "/portfolio/orders", payload)
            order_id = (resp or {}).get("order", {}).get("order_id")
            elapsed  = (time.monotonic() - t_fire) * 1_000

            if order_id:
                self._stats["filled"] += 1
                log.info("arb: FILL  %-38s  order_id=%s  %.0fms", ticker, order_id, elapsed)
                if self._redis:
                    # entry_cents convention: always the YES price (0-100) regardless
                    # of side. For a NO leg filled at NO-price `limit_cents`, the
                    # YES-equivalent is `100 - limit_cents`. Prior versions stored
                    # the raw NO price, which inverted every downstream P&L and
                    # exit-trigger calc for arb NO legs.
                    entry_cents_yes = (100 - limit_cents) if side == "no" else limit_cents

                    # Derive meeting from ticker so the concentration-limit gate in
                    # ep_exec counts arb-placed positions (KXFED / KXGDP / etc.).
                    meeting = ""
                    if ticker.startswith(("KXFED-", "KXGDP-", "KXCPI-", "KXNFP-", "KXINFLATION-")):
                        _parts = ticker.rsplit("-T", 1)
                        meeting = _parts[0] if len(_parts) > 1 else ""

                    # Race re-check: another service (ep_exec most likely) may have
                    # written to this ticker during our ~100ms order-placement HTTP
                    # call. If so, our hset would overwrite their record. Cancel our
                    # order and abort rather than corrupt their state.
                    try:
                        raced = await self._redis.hexists(EP_POSITIONS, ticker)
                    except Exception:
                        raced = False
                    if raced:
                        log.warning(
                            "arb: RACE %s — another writer claimed ticker during "
                            "order placement; cancelling order_id=%s",
                            ticker, order_id,
                        )
                        try:
                            await asyncio.to_thread(
                                self._client._request, "DELETE",
                                f"/portfolio/orders/{order_id}",
                            )
                        except Exception as _cx:
                            log.error(
                                "arb: cancel FAILED after race; order_id=%s open on "
                                "Kalshi with no Redis record: %s",
                                order_id, _cx,
                            )
                        return

                    # Write with fill_confirmed=False so ep_exec's _fill_poll_loop
                    # picks up the resting order. Without this flag the position
                    # stays as a phantom in Redis if the limit order never fills.
                    #
                    # If the Redis write fails after the Kalshi order succeeded we
                    # have an orphan. Cancel the Kalshi order to keep Redis as the
                    # source of truth. (ep_exec's orphan reconciliation would also
                    # eventually pick it up, but an explicit cancel is cleaner.)
                    try:
                        await self._redis.hset(EP_POSITIONS, ticker, json.dumps({
                            "ticker":         ticker,
                            "side":           side,
                            "contracts":      MAX_CONTRACTS,
                            "contracts_filled": 0,
                            "entry_cents":    entry_cents_yes,
                            "meeting":        meeting,
                            "entered_at":     datetime.now(timezone.utc).isoformat(),
                            "model_source":   "arb_realtime",
                            "strategy":       "poly_arb",
                            "order_id":       order_id,
                            "fill_confirmed": False,
                            "pending":        False,
                        }))
                    except Exception as _re:
                        log.error(
                            "arb: Redis hset FAILED after fill; cancelling order_id=%s "
                            "to prevent orphan: %s", order_id, _re,
                        )
                        try:
                            await asyncio.to_thread(
                                self._client._request, "DELETE",
                                f"/portfolio/orders/{order_id}",
                            )
                        except Exception as _cx:
                            log.error(
                                "arb: cancel FAILED after Redis error; order_id=%s "
                                "open on Kalshi with no Redis record (ep_exec "
                                "orphan reconciliation will eventually pick it up): %s",
                                order_id, _cx,
                            )
            else:
                self._stats["rejected"] += 1
                log.info("arb: REJECTED  %s  %.0fms  resp=%s", ticker, elapsed, str(resp)[:120])

        except Exception as exc:
            log.warning("arb: order error for %s: %s", ticker, exc)

        # Always enter cooldown — win or lose
        pair.cooldown_until = time.monotonic() + COOLDOWN_S

    # ── Background tasks ───────────────────────────────────────────────────────

    async def _halt_watcher(self) -> None:
        while True:
            try:
                if self._redis:
                    val = await self._redis.hget(EP_CONFIG, "HALT_TRADING")
                    was, self._halt = self._halt, val in ("1", b"1")
                    if self._halt and not was:
                        log.warning("arb: HALT_TRADING active")
                    elif not self._halt and was:
                        log.info("arb: HALT_TRADING cleared")
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _map_refresher(self, http: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(MAP_REFRESH_S)
            log.info("arb: refreshing market mapping...")
            await self._build_pairs(http)

    async def _status_writer(self) -> None:
        while True:
            await asyncio.sleep(300)
            if not self._redis:
                continue
            try:
                divergent = [
                    {"ticker": t, "poly": round(p.poly_price * 100, 1),
                     "kalshi": round(p.kalshi_mid, 1),
                     "diff": round(abs(p.poly_price * 100 - p.kalshi_mid), 1)}
                    for t, p in self._pairs.items()
                    if p.poly_price and p.kalshi_mid
                    and abs(p.poly_price * 100 - p.kalshi_mid) >= 1.0
                ][:10]
                await self._redis.set(EP_ARB_STATUS, json.dumps({
                    "ts":          datetime.now(timezone.utc).isoformat(),
                    "pairs":       len(self._pairs),
                    "halt":        self._halt,
                    "stats":       self._stats,
                    "divergent":   divergent,
                }, default=str), ex=3600)
                log.info(
                    "arb: stats  detected=%d  fired=%d  filled=%d  rejected=%d  "
                    "pairs=%d  divergent=%d",
                    self._stats["detected"], self._stats["fired"],
                    self._stats["filled"], self._stats["rejected"],
                    len(self._pairs), len(divergent),
                )
            except Exception:
                pass

    # ── Main ───────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        import redis.asyncio as aioredis
        self._redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=False,
            socket_connect_timeout=5,
        )

        if not self._client:
            log.warning("arb: no Kalshi credentials — monitor-only mode")

        async with httpx.AsyncClient(timeout=10.0) as http:
            log.info("arb: building initial market mapping...")
            await self._build_pairs(http)
            log.info(
                "arb: ready  pairs=%d  min=%.1f¢  hold=%dms  "
                "poly_poll=%dms  kalshi_poll=%dms  cooldown=%ds",
                len(self._pairs), ARB_MIN_CENTS, HOLD_MS,
                POLY_POLL_MS, KALSHI_POLL_MS, COOLDOWN_S,
            )
            await asyncio.gather(
                self._poll_poly(),
                self._poll_kalshi(),
                self._halt_watcher(),
                self._map_refresher(http),
                self._status_writer(),
            )

        await self._redis.aclose()


async def main() -> None:
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    engine = ArbEngine()
    try:
        await engine.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("arb: shutdown")


if __name__ == "__main__":
    asyncio.run(main())
