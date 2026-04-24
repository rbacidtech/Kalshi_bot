"""
ep_ob_depth.py — Kalshi order book depth imbalance fast-reaction.

Runs exclusively on Exec (Chicago) node.

Strategy:
  • Subscribe via Kalshi WS to orderbook_delta for tracked KXFED / KXCPI / KXGDP markets.
  • Maintain a local top-of-book (within DEPTH_BAND_C cents of mid) depth state.
  • When total YES depth or NO depth shifts by ≥ IMBALANCE_THRESHOLD (30%) in a single
    delta tick AND the incoming large order is within SIGNAL_BAND_C cents of current mid:
    place a follow-on limit order in the same direction.
  • Per-ticker cooldown prevents double-firing.

Why this works:
  Large resting orders on thin Kalshi books reveal institutional conviction.
  Retail takes 30–60 s to reprice; at 50 ms placement latency from Chicago, the
  window between depth shift and price move is reliably capturable.
"""

import asyncio
import json
import logging
import os
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import httpx
from dotenv import load_dotenv
load_dotenv()

from ep_config import cfg, log
from kalshi_bot.auth   import KalshiAuth
from kalshi_bot.client import KalshiClient

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL           = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EP_CONFIG           = "ep:config"
EP_PRICES           = "ep:prices"
EP_OB_STATUS        = "ep:ob_depth:status"

# Kalshi WS — derive from BASE_URL
_KAL_HOST           = cfg.BASE_URL.split("//")[-1].split("/")[0]
KAL_WS_URL          = f"wss://{_KAL_HOST}/trade-api/ws/v2"
KAL_WS_PATH         = "/trade-api/ws/v2"

TRACKED_SERIES      = os.getenv("OB_SERIES", "KXFED,KXCPI,KXGDP").split(",")
MAX_MARKETS         = int(os.getenv("OB_MAX_MARKETS",    "20"))
DEPTH_BAND_C        = float(os.getenv("OB_DEPTH_BAND_C", "5.0"))  # cents around mid
SIGNAL_BAND_C       = float(os.getenv("OB_SIGNAL_BAND_C","3.0"))  # incoming order ≤N¢ from mid
IMBALANCE_THRESH    = float(os.getenv("OB_IMBAL_THRESH", "0.30")) # 30% ratio shift
MIN_ORDER_SIZE      = float(os.getenv("OB_MIN_ORDER_SZ", "50.0")) # contracts in new level
ORDER_LOTS          = int(os.getenv("OB_ORDER_LOTS",     "2"))
COOLDOWN_S          = int(os.getenv("OB_COOLDOWN_S",     "60"))
RECONNECT_DELAY_S   = int(os.getenv("OB_RECONNECT_S",   "5"))

# Signal publishing — as of 2026-04-24, this service publishes to ep:signals
# instead of placing orders directly. ep_exec then consumes and applies all
# standard gates (dedup, Kelly, exposure, halt, etc.). Previously this service
# placed orders directly, bypassing every gate — see KNOWN_GAPS.md C3.
EP_SIGNALS          = "ep:signals"
EP_SIGNALS_MAXLEN   = 10_000
SIGNAL_TTL_MS       = int(os.getenv("OB_SIGNAL_TTL_MS",  "15000"))  # 15s — short
# Edge injected into signals: OB imbalance is a directional momentum signal,
# not a fundamental fair-value forecast. We assert a small edge (~3¢) in the
# imbalance direction. If the operator's override_edge_threshold is tighter
# than this, ep_exec will (correctly) reject these signals — that is the
# intended behavior under a tight-EV regime.
OB_EDGE_DECIMAL     = float(os.getenv("OB_EDGE_DECIMAL", "0.03"))


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BookState:
    """Local order book for one market."""
    ticker:       str
    yes_bids:     Dict[str, float] = field(default_factory=dict)  # price_str → qty
    no_bids:      Dict[str, float] = field(default_factory=dict)
    prev_ratio:   float = 0.5      # previous YES depth ratio
    cooldown_until: float = 0.0

    def mid_cents(self) -> float:
        """Estimate mid from best YES bid + best NO bid.

        Returns 0.0 when BOTH books are empty — callers must guard against
        mid=0 before any price-dependent logic (line 190 check in _apply_delta
        rejects but downstream _publish_signal also re-checks).
        """
        top_yes = max((float(p) for p in self.yes_bids), default=0.0) * 100
        top_no  = max((float(p) for p in self.no_bids),  default=0.0) * 100
        if top_yes and top_no:
            return (top_yes + (100 - top_no)) / 2.0
        return top_yes or (100 - top_no)

    def depth_ratio(self, mid: float) -> float:
        """
        YES depth ratio = total YES contracts within DEPTH_BAND_C of mid /
                          (total YES + total NO contracts in same band).

        Note on NO bid geometry: self.no_bids stores NO bid prices (what a
        buyer of NO is willing to pay). The equivalent YES ask price is
        100 - no_bid. The below arithmetic converts NO bids to YES-space
        before comparing to `mid` so YES and NO legs are measured in the
        same coordinate.
        """
        yes_depth = sum(
            qty for p, qty in self.yes_bids.items()
            if abs(float(p) * 100 - mid) <= DEPTH_BAND_C
        )
        no_depth = sum(
            qty for p, qty in self.no_bids.items()
            if abs((100 - float(p) * 100) - mid) <= DEPTH_BAND_C
        )
        total = yes_depth + no_depth
        return yes_depth / total if total > 0 else 0.5


