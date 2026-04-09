"""
risk.py — Position sizing and risk management for FOMC-focused bot.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    max_contracts:        int   = 10
    kelly_fraction:       float = 0.25
    max_market_exposure:  float = 0.05
    max_total_exposure:   float = 0.30
    daily_drawdown_limit: float = 0.10
    max_spread_cents:     int   = 10
    fee_cents:            int   = 7


class RiskManager:

    def __init__(self, config: RiskConfig):
        self.cfg             = config
        self._start_balance  = None
        self._halted         = False

    def set_balance(self, balance_cents: int) -> None:
        if self._start_balance is None:
            self._start_balance = balance_cents
            log.info("RiskManager: session balance set to $%.2f", balance_cents / 100)

        if self._start_balance > 0:
            drawdown = 1.0 - (balance_cents / self._start_balance)
            if drawdown >= self.cfg.daily_drawdown_limit:
                if not self._halted:
                    log.warning(
                        "DRAWDOWN LIMIT HIT (%.1f%%). Trading halted.",
                        drawdown * 100,
                    )
                self._halted = True
            else:
                self._halted = False

    def reset_day(self):
        self._start_balance = None
        self._halted = False

    def size(
        self,
        edge: float,
        market_price: float,
        balance_cents: int,
        confidence: float = 1.0,
    ) -> int:
        """
        Confidence-scaled Kelly sizing, net of fees.

        effective_kelly = kelly_fraction × confidence
        This means FedWatch+ZQ (conf≈0.90) sizes at 3x a single-source
        signal (conf≈0.70) for the same edge, automatically rewarding
        higher-quality information.
        """
        if balance_cents <= 0:
            log.warning("Sizing skipped: balance unknown (balance_cents=%d). "
                        "Is the balance REST call failing?", balance_cents)
            return 0

        net_edge = edge - (self.cfg.fee_cents / 100)
        if net_edge <= 0:
            log.debug("Net edge %.4f after %.0f¢ fee is non-positive — no trade.", 
                      edge, self.cfg.fee_cents)
            return 0

        effective_kelly = self.cfg.kelly_fraction * confidence
        kelly_f         = net_edge / max(1 - market_price, 0.01)
        bet_fraction    = kelly_f * effective_kelly

        price_cents  = max(int(market_price * 100), 1)
        max_by_kelly = int((balance_cents * bet_fraction) / price_cents)
        max_by_cap   = int((balance_cents * self.cfg.max_market_exposure) / price_cents)

        return min(max_by_kelly, max_by_cap, self.cfg.max_contracts)

    def approve(
        self,
        ticker: str,
        contracts: int,
        market_price: float,
        balance_cents: int,
        open_exposure_cents: int,
        spread_cents: int | None = None,
    ) -> bool:
        if self._halted:
            return False
        if contracts <= 0:
            return False
        if spread_cents is not None and spread_cents > self.cfg.max_spread_cents:
            log.info("Rejected %s — spread %d¢ > limit %d¢.",
                     ticker, spread_cents, self.cfg.max_spread_cents)
            return False

        order_cost = int(market_price * 100) * contracts
        if balance_cents > 0:
            if order_cost / balance_cents > self.cfg.max_market_exposure:
                log.info("Rejected %s — order exceeds market exposure limit.", ticker)
                return False
            if (open_exposure_cents + order_cost) / balance_cents > self.cfg.max_total_exposure:
                log.info("Rejected %s — would exceed total exposure limit.", ticker)
                return False
        return True
