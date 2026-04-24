"""
state.py — Shared in-memory state for the entire bot.

A single BotState instance is created at startup and passed to every
component. This replaces the pattern of each module fetching its own
data independently and means:

  - The WebSocket writes prices here as they arrive
  - The strategy reads from here instead of polling REST
  - The dashboard reads from here to render the UI
  - Alerts subscribe to events emitted from here

Thread safety: all mutations go through a threading.Lock.
Reads are lock-free for performance (Python GIL protects simple assignments).
"""

import threading
import datetime
from datetime import timezone
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MarketState:
    """Live state for a single FOMC market."""
    ticker:        str
    title:         str
    yes_price:     int    = 50    # cents
    no_price:      int    = 50    # cents
    last_price:    int    = 0     # cents (last matched trade; 0 = no trades yet)
    spread:        int    = 0     # cents
    volume:        int    = 0
    fair_value:    float  = 0.0   # FedWatch-derived
    edge:          float  = 0.0   # fair_value - market_price
    confidence:    float  = 0.0
    updated_at:    datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(timezone.utc)
    )


@dataclass
class PositionState:
    """A currently open position."""
    ticker:       str
    side:         str
    contracts:    int
    entry_cents:  int
    entry_time:   datetime.datetime
    fair_value:   float
    unrealized_pnl_cents: int = 0


@dataclass
class TradeEvent:
    """A completed trade (entry or exit) for the activity feed."""
    timestamp:  datetime.datetime
    ticker:     str
    action:     str    # "entry" or "exit"
    side:       str
    contracts:  int
    price:      int    # cents
    edge:       float
    mode:       str    # "paper" or "live"