# ── Engine ────────────────────────────────────────────────────────────────────

class ObDepthEngine:

    def __init__(self) -> None:
        self._auth = KalshiAuth(
            api_key_id       = cfg.API_KEY_ID or "",
            private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        ) if cfg.API_KEY_ID else None
        self._client: Optional[KalshiClient] = KalshiClient(
            base_url = cfg.BASE_URL,
            auth     = self._auth,
        ) if self._auth else None

        self._redis       = None
        self._halt        = False
        self._books:  Dict[str, BookState]  = {}
        self._tickers: List[str]            = []
        self._stats = {"signals": 0, "signals_published": 0}

    # ── Halt check ────────────────────────────────────────────────────────────

    async def _check_halt(self) -> bool:
        try:
            if self._redis:
                val = await self._redis.hget(EP_CONFIG, "HALT_TRADING")
                return val in ("1", b"1")
        except Exception:
            pass
        return False

    # ── Market discovery ─────────────────────────────────────────────────────

    async def _fetch_tickers(self) -> List[str]:
        """Return tickers for all open TRACKED_SERIES markets, capped at MAX_MARKETS."""
        if not self._client:
            return []
        tickers = []
        for series in TRACKED_SERIES:
            try:
                resp = await asyncio.to_thread(
                    self._client.get, "/markets",
                    {"status": "open", "series_ticker": series.strip(), "limit": MAX_MARKETS},
                )
                for m in (resp or {}).get("markets", []):
                    t = m.get("ticker", "")
                    if t:
                        tickers.append(t)
            except Exception as exc:
                log.warning("ob: market fetch error for %s: %s", series, exc)
        tickers = tickers[:MAX_MARKETS]
        log.info("ob: tracking %d markets  %s…", len(tickers), tickers[:3])
        return tickers

    # ── Book state management ─────────────────────────────────────────────────

    def _apply_snapshot(self, msg: dict) -> None:
        ticker = msg.get("market_ticker", "")
        if not ticker:
            return
        book = self._books.setdefault(ticker, BookState(ticker=ticker))
        for price_str, qty_str in msg.get("yes_dollars_fp", []):
            qty = float(qty_str)
            if qty > 0:
                book.yes_bids[price_str] = qty
            else:
                book.yes_bids.pop(price_str, None)
        for price_str, qty_str in msg.get("no_dollars_fp", []):
            qty = float(qty_str)
            if qty > 0:
                book.no_bids[price_str] = qty
            else:
                book.no_bids.pop(price_str, None)
        mid = book.mid_cents()
        book.prev_ratio = book.depth_ratio(mid)

    def _apply_delta(self, msg: dict) -> Optional[str]:
        """
        Apply a depth delta.  Returns ticker if a significant imbalance was detected,
        else None.  Handles both 'yes'/'no' and 'yes_dollars_fp'/'no_dollars_fp' keys.
        """
        ticker = msg.get("market_ticker", "")
        if not ticker or ticker not in self._books:
            return None
        book = self._books[ticker]

        if time.monotonic() < book.cooldown_until:
            return None

        # Detect which field names the server is using
        yes_changes = msg.get("yes") or msg.get("yes_dollars_fp") or []
        no_changes  = msg.get("no")  or msg.get("no_dollars_fp")  or []

        mid = book.mid_cents()
        if mid <= 0:
            return None

        # Track largest new order in this delta (for SIGNAL_BAND_C check)
        max_new_yes = 0.0
        max_new_yes_price = 0.0
        max_new_no  = 0.0
        max_new_no_price  = 0.0

        for price_str, qty_str in yes_changes:
            qty = float(qty_str)
            old = book.yes_bids.get(price_str, 0.0)
            added = qty - old
            if added > max_new_yes:
                max_new_yes = added
                max_new_yes_price = float(price_str) * 100
            if qty > 0:
                book.yes_bids[price_str] = qty
            else:
                book.yes_bids.pop(price_str, None)

        for price_str, qty_str in no_changes:
            qty = float(qty_str)
            old = book.no_bids.get(price_str, 0.0)
            added = qty - old
            if added > max_new_no:
                max_new_no = added
                max_new_no_price = float(price_str) * 100  # NO bid price in cents
            if qty > 0:
                book.no_bids[price_str] = qty
            else:
                book.no_bids.pop(price_str, None)

        new_ratio = book.depth_ratio(mid)
        shift     = new_ratio - book.prev_ratio

        # Imbalance detected?
        if abs(shift) < IMBALANCE_THRESH:
            book.prev_ratio = new_ratio
            return None

        # The dominant new order must be close to the current mid
        if shift > 0:
            # YES side surge: check if the new YES order is within SIGNAL_BAND_C of mid
            if max_new_yes < MIN_ORDER_SIZE:
                book.prev_ratio = new_ratio
                return None
            if abs(max_new_yes_price - mid) > SIGNAL_BAND_C:
                book.prev_ratio = new_ratio
                return None
            direction = "yes"
        else:
            # NO side surge: check if new NO order is near equivalent YES ask
            if max_new_no < MIN_ORDER_SIZE:
                book.prev_ratio = new_ratio
                return None
            no_equiv_yes_ask = 100 - max_new_no_price  # equivalent YES ask price
            if abs(no_equiv_yes_ask - mid) > SIGNAL_BAND_C:
                book.prev_ratio = new_ratio
                return None
            direction = "no"

        log.info(
            "ob: IMBALANCE  %-30s  shift=%+.2f  ratio=%.2f→%.2f  "
            "direction=%s  mid=%.1f¢  new_qty=%.0f",
            ticker, shift, book.prev_ratio, new_ratio, direction, mid,
            max_new_yes if direction == "yes" else max_new_no,
        )
        self._stats["signals"] += 1
        book.prev_ratio = new_ratio
        return direction   # return direction string as the trigger

    # ── Signal publishing ─────────────────────────────────────────────────────

    async def _publish_signal(self, ticker: str, direction: str) -> None:
        """
        Publish an OB-imbalance signal to ep:signals.

        ep_exec consumes the stream and applies every standard gate (dedup,
        Kelly sizing, balance, exposure, halt, meeting-concentration, etc.).
        This service owns the detection but not the trading decision.
        Prior behavior: direct POST /portfolio/orders with zero gates.
        """
        if await self._check_halt():
            return

        book = self._books.get(ticker)
        if not book:
            return

        mid_cents = book.mid_cents()
        if mid_cents <= 0:
            return

        # SignalMessage convention: market_price is always the YES mid (decimal
        # 0-1). fair_value is P(YES wins) per the model — for an imbalance
        # momentum signal, we assert a small edge in the direction of the shift.
        mid_decimal = mid_cents / 100.0
        if direction == "yes":
            side       = "yes"
            fair_value = min(0.99, mid_decimal + OB_EDGE_DECIMAL)
        else:
            side       = "no"
            # NO signal means we expect YES price to DROP by OB_EDGE_DECIMAL.
            fair_value = max(0.01, mid_decimal - OB_EDGE_DECIMAL)

        # Confidence scaled by the imbalance shift that triggered this signal.
        # Bounded to [0.60, 0.85] — OB imbalance is suggestive, not conclusive.
        confidence = min(0.85, 0.60 + abs(book.prev_ratio - 0.5))

        # Meeting tag so the concentration gate at ep_exec counts OB positions
        # toward per-meeting limits (same derivation ep_exec / ep_arb use).
        meeting = ""
        if ticker.startswith(("KXFED-", "KXGDP-", "KXCPI-", "KXNFP-", "KXINFLATION-")):
            parts = ticker.rsplit("-T", 1)
            meeting = parts[0] if len(parts) > 1 else ""

        book.cooldown_until = time.monotonic() + COOLDOWN_S

        # Build the signal dict. We stay schema-compatible by matching the
        # fields SignalMessage serializes; exec will rehydrate via from_redis.
        payload = {
            "signal_id":      _uuid.uuid4().__str__(),
            "schema_version": "1",
            "msg_type":       "SIGNAL",
            "source_node":    os.getenv("NODE_ID", "ob_depth"),
            "ts_us":          int(time.time() * 1_000_000),
            "ttl_ms":         SIGNAL_TTL_MS,
            "asset_class":    "kalshi",
            "strategy":       "ob_imbalance",
            "category":       "imbalance",
            "ticker":         ticker,
            "exchange":       "kalshi",
            "side":           side,
            "market_price":   round(mid_decimal, 4),
            "fair_value":     round(fair_value, 4),
            "edge":           round(OB_EDGE_DECIMAL, 4),
            "fee_adjusted_edge": round(OB_EDGE_DECIMAL - 0.02, 4),  # rough fee net
            "confidence":     round(confidence, 4),
            "suggested_size": ORDER_LOTS,
            "kelly_fraction": 0.25,
            "priority":       2,
            "risk_flags":     ["OB_IMBALANCE"],
            "model_source":   "ob_depth",
            "meeting":        meeting,
        }

        log.info(
            "ob: SIGNAL  %-30s  side=%s  mid=%.1f¢  fair=%.3f  edge=%.3f  conf=%.2f",
            ticker, side, mid_cents, fair_value, OB_EDGE_DECIMAL, confidence,
        )

        if self._redis is None:
            log.warning("ob: no Redis client — signal not published for %s", ticker)
            return

        try:
            await self._redis.xadd(
                EP_SIGNALS,
                {"payload": json.dumps(payload)},
                maxlen=EP_SIGNALS_MAXLEN,
                approximate=True,
            )
            self._stats["signals_published"] += 1
        except Exception as exc:
            log.warning("ob: signal publish error for %s: %s", ticker, exc)

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        import websockets
        backoff = RECONNECT_DELAY_S

        while True:
            try:
                hdrs = self._auth.sign("GET", KAL_WS_PATH)
                async with websockets.connect(
                    KAL_WS_URL,
                    additional_headers = hdrs,
                    open_timeout       = 10,
                    ping_interval      = 30,
                    ping_timeout       = 10,
                ) as ws:
                    log.info("ob: WS connected  url=%s", KAL_WS_URL)
                    backoff = RECONNECT_DELAY_S  # reset on success

                    # Subscribe to orderbook deltas for all tracked tickers
                    if self._tickers:
                        await ws.send(json.dumps({
                            "id": 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_tickers": self._tickers,
                            },
                        }))

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        msg_type = data.get("type", "")
                        msg      = data.get("msg", {})

                        if msg_type == "orderbook_snapshot":
                            self._apply_snapshot(msg)

                        elif msg_type == "orderbook_delta":
                            direction = self._apply_delta(msg)
                            if direction:
                                asyncio.get_event_loop().create_task(
                                    self._publish_signal(msg.get("market_ticker", ""), direction)
                                )

                        elif msg_type == "error":
                            log.warning("ob: WS server error: %s", data)

            except Exception as exc:
                log.warning("ob: WS disconnected: %s — retry in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(120, backoff * 2)
                # Refresh tickers and rebuild auth headers on reconnect
                if self._client:
                    self._tickers = await self._fetch_tickers()

    # ── Status writer ─────────────────────────────────────────────────────────

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            if not self._redis:
                continue
            try:
                active = [
                    {"ticker": t, "mid": round(b.mid_cents(), 1), "ratio": round(b.prev_ratio, 3)}
                    for t, b in self._books.items()
                    if b.mid_cents() > 0
                ]
                await self._redis.set(EP_OB_STATUS, json.dumps({
                    "ts":       datetime.now(timezone.utc).isoformat(),
                    "markets":  len(self._books),
                    "stats":    self._stats,
                    "halt":     self._halt,
                    "sample":   active[:5],
                }, default=str), ex=3600)
                log.info("ob: stats  signals=%d  published=%d  markets=%d",
                         self._stats["signals"],
                         self._stats["signals_published"], len(self._books))
            except Exception:
                pass

    # ── Ticker refresh ────────────────────────────────────────────────────────

    async def _ticker_refresh_loop(self) -> None:
        """Refresh tracked tickers every 30 min (new markets open/close)."""
        while True:
            await asyncio.sleep(1800)
            if self._client:
                new_tickers = await self._fetch_tickers()
                if set(new_tickers) != set(self._tickers):
                    log.info("ob: ticker list changed — reconnect WS to resubscribe")
                    self._tickers = new_tickers
                    # The _ws_loop will reconnect and subscribe to the new list

    # ── Main ──────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        import redis.asyncio as aioredis
        self._redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=False,
            socket_connect_timeout=5,
        )

        if not self._auth:
            log.warning("ob: no Kalshi credentials — depth monitoring disabled")
            while True:
                await asyncio.sleep(3600)

        self._tickers = await self._fetch_tickers()
        if not self._tickers:
            log.warning("ob: no tracked markets — check TRACKED_SERIES and Kalshi API")

        log.info("ob: starting  ws=%s  series=%s  band=%.0f¢  threshold=%.0f%%",
                 KAL_WS_URL, TRACKED_SERIES,
                 DEPTH_BAND_C, IMBALANCE_THRESH * 100)

        await asyncio.gather(
            self._ws_loop(),
            self._status_loop(),
            self._ticker_refresh_loop(),
        )

        await self._redis.aclose()


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    engine = ObDepthEngine()
    try:
        await engine.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("ob: shutdown")


if __name__ == "__main__":
    asyncio.run(main())
