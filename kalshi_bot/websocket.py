"""
websocket.py — Real-time Kalshi price feed via WebSocket.

Replaces the 120-second REST polling loop with a persistent WebSocket
connection that pushes price updates the instant they happen on Kalshi.

How it works:
  1. Authenticates with Kalshi's WSS endpoint using the same RSA-PSS
     credentials as the REST client
  2. Subscribes to orderbook_delta and trade channels for all open
     FOMC market tickers
  3. Maintains a local orderbook mirror and writes price updates to
     the shared BotState the moment they arrive
  4. Reconnects automatically on disconnect with exponential backoff
  5. Runs in a background daemon thread — main loop continues unaffected

The main loop still runs on its timer but now reads from BotState
(already up-to-date from WebSocket) rather than making REST calls
for current prices. REST is only used for order placement and
account data which WebSocket doesn't cover.

Speed gain: signal detection latency drops from ~120s to <1s.
On FOMC meeting days when prices move fast, this is the difference
between catching an edge and missing it entirely.

Kalshi WebSocket docs:
  wss://demo-api.kalshi.co/trade-api/ws/v2      (demo)
  wss://api.elections.kalshi.com/trade-api/ws/v2 (production)
"""

import json
import time
import logging
import threading
from typing import TYPE_CHECKING

import websocket   # websocket-client package

if TYPE_CHECKING:
    from .state import BotState
    from .auth  import KalshiAuth

log = logging.getLogger(__name__)

# WebSocket URLs
_WS_DEMO = "wss://demo-api.kalshi.co/trade-api/ws/v2"
_WS_LIVE = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Reconnect settings
_INITIAL_BACKOFF = 2.0    # seconds
_MAX_BACKOFF     = 60.0   # seconds
_BACKOFF_FACTOR  = 2.0