class BotState:
    """
    Single shared state object. Thread-safe writes, fast reads.

    Components interact with state like:
        state.update_market(ticker, yes_price=65, no_price=35)
        state.add_trade(trade_event)
        state.subscribe(callback)   # for alerts / dashboard SSE
    """

    def __init__(self):
        self._lock          = threading.Lock()
        self.markets:       dict[str, MarketState]  = {}
        self.positions:     dict[str, PositionState] = {}
        self.recent_trades: list[TradeEvent]        = []   # last 50
        self.signals:       list[dict]              = []   # current cycle signals
        self.balance_cents: int                     = 0
        self.session_pnl_cents: int                 = 0
        self.start_balance_cents: int               = 0
        self.cycle_count:   int                     = 0
        self.last_cycle_at: datetime.datetime | None = None
        self.ws_connected:  bool                    = False
        self.mode:          str                     = "paper"
        self._subscribers:  list[Callable]          = []

    # ── Market updates ────────────────────────────────────────────────────────

    def update_market(self, ticker: str, **kwargs):
        with self._lock:
            if ticker not in self.markets:
                self.markets[ticker] = MarketState(
                    ticker=ticker, title=kwargs.pop("title", ticker)
                )
            m = self.markets[ticker]
            for k, v in kwargs.items():
                if hasattr(m, k):
                    setattr(m, k, v)
            m.updated_at = datetime.datetime.now(timezone.utc)
        self._emit("market_update", ticker)

    def update_fair_value(self, ticker: str, fair_value: float,
                          edge: float, confidence: float):
        with self._lock:
            if ticker in self.markets:
                self.markets[ticker].fair_value  = fair_value
                self.markets[ticker].edge        = edge
                self.markets[ticker].confidence  = confidence

    # ── Position tracking ─────────────────────────────────────────────────────

    def open_position(self, pos: PositionState):
        with self._lock:
            self.positions[pos.ticker] = pos
        self._emit("position_opened", pos.ticker)

    def close_position(self, ticker: str, exit_cents: int):
        with self._lock:
            pos = self.positions.pop(ticker, None)
            if pos:
                pnl = self._calc_pnl(pos, exit_cents)
                self.session_pnl_cents += pnl
        self._emit("position_closed", ticker)

    def update_unrealized_pnl(self):
        """Recalculate unrealized P&L for all open positions from current prices."""
        with self._lock:
            for ticker, pos in self.positions.items():
                mkt = self.markets.get(ticker)
                if mkt:
                    self.positions[ticker].unrealized_pnl_cents = \
                        self._calc_pnl(pos, mkt.last_price)

    @staticmethod
    def _calc_pnl(pos: PositionState, current_cents: int) -> int:
        if pos.side == "yes":
            return (current_cents - pos.entry_cents) * pos.contracts
        else:
            return (pos.entry_cents - current_cents) * pos.contracts

    # ── Trade log ─────────────────────────────────────────────────────────────

    def add_trade(self, trade: TradeEvent):
        with self._lock:
            self.recent_trades.append(trade)
            self.recent_trades = self.recent_trades[-50:]   # keep last 50
        self._emit("trade", trade)

    # ── Session state ─────────────────────────────────────────────────────────

    def set_balance(self, balance_cents: int):
        with self._lock:
            if self.start_balance_cents == 0:
                self.start_balance_cents = balance_cents
            self.balance_cents = balance_cents
        self._emit("balance", balance_cents)

    def set_signals(self, signals: list):
        with self._lock:
            self.signals = signals
        self._emit("signals", signals)

    def record_cycle(self):
        with self._lock:
            self.cycle_count  += 1
            self.last_cycle_at = datetime.datetime.now(timezone.utc)

    def set_ws_connected(self, connected: bool):
        with self._lock:
            self.ws_connected = connected
        self._emit("ws_status", connected)

    # ── Pub/sub for alerts and dashboard ──────────────────────────────────────

    def subscribe(self, callback: Callable) -> Callable:
        """
        Register a callback(event_type, data) for state change events.

        Returns the callback itself as a token for unsubscription.
        Usage:
            token = state.subscribe(my_handler)
            # later:
            state.unsubscribe(token)
        """
        if callback not in self._subscribers:
            self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: Callable) -> None:
        """Remove a previously registered callback. Safe to call if not subscribed."""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    def _emit(self, event_type: str, data):
        for cb in self._subscribers:
            try:
                cb(event_type, data)
            except Exception:
                pass   # never let a bad subscriber crash the bot

    # ── Snapshot for dashboard ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of the full bot state."""
        with self._lock:
            markets   = {t: self._market_dict(m) for t, m in self.markets.items()}
            positions = {t: self._pos_dict(p) for t, p in self.positions.items()}
            trades    = [self._trade_dict(t) for t in self.recent_trades[-20:]]
            signals   = list(self.signals)
            # Compute inside the lock — position dicts must not be mutated
            # by another thread between capture and aggregation
            total_unrealized = sum(
                p.get("unrealized_pnl_cents", 0) for p in positions.values()
            )

        return {
            "mode":              self.mode,
            "balance_cents":     self.balance_cents,
            "start_balance":     self.start_balance_cents,
            "session_pnl":       self.session_pnl_cents,
            "unrealized_pnl":    total_unrealized,
            "cycle_count":       self.cycle_count,
            "last_cycle_at":     self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "ws_connected":      self.ws_connected,
            "markets":           markets,
            "positions":         positions,
            "recent_trades":     trades,
            "signals":           signals,
            "open_position_count": len(positions),
        }

    @staticmethod
    def _market_dict(m: MarketState) -> dict:
        return {
            "ticker":     m.ticker,
            "title":      m.title,
            "yes_price":  m.yes_price,
            "no_price":   m.no_price,
            "last_price": m.last_price,
            "spread":     m.spread,
            "fair_value": round(m.fair_value * 100, 1),   # as cents for display
            "edge":       round(m.edge * 100, 1),
            "confidence": round(m.confidence, 2),
            "updated_at": m.updated_at.isoformat(),
        }

    @staticmethod
    def _pos_dict(p: PositionState) -> dict:
        return {
            "ticker":       p.ticker,
            "side":         p.side,
            "contracts":    p.contracts,
            "entry_cents":  p.entry_cents,
            "entry_time":   p.entry_time.isoformat(),
            "unrealized_pnl": p.unrealized_pnl_cents,
        }

    @staticmethod
    def _trade_dict(t: TradeEvent) -> dict:
        return {
            "timestamp":  t.timestamp.isoformat(),
            "ticker":     t.ticker,
            "action":     t.action,
            "side":       t.side,
            "contracts":  t.contracts,
            "price":      t.price,
            "edge":       round(t.edge, 3),
            "mode":       t.mode,
        }
