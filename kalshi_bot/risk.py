"""
risk.py — Position sizing and risk management for FOMC-focused bot.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    max_contracts:        int   = 10
    kelly_fraction:       float = 0.25
    max_market_exposure:  float = 0.05
    max_total_exposure:   float = 0.80
    daily_drawdown_limit: float = 0.10
    max_spread_cents:     int   = 10
    fee_cents:            int   = 7


class RiskManager:

    def __init__(self, config: RiskConfig):
        self.cfg             = config
        self._start_balance  = None
        self._halted         = False
        self._risk_day       = int(time.time() // 86400)
        # Empirical Kelly cache — refreshed at most once per calendar day
        self._kelly_cached:    float              = config.kelly_fraction
        self._kelly_cache_day: Optional[datetime] = None

    def set_balance(self, balance_cents: int) -> None:
        # Reset at UTC midnight
        today = int(time.time() // 86400)
        if self._risk_day != today:
            self._start_balance = None
            if self._halted:
                log.warning(
                    "Drawdown halt reset at UTC midnight — daily reset in effect."
                )
            self._halted = False
            self._risk_day = today

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
                    log.error(
                        "DRAWDOWN HALT ACTIVATED — restart will clear this halt. "
                        "Manual intervention required."
                    )
                self._halted = True
            else:
                self._halted = False

    def reset_day(self):
        if self._halted:
            log.warning(
                "reset_day() called — drawdown halt was manually cleared. "
                "Ensure this is intentional before resuming trading."
            )
        self._start_balance = None
        self._halted = False

    async def calibrate_kelly(self, category: str = None) -> float:
        """
        Compute an empirical half-Kelly fraction from recent resolved trades.

        Uses the last 90 days of trade history from ep_resolution_db.  Falls
        back to the configured default (typically 0.25) when there is not yet
        enough data (< 10 completed trades or win_rate < 0.10).

        Result is cached per-instance for 24 hours so the I/O cost is
        negligible even when called every sizing cycle.

        Formula: Kelly = (p * odds - q) / odds  with odds=1.0 (binary markets),
        then half-Kelly capped at [0.05, 0.40].
        """
        # Refresh at most once per calendar day
        today = datetime.now(timezone.utc).date()
        if self._kelly_cache_day is not None and self._kelly_cache_day == today:
            return self._kelly_cached

        default = self.cfg.kelly_fraction
        try:
            # FIX 2: Kelly calibration uses only terminal-resolved trades.
            # Stop-loss / pre-expiry / trailing-stop exits have 3.4% win rate
            # and were poisoning the Kelly computation (full_kelly → -0.76,
            # capped at 5% floor).  Terminal exits (price ∈ {0, 100}) have
            # 71.8% win rate and are the true performance signal.
            from ep_resolution_db import _load_completed_trades
            from ep_config import cfg as _ep_cfg
            from datetime import timedelta
            from pathlib import Path

            try:
                import kalshi_bot.config as _kbc
                _min_kelly_trades: int = _kbc.MIN_KELLY_TRADES
            except Exception:
                _min_kelly_trades = 10

            _since = datetime.now(timezone.utc) - timedelta(days=90)
            _csv_path = Path(_ep_cfg.TRADES_CSV)
            _all_trades = _load_completed_trades(_csv_path, _since)

            # Terminal exits: contract resolved to YES=100¢ or YES=0¢.
            # The OR condition (checking model_source for stop keywords) was a bug —
            # model_source is "fedwatch+zq+wsj", never contains "stop_loss", so
            # it caused all 647 trades to pass, poisoning Kelly with stop-loss exits.
            _terminal = [
                t for t in _all_trades
                if t.get("exit_price_cents") in (0, 100)
            ]

            # Use terminal trades if we have enough; fall back to full population.
            if len(_terminal) >= _min_kelly_trades:
                _trades_for_kelly = _terminal
                _kelly_population = "terminal"
            else:
                _trades_for_kelly = _all_trades
                _kelly_population = "all"
                log.debug(
                    "calibrate_kelly: only %d terminal trades (need %d) — "
                    "falling back to full population (%d trades)",
                    len(_terminal), _min_kelly_trades, len(_all_trades),
                )

            total_trades = len(_trades_for_kelly)
            wins         = sum(1 for t in _trades_for_kelly if t.get("pnl_cents", 0) > 0)
            win_rate     = round(wins / total_trades, 4) if total_trades else None

            if win_rate is None or win_rate < 0.10 or total_trades < 10:
                log.debug(
                    "calibrate_kelly: insufficient data "
                    "(trades=%d win_rate=%s population=%s) — using default %.2f",
                    total_trades, win_rate, _kelly_population, default,
                )
                self._kelly_cached    = default
                self._kelly_cache_day = today
                return default

            p    = win_rate
            q    = 1.0 - p
            odds = 1.0
            kelly = (p * odds - q) / odds          # full Kelly
            result = max(0.05, min(kelly * 0.5, 0.40))   # half-Kelly, capped
            log.info(
                "calibrate_kelly: trades=%d win_rate=%.3f "
                "full_kelly=%.4f half_kelly=%.4f (capped=%.4f) "
                "population=%s (terminal=%d / all=%d)",
                total_trades, win_rate, kelly, kelly * 0.5, result,
                _kelly_population, len(_terminal), len(_all_trades),
            )
            self._kelly_cached    = result
            self._kelly_cache_day = today
            return result

        except Exception as exc:
            log.warning("calibrate_kelly failed (%s) — using default %.2f", exc, default)
            return default

    def size(
        self,
        edge: float,
        market_price: float,
        balance_cents: int,
        confidence: float = 1.0,
        side: str = "yes",
    ) -> int:
        """
        Confidence-scaled Kelly sizing, net of fees.

        effective_kelly = kelly_fraction × confidence
        This means FedWatch+ZQ (conf≈0.90) sizes at 3x a single-source
        signal (conf≈0.70) for the same edge, automatically rewarding
        higher-quality information.

        Kelly denominator depends on side:
          YES: win amount = 1 - market_price  →  kelly_f = edge / (1 - market_price)
          NO:  win amount = market_price       →  kelly_f = edge / market_price
        """
        if balance_cents <= 0:
            log.warning("Sizing skipped: balance unknown (balance_cents=%d). "
                        "Is the balance REST call failing?", balance_cents)
            return 0

        # Refresh empirical Kelly once per day in the background (non-blocking).
        # _kelly_cached is seeded to cfg.kelly_fraction at construction; the
        # first successful async refresh upgrades it to the data-driven value.
        today = datetime.now(timezone.utc).date()
        if self._kelly_cache_day != today:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.calibrate_kelly())
            except RuntimeError:
                pass   # no event loop — caller must await calibrate_kelly() manually

        net_edge = edge - (self.cfg.fee_cents / 100)
        if net_edge <= 0:
            log.debug("Net edge %.4f after %.0f¢ fee is non-positive — no trade.",
                      edge, self.cfg.fee_cents)
            return 0

        effective_kelly = self._kelly_cached * confidence
        if side == "yes":
            kelly_f = net_edge / max(1 - market_price, 0.01)
        else:
            kelly_f = net_edge / max(market_price, 0.01)
        bet_fraction = kelly_f * effective_kelly

        # For NO contracts the actual outlay per contract is (1 - market_price),
        # not market_price. Using the YES price here would over-size NO positions.
        if side == "no":
            price_cents = max(100 - int(market_price * 100), 1)
        else:
            price_cents = max(int(market_price * 100), 1)
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
        side: str = "yes",
    ) -> bool:
        if self._halted:
            return False
        if contracts <= 0:
            return False
        if spread_cents is not None and spread_cents > self.cfg.max_spread_cents:
            log.info("Rejected %s — spread %d¢ > limit %d¢.",
                     ticker, spread_cents, self.cfg.max_spread_cents)
            return False

        if side == "no":
            order_cost = (100 - int(market_price * 100)) * contracts
        else:
            order_cost = int(market_price * 100) * contracts
        if balance_cents > 0:
            if order_cost / balance_cents > self.cfg.max_market_exposure:
                log.info("Rejected %s — order exceeds market exposure limit.", ticker)
                return False
            if (open_exposure_cents + order_cost) / balance_cents > self.cfg.max_total_exposure:
                log.info("Rejected %s — would exceed total exposure limit.", ticker)
                return False
        return True