class KalshiWebSocket:
    """
    Persistent WebSocket connection to Kalshi's real-time feed.

    Writes all incoming price data directly to the shared BotState object.
    Runs in a daemon thread — call start() once at bot startup.

    Args:
        state:      Shared BotState instance
        auth:       KalshiAuth instance (for signing the initial REST auth call)
        paper:      If True, connect to demo endpoint
        tickers:    Initial list of market tickers to subscribe to.
                    More can be added via subscribe_tickers() at any time.
    """

    def __init__(
        self,
        state:   "BotState",
        auth:    "KalshiAuth",
        paper:   bool = True,
        tickers: list[str] = None,
    ):
        self.state        = state
        self.auth         = auth
        self.paper        = paper
        self._tickers     = set(tickers or [])
        self._ws          = None
        self._thread      = None
        self._running     = False
        self._backoff     = _INITIAL_BACKOFF
        self._orderbooks  = {}   # local mirror: ticker → {yes: [], no: []}

    @property
    def _ws_url(self) -> str:
        return _WS_DEMO if self.paper else _WS_LIVE

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Start the WebSocket in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop,
            name="kalshi-ws",
            daemon=True,
        )
        self._thread.start()
        log.info("WebSocket thread started (endpoint=%s).", self._ws_url)

    def stop(self):
        """Gracefully close the WebSocket."""
        self._running = False
        if self._ws:
            self._ws.close()
        log.info("WebSocket stopped.")

    def subscribe_tickers(self, tickers: list[str]):
        """
        Subscribe to additional tickers at runtime.
        Call this when the scanner finds new FOMC markets.
        """
        new = set(tickers) - self._tickers
        if not new:
            return
        self._tickers.update(new)
        if self._ws and self.state.ws_connected:
            self._send_subscriptions(new)

    # ── Connection loop ───────────────────────────────────────────────────────

    def _run_loop(self):
        """Outer reconnect loop. Reconnects with backoff on any disconnect."""
        while self._running:
            try:
                log.info("WebSocket connecting to %s...", self._ws_url)
                self._connect()
                # If we get here cleanly, reset backoff
                self._backoff = _INITIAL_BACKOFF
            except Exception as exc:
                log.warning("WebSocket error: %s — reconnecting in %.1fs",
                            exc, self._backoff)

            if not self._running:
                break

            self.state.set_ws_connected(False)
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * _BACKOFF_FACTOR, _MAX_BACKOFF)

    def _connect(self):
        """Establish one WebSocket connection and block until it closes."""
        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
            header     = self._auth_headers(),
        )
        # ping_interval=0 disables websocket-client's built-in RFC 6455 PING frames.
        # Kalshi's server does not respond to these, causing spurious ping/pong
        # timeouts every ~2.5 min. Application-level keepalive handles liveness.
        self._ws.run_forever(ping_interval=0)

    def _auth_headers(self) -> list[str]:
        """
        Build HTTP upgrade headers with RSA-PSS auth signature.
        Kalshi requires the same auth headers on the WebSocket handshake
        as on REST requests.
        """
        try:
            signed = self.auth.sign("GET", "/trade-api/ws/v2")
            return [f"{k}: {v}" for k, v in signed.items()]
        except Exception:
            return []   # NoAuth — demo mode without credentials

    # ── WebSocket event handlers ──────────────────────────────────────────────

    def _on_open(self, ws):
        log.info("WebSocket connected.")
        self.state.set_ws_connected(True)
        self._backoff = _INITIAL_BACKOFF

        # Subscribe to all known FOMC tickers
        if self._tickers:
            self._send_subscriptions(self._tickers)

    def _on_message(self, ws, raw: str):
        try:
            msg = json.loads(raw)
            self._dispatch(msg)
        except json.JSONDecodeError:
            log.debug("Non-JSON WebSocket message: %s", raw[:200])
        except Exception as exc:
            log.debug("Message handler error: %s", exc)

    def _on_error(self, ws, error):
        log.warning("WebSocket error: %s", error)

    def _on_close(self, ws, code, reason):
        log.info("WebSocket closed (code=%s reason=%s).", code, reason)
        self.state.set_ws_connected(False)

    # ── Subscription management ───────────────────────────────────────────────

    def _send_subscriptions(self, tickers):
        """
        Send subscription commands for a set of tickers.

        Subscribes to two channels per ticker:
          - orderbook_delta: incremental order book updates (best bid/ask)
          - trade:           every matched trade (last price, volume)
        """
        for ticker in tickers:
            for channel in ["orderbook_delta", "trade"]:
                msg = json.dumps({
                    "id":     1,
                    "cmd":    "subscribe",
                    "params": {
                        "channels": [channel],
                        "market_tickers": [ticker],
                    },
                })
                try:
                    self._ws.send(msg)
                    log.debug("Subscribed to %s:%s", channel, ticker)
                except Exception as exc:
                    # Socket closed mid-subscribe (normal during reconnect) — not a warning
                    log.debug("Subscription failed for %s:%s — %s. Aborting.",
                              channel, ticker, exc)
                    return

    # ── Message dispatcher ────────────────────────────────────────────────────

    def _dispatch(self, msg: dict):
        """Route incoming messages to the appropriate handler."""
        msg_type = msg.get("type") or msg.get("msg_type", "")

        if msg_type in ("orderbook_snapshot", "orderbook_delta"):
            self._handle_orderbook(msg)
        elif msg_type == "trade":
            self._handle_trade(msg)
        elif msg_type == "subscribed":
            log.debug("Subscription confirmed: %s", msg)
        elif msg_type == "error":
            log.warning("WebSocket server error: %s", msg)

    def _handle_orderbook(self, msg: dict):
        """
        Process an orderbook snapshot or delta.

        Kalshi sends:
          - snapshot: full book on first subscribe
          - delta:    incremental changes (price, size) pairs

        We maintain a local mirror and write best bid/ask to BotState.
        """
        ticker = msg.get("market_ticker") or msg.get("ticker", "")
        if not ticker:
            return

        data = msg.get("msg", msg)

        if msg.get("type") == "orderbook_snapshot":
            # Full replacement
            self._orderbooks[ticker] = {
                "yes": data.get("yes", []),
                "no":  data.get("no",  []),
            }
        else:
            # Incremental delta — apply changes
            book = self._orderbooks.setdefault(ticker, {"yes": [], "no": []})
            self._apply_delta(book["yes"], data.get("yes", []))
            self._apply_delta(book["no"],  data.get("no",  []))

        # Update BotState from current book
        book = self._orderbooks.get(ticker, {})
        self._write_prices_to_state(ticker, book)

    @staticmethod
    def _apply_delta(side: list, deltas: list):
        """
        Apply delta updates to one side of the order book.

        Kalshi delta format: [[price, size], ...]
        A size of 0 means remove that price level.
        """
        price_map = {entry[0]: entry[1] for entry in side}
        for price, size in deltas:
            if size == 0:
                price_map.pop(price, None)
            else:
                price_map[price] = size

        # Rebuild sorted list: YES bids descending, NO asks descending by price
        side.clear()
        side.extend(sorted(price_map.items(), key=lambda x: -x[0]))

    def _write_prices_to_state(self, ticker: str, book: dict):
        """Compute best bid/ask/spread from local book and push to BotState."""
        bids = book.get("yes", [])
        asks = book.get("no",  [])

        if bids:
            yes_price = bids[0][0]    # best YES bid (cents)
        else:
            yes_price = (self.state.markets[ticker].yes_price
                         if ticker in self.state.markets else 50)

        if asks:
            no_price = asks[0][0]     # best NO bid (cents)
            ask_yes  = 100 - no_price
        else:
            no_price = 100 - yes_price
            ask_yes  = yes_price

        spread = max(ask_yes - yes_price, 0)

        self.state.update_market(
            ticker,
            yes_price  = yes_price,
            no_price   = no_price,
            last_price = yes_price,
            spread     = spread,
        )

    def _handle_trade(self, msg: dict):
        """Process a matched trade — update last traded price."""
        ticker = msg.get("market_ticker") or msg.get("ticker", "")
        data   = msg.get("msg", msg)
        price  = data.get("yes_price") or data.get("price")

        if ticker and price is not None:
            self.state.update_market(ticker, last_price=int(price))
            log.debug("Trade: %s @ %d¢", ticker, price)
