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
        self._kelly_cached:      float              = config.kelly_fraction
        self._kelly_by_category: dict               = {}   # {bucket: fraction}
        self._kelly_cache_day:   Optional[datetime] = None

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

    @staticmethod
    def _kelly_bucket(model_source: str) -> str:
        """Map a model_source string to one of three Kelly buckets."""
        ms = (model_source or "").lower()
        if ms.endswith("_arb"):
            return "arb"
        if "coherence" in ms:
            return "coherence"
        if "fred_" in ms or "gdp" in ms:
            return "economic"
        return "directional"

    async def calibrate_kelly(self) -> float:
        """
        Compute empirical half-Kelly fractions from recent resolved trades.

        Three changes vs the original:
          1. Fee-adjusted P&L: subtract 2 × fee_cents (14¢ round-trip) per contract
             before deciding win/loss — removes the optimistic bias from ignoring fees.
          2. Per-category Kelly: separate fractions for arb, coherence, economic,
             and directional buckets.  Falls back to global when a bucket has < 10
             fee-adjusted terminal trades.
          3. Still uses only terminal exits (price ∈ {0, 100}) for the same reason
             as before — stop-loss exits have ~3% win rate and pollute the estimate.

        Results are cached per-instance for one calendar day.
        """
        today = datetime.now(timezone.utc).date()
        if self._kelly_cache_day is not None and self._kelly_cache_day == today:
            return self._kelly_cached

        default = self.cfg.kelly_fraction
        fee_rt  = 2 * self.cfg.fee_cents   # round-trip fee per contract (14¢)

        def _half_kelly(trades: list) -> Optional[float]:
            """Compute half-Kelly from a list of trade dicts (fee-adjusted P&L)."""
            n = len(trades)
            if n < 10:
                return None
            wins = sum(
                1 for t in trades
                if t.get("pnl_cents", 0) - fee_rt * t.get("contracts", 1) > 0
            )
            wr = wins / n
            if wr < 0.10:
                return None
            full_k = (wr - (1.0 - wr))       # Kelly = p - q  (odds=1 binary)
            return max(0.05, min(full_k * 0.5, 0.40))

        try:
            from ep_resolution_db import _load_completed_trades
            from ep_config import cfg as _ep_cfg
            from datetime import timedelta
            from pathlib import Path

            try:
                import kalshi_bot.config as _kbc
                _min_kelly_trades: int = _kbc.MIN_KELLY_TRADES
            except Exception:
                _min_kelly_trades = 10

            _since     = datetime.now(timezone.utc) - timedelta(days=90)
            _csv_path  = Path(_ep_cfg.TRADES_CSV)
            _all_trades = _load_completed_trades(_csv_path, _since)

            # Terminal exits only (price resolved to 0 or 100¢)
            _terminal = [
                t for t in _all_trades
                if t.get("exit_price_cents") in (0, 100)
            ]

            if len(_terminal) >= _min_kelly_trades:
                _pool           = _terminal
                _kelly_population = "terminal"
            else:
                _pool           = _all_trades
                _kelly_population = "all"
                log.debug(
                    "calibrate_kelly: only %d terminal trades (need %d) — "
                    "falling back to full population (%d trades)",
                    len(_terminal), _min_kelly_trades, len(_all_trades),
                )

            # ── Global Kelly ─────────────────────────────────────────────────
            global_kelly = _half_kelly(_pool)
            if global_kelly is None:
                log.debug(
                    "calibrate_kelly: insufficient data "
                    "(trades=%d population=%s) — using default %.2f",
                    len(_pool), _kelly_population, default,
                )
                self._kelly_cached    = default
                self._kelly_cache_day = today
                return default

            # ── Per-category Kelly ────────────────────────────────────────────
            from collections import defaultdict
            by_bucket: dict = defaultdict(list)
            for t in _pool:
                by_bucket[self._kelly_bucket(t.get("strategy", ""))].append(t)

            cat_kelly: dict = {}
            for bucket, bucket_trades in by_bucket.items():
                bk = _half_kelly(bucket_trades)
                if bk is not None:
                    cat_kelly[bucket] = bk
                else:
                    cat_kelly[bucket] = global_kelly   # fall back to global
                log.debug(
                    "calibrate_kelly[%s]: trades=%d kelly=%.4f",
                    bucket, len(bucket_trades), cat_kelly[bucket],
                )

            self._kelly_cached      = global_kelly
            self._kelly_by_category = cat_kelly
            self._kelly_cache_day   = today

            _total = len(_pool)
            _wins  = sum(
                1 for t in _pool
                if t.get("pnl_cents", 0) - fee_rt * t.get("contracts", 1) > 0
            )
            log.info(
                "calibrate_kelly: trades=%d fee_adj_win_rate=%.3f "
                "global_half_kelly=%.4f  by_category=%s  population=%s",
                _total, _wins / _total if _total else 0, global_kelly,
                {k: f"{v:.3f}" for k, v in cat_kelly.items()},
                _kelly_population,
            )
            return global_kelly

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
        model_source: str = "",
        vol_multiplier: float = 1.0,
    ) -> int:
        """
        Confidence-scaled, per-category Kelly sizing, net of fees.

        effective_kelly = kelly_fraction(category) × confidence × vol_multiplier

        kelly_fraction is looked up per model_source bucket (arb / coherence /
        economic / directional) so each strategy is sized by its own empirical
        win rate rather than a pooled global.  Falls back to global when a bucket
        has insufficient history.

        vol_multiplier adjusts for release-proximity volatility:
          > 1.0 — post-release window (uncertainty resolved, edge confirmed)
          < 1.0 — pre-release window (high uncertainty, size down)
          1.0   — default / no adjustment

        Kelly denominator depends on side:
          YES: win amount = 1 - market_price  →  kelly_f = edge / (1 - market_price)
          NO:  win amount = market_price       →  kelly_f = edge / market_price
        """
        if balance_cents <= 0:
            log.warning("Sizing skipped: balance unknown (balance_cents=%d). "
                        "Is the balance REST call failing?", balance_cents)
            return 0

        # Refresh empirical Kelly once per day in the background (non-blocking).
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

        # Per-category Kelly: use bucket-specific fraction when available
        bucket        = self._kelly_bucket(model_source)
        base_kelly    = self._kelly_by_category.get(bucket, self._kelly_cached)
        effective_kelly = base_kelly * confidence * max(0.1, vol_multiplier)

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
