"""
ep_risk.py — UnifiedRiskEngine: single approval gate for all asset classes.

Kalshi logic delegates to kalshi_bot.risk.RiskManager (unchanged).
BTC sizing uses 2% risk-per-trade; daily loss cap is 5% of balance; total
BTC exposure is capped at 30% of balance.  CME basis is future-ready stub.
"""

import time
from typing import Optional, Tuple

from ep_config import log
from kalshi_bot.risk import RiskManager
from ep_schema import SignalMessage

_BTC_DAILY_LOSS_CAP  = 0.05   # halt BTC entries if session BTC loss > 5% of balance
_BTC_EXPOSURE_CAP    = 0.30   # max BTC exposure as fraction of balance
_BTC_RISK_PER_TRADE  = 0.02   # Kelly-substitute: risk 2% of balance per BTC trade


class UnifiedRiskEngine:

    def __init__(self, kalshi_risk: RiskManager):
        self._kalshi = kalshi_risk

        # BTC daily loss tracking (in-memory; resets at UTC midnight)
        self._btc_daily_loss_cents: int = 0
        self._btc_day: int = int(time.time() // 86400)

    # ── Public API ────────────────────────────────────────────────────────────

    def size(self, sig: SignalMessage, balance_cents: int) -> int:
        """Return number of contracts / units to trade. 0 = skip."""
        if sig.asset_class == "kalshi":
            return self._kalshi.size(
                edge          = sig.edge,
                market_price  = sig.market_price,
                balance_cents = balance_cents,
                confidence    = sig.confidence,
                side          = sig.side,
            )
        if sig.asset_class == "btc_spot":
            return self._size_btc(sig, balance_cents)
        if sig.asset_class == "cme_btc_basis":
            return self._size_basis(sig, balance_cents)
        log.warning("UnifiedRiskEngine.size: unknown asset_class=%r", sig.asset_class)
        return 0

    def approve(
        self,
        sig:           SignalMessage,
        contracts:     int,
        balance_cents: int,
        open_exposure: int,
    ) -> Tuple[bool, Optional[str]]:
        """Returns (approved, reject_reason_or_None)."""
        if sig.asset_class == "kalshi":
            ok = self._kalshi.approve(
                ticker              = sig.ticker,
                contracts           = contracts,
                market_price        = sig.market_price,
                balance_cents       = balance_cents,
                open_exposure_cents = open_exposure,
                spread_cents        = sig.spread_cents,
            )
            return ok, (None if ok else "RISK_GATE_KALSHI")
        if sig.asset_class == "btc_spot":
            return self._approve_btc(sig, contracts, balance_cents, open_exposure)
        return False, "UNKNOWN_ASSET_CLASS"

    def record_btc_pnl(self, pnl_cents: int) -> None:
        """
        Call after each BTC position closes.  Negative pnl_cents accumulates
        toward the daily loss cap; positive pnl_cents is not credited back
        (conservative: once losses hit the cap, no new BTC entries that day).
        """
        self._reset_daily_if_needed()
        if pnl_cents < 0:
            self._btc_daily_loss_cents += abs(pnl_cents)
            log.info(
                "BTC daily loss updated: $%.2f / cap $%.2f",
                self._btc_daily_loss_cents / 100,
                self._btc_daily_loss_cents / 100,   # cap is computed at approve time
            )

    # ── BTC ───────────────────────────────────────────────────────────────────

    def _reset_daily_if_needed(self) -> None:
        today = int(time.time() // 86400)
        if today != self._btc_day:
            self._btc_daily_loss_cents = 0
            self._btc_day = today

    def _size_btc(self, sig: SignalMessage, balance_cents: int) -> int:
        """
        Size a BTC trade using a fixed 2% risk-per-trade fraction.

        Units = floor(risk_budget / price_per_unit)
        For spot BTC: one "unit" is worth sig.market_price USD → * 100 cents.
        """
        if not sig.btc_price or balance_cents <= 0:
            return 0
        risk_cents = int(balance_cents * _BTC_RISK_PER_TRADE)
        price_per_unit_cents = int(sig.btc_price * 100)
        if price_per_unit_cents <= 0:
            return 0
        return max(0, risk_cents // price_per_unit_cents)

    def _approve_btc(
        self, sig: SignalMessage, units: int,
        balance_cents: int, open_exposure: int,
    ) -> Tuple[bool, Optional[str]]:
        """
        Three gates for BTC entries:
          1. units > 0
          2. Session BTC loss < 5% of balance (daily loss cap)
          3. (open_exposure + new order cost) < 30% of balance
        """
        self._reset_daily_if_needed()

        if units <= 0:
            return False, "RISK_GATE_SIZE"

        # 5% daily BTC loss cap
        if balance_cents > 0:
            loss_fraction = self._btc_daily_loss_cents / balance_cents
            if loss_fraction >= _BTC_DAILY_LOSS_CAP:
                log.warning(
                    "BTC daily loss cap hit: $%.2f lost (%.1f%% of $%.2f balance) — "
                    "no new BTC entries today.",
                    self._btc_daily_loss_cents / 100,
                    loss_fraction * 100,
                    balance_cents / 100,
                )
                return False, "RISK_GATE_DRAWDOWN"

        # 30% total BTC exposure cap
        order_cost = int(sig.market_price * units * 100)
        if balance_cents > 0 and (open_exposure + order_cost) / balance_cents > _BTC_EXPOSURE_CAP:
            return False, "RISK_GATE_EXPOSURE"

        return True, None

    # ── CME basis stub ────────────────────────────────────────────────────────

    def _size_basis(self, sig: SignalMessage, balance_cents: int) -> int:
        # Implement when CME basis leg goes live
        return 0
