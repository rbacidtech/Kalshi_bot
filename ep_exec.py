"""
ep_exec.py — Exec helpers and main loop (runs on QuantVPS Chicago).

Two concurrent async tasks (no threads needed):
  _signal_consumer — drains ep:signals stream, executes orders
  _exit_checker    — periodic exit checks using Redis price state from Intel

Both run under asyncio.gather() — neither blocks the other.
"""

import asyncio
import json
import os
import re as _re
import time
from datetime import datetime, timezone
from typing import Optional

from ep_config import cfg, NODE_ID, REDIS_URL, EXIT_INTERVAL, EP_PRICES, log, sd_notify
from kalshi_bot.auth     import KalshiAuth, NoAuth
from kalshi_bot.client   import KalshiClient
from kalshi_bot.executor import Executor
from kalshi_bot.risk     import RiskManager, RiskConfig
from kalshi_bot.logger   import setup_logging
from ep_schema import ExecutionReport, SignalMessage
from ep_bus import RedisBus
from ep_positions import PositionStore
from ep_risk import UnifiedRiskEngine
from ep_adapters import message_to_kalshi_signal
from ep_coinbase import CoinbaseTradeClient, fetch_btc_spot_usd
from ep_risk import BTC_UNIT
from ep_metrics import metrics
from ep_telegram import telegram
from ep_resolution_db import ResolutionDB, poll_resolutions_loop
from kalshi_bot.executor import ArbRollbackFailed
from ep_pg_audit import init_audit_writer, stop_audit_writer, audit as _audit_writer
try:
    from ep_kelly_calib import get_calibrated_kelly, kelly_calib_loop
    _KELLY_CALIB_AVAILABLE = True
except ImportError:
    _KELLY_CALIB_AVAILABLE = False
    def get_calibrated_kelly(_edge): return None
    async def kelly_calib_loop(_bus, **_kw): pass


def _safe_key(ticker: str) -> str:
    """Sanitize a ticker string so it is safe to embed in a Redis key."""
    return _re.sub(r'[^A-Za-z0-9\-_]', '', ticker)


# ── Stop-loss re-entry cooldown ───────────────────────────────────────────────
# After a stop-loss or trailing-stop exit, the same market is suppressed for
# a cooldown period.  Cooldown escalates with repeated stops on the same ticker:
#   1st stop: 30 min   (COOLDOWN_TIER_1)
#   2nd+ stop: 24 h    (COOLDOWN_TIER_3) — skip tier 2 to stop cycling losses
# Persisted to Redis (ep:cooldown:{ticker}) so restarts don't reset it.
_exit_cooldown: dict = {}    # ticker → float timestamp (in-memory fast path)
_COOLDOWN_SECONDS = 1800     # default / tier-1 (30 min)
_COOLDOWN_TIER_1  = 1800     # 1st stop loss
_COOLDOWN_TIER_2  = 7200     # 2nd stop loss (2 h)
_COOLDOWN_TIER_3  = 86400    # 3rd+ stop loss (24 h)
_STOP_COUNT_TTL   = 86400 * 7  # rolling 7-day window for stop-loss counts

# ── Entry-failure cooldown ─────────────────────────────────────────────────────
# After executor.execute() fails (e.g. Kalshi 409 Conflict), suppress the same
# ticker for _ENTRY_FAILED_COOLDOWN_S to break the signal → 409 → clean-up →
# signal loop that otherwise retries every 120 s indefinitely.
_entry_failed_cooldown: dict = {}   # ticker → float timestamp of failure
_ENTRY_FAILED_COOLDOWN_S      = 1800  # 30 minutes (executor failures, bad strikes)
_ENTRY_FAILED_COOLDOWN_SHORT  = 600   # 10 minutes (Kelly=0: price may move soon)

# ── Exit-API failure backoff ───────────────────────────────────────────────────
# When Kalshi rejects an exit order (e.g. HTTP 400 due to illiquid market),
# back off before retrying so we don't hammer the API every 60 s.
_exit_api_failed: dict = {}          # ticker → float timestamp of last failure
_EXIT_API_BACKOFF_S = 600            # 10 minutes between exit retries

# ── Kalshi API circuit breaker ─────────────────────────────────────────────────
# Counts consecutive executor failures (excluding 409 Conflict).  When the
# threshold is reached, new signals are gate-rejected until the counter resets.
# Auto-resets after _KALSHI_API_FAILURE_RESET_S seconds so the circuit doesn't
# stay open forever if the API recovers — without a reset, 5 failures locks out
# all future signals until a successful order, which is impossible if the circuit
# is blocking the very orders that would reset it (deadlock).
_kalshi_api_failures: int = 0
_KALSHI_API_FAILURE_THRESHOLD: int = 5
_KALSHI_API_FAILURE_RESET_S: int = 300   # 5 min auto-reset
_kalshi_api_failure_ts: float = 0.0      # time of first failure in current run

# ── Resting order TTL ─────────────────────────────────────────────────────────
_RESTING_ORDER_MAX_HOURS = 4

# ── Exit TIF escalation ────────────────────────────────────────────────────────
# If a live exit limit order hasn't filled in _EXIT_TIF_STEP_MINUTES, replace
# it with a new limit _EXIT_TIF_WIDEN_CENTS lower (more aggressive sell).
# Repeat up to _EXIT_TIF_MAX_STEPS times, then hold at the final price.
_EXIT_TIF_STEP_MINUTES = int(os.getenv("EXIT_TIF_STEP_MINUTES", "30"))
_EXIT_TIF_WIDEN_CENTS  = int(os.getenv("EXIT_TIF_WIDEN_CENTS",  "2"))
_EXIT_TIF_MAX_STEPS    = int(os.getenv("EXIT_TIF_MAX_STEPS",    "3"))

# ── Directional exposure limits ───────────────────────────────────────────────
# Cap total YES exposure and total NO exposure independently.
# Balance itself constrains total deployment; no cross-market category limits.
_MAX_LONG_PCT   = float(os.getenv("MAX_LONG_PCT",  "0.70"))  # max % of balance in YES positions
_MAX_SHORT_PCT  = float(os.getenv("MAX_SHORT_PCT", "0.70"))  # max % of balance in NO positions
_MAX_MARKET_PCT = float(os.getenv("MAX_MARKET_PCT","0.15"))  # max % of balance in any single market

# ── Long-game capital cap ─────────────────────────────────────────────────────
# Positions settling > LONG_GAME_HORIZON_DAYS out are "long-game" bets.
# Cap them at 30% of balance so 70% stays available for active/daily trading.
_LONG_GAME_HORIZON_DAYS = int(os.getenv("LONG_GAME_HORIZON_DAYS", "90"))
_LONG_GAME_MAX_PCT      = float(os.getenv("LONG_GAME_MAX_PCT", "0.30"))

# ── Book-depth gate ───────────────────────────────────────────────────────────
# Minimum combined top-5 order-book depth (contracts) required to enter.
# Far-dated markets have thin books by nature; this gate stops the bot from
# accumulating illiquid positions that are hard to exit.
# Long-game (>90d) threshold is higher because those markets are hardest to exit.
_MIN_BOOK_DEPTH          = int(os.getenv("MIN_BOOK_DEPTH", "50"))
_MIN_BOOK_DEPTH_LONG     = int(os.getenv("MIN_BOOK_DEPTH_LONG", "200"))


def _tiered_take_profit(base_tp: int, hours_remaining: float) -> int:
    """
    Scale take-profit threshold down for shorter-dated positions so they
    behave like active trades rather than hold-to-resolution bets.

    Days to expiry → effective TP:
      ≤  3 days  (weather/near-expiry) : base ÷ 5  (min 5¢)
      ≤ 30 days  (next FOMC)           : base ÷ 3  (min 8¢)
      ≤ 90 days  (Jun–Sep FOMC)        : base ÷ 2  (min 12¢)
      > 90 days  (long-game)           : full base (hold for larger moves)
    """
    days = hours_remaining / 24
    if days <= 3:   return max(5,  base_tp // 5)
    if days <= 30:  return max(8,  base_tp // 3)
    if days <= 90:  return max(12, base_tp // 2)
    return base_tp


def _tiered_trailing_stop(base_ts: int, hours_remaining: float) -> int:
    """
    Tighter trailing stops for short-dated positions — protect gains faster
    when time value is low.

      ≤  3 days : base ÷ 3  (min 3¢)
      ≤ 30 days : base ÷ 2  (min 5¢)
      > 30 days : full base
    """
    days = hours_remaining / 24
    if days <= 3:  return max(3, base_ts // 3)
    if days <= 30: return max(5, base_ts // 2)
    return base_ts

# ── Near-certain resolution threshold ────────────────────────────────────────
# When a Kalshi YES price is at or below this threshold (e.g. 8¢), the contract
# is near-certain to resolve NO; when at or above (100 - threshold), near-certain
# YES.  In either case we hold to auto-resolution rather than exiting early at a
# loss.  Overridable via env var.
KALSHI_NEAR_CERTAIN_THRESHOLD_CENTS = int(os.getenv("KALSHI_NEAR_CERTAIN_THRESHOLD_CENTS", "8"))


async def _execute_btc(
    sig:      SignalMessage,
    size_str: str,
    client:   CoinbaseTradeClient,
) -> bool:
    """
    Place a BTC market order via Coinbase Advanced Trade.
    Paper mode is handled transparently inside CoinbaseTradeClient.
    """
    cb_side  = "BUY" if sig.side.lower() in ("buy", "yes") else "SELL"
    result   = await client.create_market_order(sig.ticker, cb_side, size_str)
    if not result.get("success"):
        err    = result.get("error", "")
        detail = result.get("detail", {})
        if "403" in err:
            log.error(
                "BTC order REJECTED (403 Forbidden) — Coinbase API key is missing 'Trade' scope. "
                "Fix: portal.cdp.coinbase.com → API Keys → enable Trade scope."
            )
        elif "401" in err:
            log.error("BTC order REJECTED (401 Unauthorized) — JWT signing failed or key revoked.")
        else:
            log.error("BTC order failed: %s  detail=%s", err, detail)
        return False
    log.info(
        "BTC %s %s  size=%s BTC @ $%.2f  z=%.2f  order_id=%s",
        cb_side, sig.ticker, size_str, sig.market_price,
        sig.btc_z_score or 0.0,
        result.get("order_id", "?"),
    )
    return True


async def _process_signal(
    sig:         SignalMessage,
    bus:         RedisBus,
    positions:   PositionStore,
    risk_engine: UnifiedRiskEngine,
    executor:    Executor,
    coinbase:    CoinbaseTradeClient,
) -> ExecutionReport:
    """
    Run one SignalMessage through:  TTL check → dedup → risk gate → execute.

    Always returns an ExecutionReport — even on rejection — so Intel can
    audit every signal's fate via the ep:executions stream.
    """
    global _kalshi_api_failures, _kalshi_api_failure_ts

    def _rejected(reason: str) -> ExecutionReport:
        metrics.record_risk_gate(reason, "reject")
        return ExecutionReport(
            signal_id     = sig.signal_id,
            ticker        = sig.ticker,
            asset_class   = sig.asset_class,
            side          = sig.side,
            mode          = "paper" if cfg.PAPER_TRADE else "live",
            status        = "rejected",
            reject_reason = reason,
        )

    # ── TTL ───────────────────────────────────────────────────────────────────
    if sig.is_expired():
        age_ms = (int(time.time() * 1_000_000) - sig.ts_us) // 1_000
        log.info("EXPIRED signal %s  age=%dms  ttl=%dms  (bump EP_SIGNAL_TTL_MS if frequent)",
                 sig.ticker, age_ms, sig.ttl_ms)
        return ExecutionReport(
            signal_id     = sig.signal_id,
            ticker        = sig.ticker,
            asset_class   = sig.asset_class,
            side          = sig.side,
            mode          = "paper" if cfg.PAPER_TRADE else "live",
            status        = "expired",
            reject_reason = "EXPIRED",
        )

    # ── Dedup on Redis positions ──────────────────────────────────────────────
    if await positions.exists(sig.ticker):
        log.debug("Skipping %s — already in Redis positions.", sig.ticker)
        return ExecutionReport(
            signal_id     = sig.signal_id,
            ticker        = sig.ticker,
            asset_class   = sig.asset_class,
            side          = sig.side,
            mode          = "paper" if cfg.PAPER_TRADE else "live",
            status        = "duplicate",
            reject_reason = "DUPLICATE",
        )

    # ── Entry-failure cooldown ────────────────────────────────────────────────
    # Suppress tickers that recently failed at the Kalshi order-placement level
    # (e.g. HTTP 409 Conflict).  Prevents the signal → 409 → cleanup → signal
    # loop from hammering the same market every 120 s.
    _eft = _entry_failed_cooldown.get(sig.ticker)
    if _eft is not None:
        _elapsed_eft = time.time() - _eft
        if _elapsed_eft < _ENTRY_FAILED_COOLDOWN_S:
            log.debug(
                "Entry-failure cooldown: %s  (%.0fs / %.0fs elapsed)  signal_id=%.8s",
                sig.ticker, _elapsed_eft, _ENTRY_FAILED_COOLDOWN_S, sig.signal_id,
            )
            return _rejected("ENTRY_FAILED_COOLDOWN")
        else:
            del _entry_failed_cooldown[sig.ticker]   # expired — allow retry

    # ── Kalshi API circuit breaker ────────────────────────────────────────────
    if _kalshi_api_failures >= _KALSHI_API_FAILURE_THRESHOLD:
        if (time.time() - _kalshi_api_failure_ts) >= _KALSHI_API_FAILURE_RESET_S:
            _kalshi_api_failures = 0
            log.info("Kalshi API circuit breaker auto-reset after %ds", _KALSHI_API_FAILURE_RESET_S)
        else:
            return _rejected("KALSHI_API_CIRCUIT")

    # ── Stop-loss cooldown ────────────────────────────────────────────────────
    # Prevent re-entering a market we just stopped out of.
    # In-memory fast path first; Redis check survives restarts.
    _ts = _exit_cooldown.get(sig.ticker)
    if _ts is not None:
        _elapsed = time.time() - _ts
        if _elapsed < _COOLDOWN_SECONDS:
            log.debug(
                "Stop-loss cooldown: %s  (%.0fs / %.0fs elapsed)",
                sig.ticker, _elapsed, _COOLDOWN_SECONDS,
            )
            return _rejected("STOP_COOLDOWN")
        else:
            del _exit_cooldown[sig.ticker]   # expired — allow re-entry
    # Redis cooldown persists across restarts
    try:
        _redis_ttl = await bus._r.ttl(f"ep:cooldown:{_safe_key(sig.ticker)}")
        if _redis_ttl > 0:
            log.debug("Stop-loss cooldown (Redis): %s  (%ds remaining)", sig.ticker, _redis_ttl)
            return _rejected("STOP_COOLDOWN")
    except Exception:
        pass  # Redis unavailable — fall through to in-memory only

    # ── Per-meeting concentration limit (FOMC correlation risk) ─────────────
    # KXFED tickers for the same meeting date are positively correlated:
    # T2.75/T3.00/T3.25 YES all win/lose together if the Fed holds. Cap per-meeting
    # exposure so a single meeting doesn't dominate the portfolio.
    # arb_partner signals are exempt: they're the second leg of an already-committed
    # two-leg trade. Blocking them would leave the first leg unhedged.
    if sig.meeting and sig.asset_class == "kalshi" and not getattr(sig, "arb_partner", None):
        all_pos      = await positions.get_all()
        # Arb legs from the same arb_id count as 1 (they're a single hedged position).
        # Standalone (non-arb) positions each count as 1.
        _meeting_arb_ids: set = set()
        _meeting_standalone  = 0
        for p in all_pos.values():
            if p.get("meeting") != sig.meeting:
                continue
            _aid = p.get("arb_id")
            if _aid:
                _meeting_arb_ids.add(_aid)
            else:
                _meeting_standalone += 1
        meeting_count = len(_meeting_arb_ids) + _meeting_standalone
        if meeting_count >= cfg.MAX_POSITIONS_PER_MEETING:
            log.debug(
                "Meeting concentration limit: %s already has %d/%d positions — skipping %s",
                sig.meeting, meeting_count, cfg.MAX_POSITIONS_PER_MEETING, sig.ticker,
            )
            return _rejected("MEETING_CONCENTRATION")

    # ── Balance ───────────────────────────────────────────────────────────────
    # BTC trades: use total Coinbase portfolio value (USD cash + BTC holdings at
    # current spot) so that sizing reflects the full account, not just USD cash.
    # Kalshi trades: use Kalshi balance published to Redis by Intel each cycle.
    balance_cents = 100_000   # paper default ($1,000)
    if sig.asset_class == "btc_spot":
        cb_bal = await coinbase.get_total_balance_cents(
            btc_price_usd=sig.btc_price or 0.0
        )
        if cb_bal is not None and cb_bal > 0:
            balance_cents = cb_bal
            log.debug("BTC sizing: Coinbase total balance = $%.2f", balance_cents / 100)
        # else: paper mode, fetch failed, or unfunded — keep paper default
    elif not cfg.PAPER_TRADE:
        balances  = await bus.get_all_balances()
        intel_bal = next(
            (v for k, v in balances.items() if "intel" in k.lower()),
            None,
        )
        if intel_bal is None:
            _entry_failed_cooldown[sig.ticker] = time.time()
            return _rejected("BALANCE_UNKNOWN")
        balance_cents = intel_bal.get("balance_cents", 0)

    # ── LLM policy overrides (written each cycle by llm_agent.py → ep:config) ─
    llm_kelly, llm_scale, llm_btc_on, llm_kal_on = await asyncio.gather(
        bus.get_config_override("llm_kelly_fraction"),
        bus.get_config_override("llm_scale_factor"),
        bus.get_config_override("llm_btc_enabled"),
        bus.get_config_override("llm_kalshi_enabled"),
    )

    # ── Asset-class kill switches ─────────────────────────────────────────────
    if sig.asset_class == "btc_spot" and llm_btc_on == "0":
        return _rejected("LLM_BTC_DISABLED")
    if sig.asset_class == "kalshi" and llm_kal_on == "0":
        return _rejected("LLM_KALSHI_DISABLED")

    # ── Volatility-adjusted sizing multiplier ────────────────────────────────
    # For economic release markets (GDP, CPI…) the edge quality is asymmetric:
    # - Pre-release (0–7 days before print): high model uncertainty → size down 30%
    # - Post-release (0–48h after print): uncertainty resolved → size up 40%
    # Only applies when ep:econ_release:status is available and the signal is
    # in the economic Kelly bucket (fred_* / gdp model_source).
    _vol_multiplier = 1.0
    _ms = (sig.model_source or "").lower()
    if "fred_" in _ms or "gdp" in _ms:
        try:
            _econ_raw = await bus._r.get("ep:econ_release:status")
            if _econ_raw:
                _econ = json.loads(_econ_raw if isinstance(_econ_raw, str) else _econ_raw.decode())
                _now = datetime.now(timezone.utc)
                _next_ts = _econ.get("next_time_utc")
                _last_ts = _econ.get("last_release_ts")
                if _next_ts:
                    _next_dt = datetime.fromisoformat(_next_ts.replace("Z", "+00:00"))
                    _hours_to_release = (_next_dt - _now).total_seconds() / 3600
                    if 0 < _hours_to_release <= 168:   # within 7 days before release
                        _vol_multiplier = 0.70
                        log.debug("vol_multiplier=0.70 (pre-release %.0fh)", _hours_to_release)
                if _last_ts and _vol_multiplier == 1.0:
                    _last_dt = datetime.fromisoformat(_last_ts.replace("Z", "+00:00"))
                    _hours_since = (_now - _last_dt).total_seconds() / 3600
                    if 0 <= _hours_since <= 48:        # within 48h after print
                        _vol_multiplier = 1.40
                        log.debug("vol_multiplier=1.40 (post-release %.0fh ago)", _hours_since)
        except Exception as _ve:
            log.debug("vol_multiplier lookup error: %s", _ve)

    # ── Kelly sizing ──────────────────────────────────────────────────────────
    # Priority: llm_kelly (operator override) > empirical calibration > configured default.
    # asyncio is cooperative — no await between override and restore, so this is race-free.
    orig_kelly = risk_engine._kalshi.cfg.kelly_fraction
    if llm_kelly:
        risk_engine._kalshi.cfg.kelly_fraction = max(0.05, min(0.50, float(llm_kelly)))
    elif sig.asset_class == "kalshi":
        _empirical = get_calibrated_kelly(sig.edge)
        if _empirical is not None:
            risk_engine._kalshi.cfg.kelly_fraction = _empirical
            log.debug("Kelly calib applied: edge=%.3f bucket_kelly=%.4f", sig.edge, _empirical)
    contracts = risk_engine.size(sig, balance_cents, vol_multiplier=_vol_multiplier)
    risk_engine._kalshi.cfg.kelly_fraction = orig_kelly   # always restore

    # Apply LLM scale factor after Kelly sizing (0.5 = half size, 1.5 = +50%)
    if llm_scale and contracts > 0:
        _scale = max(0.1, min(3.0, float(llm_scale)))
        contracts = max(1, int(contracts * _scale))

    _ABSOLUTE_MAX_CONTRACTS = 500  # hard safety cap — prevents Kelly misconfiguration
    if contracts > _ABSOLUTE_MAX_CONTRACTS:
        log.warning("Contract cap: %s sized %d → %d (hard limit)",
                    sig.ticker, contracts, _ABSOLUTE_MAX_CONTRACTS)
        contracts = _ABSOLUTE_MAX_CONTRACTS

    if contracts <= 0:
        metrics.record_risk_gate("kelly", "zero_size")
        _entry_failed_cooldown[sig.ticker] = time.time() - (_ENTRY_FAILED_COOLDOWN_S - _ENTRY_FAILED_COOLDOWN_SHORT)
        return _rejected("RISK_GATE_SIZE")
    metrics.record_kelly(sig.asset_class, contracts)

    # ── Book-depth gate ───────────────────────────────────────────────────────
    if sig.asset_class == "kalshi" and sig.book_depth is not None:
        _is_long = False
        if sig.close_time:
            try:
                _ct_bd = datetime.fromisoformat(sig.close_time.replace("Z", "+00:00"))
                _is_long = (_ct_bd - datetime.now(timezone.utc)).days > _LONG_GAME_HORIZON_DAYS
            except Exception:
                pass
        _min_depth = _MIN_BOOK_DEPTH_LONG if _is_long else _MIN_BOOK_DEPTH
        if sig.book_depth < _min_depth:
            log.info(
                "Book-depth gate: %s  depth=%d < min=%d (%s)  — skipping",
                sig.ticker, sig.book_depth, _min_depth,
                "long-game" if _is_long else "standard",
            )
            return _rejected("RISK_GATE_BOOK_DEPTH")

    # ── Directional exposure limits ───────────────────────────────────────────
    # Runs after Kelly sizing so sig_cost uses the actual contract count.
    # Only applies to Kalshi — BTC sizing is governed by btc_risk.
    if sig.asset_class == "kalshi":
        _all_pos  = await positions.get_all()
        _sig_cost = ((100 - int(sig.market_price * 100)) if sig.side == "no"
                     else int(sig.market_price * 100)) * contracts

        _long_exp = _short_exp = 0
        for _p in _all_pos.values():
            if _p.get("arb_id"):
                continue
            _t_c = _p.get("contracts_filled") or _p.get("contracts", 1)
            _t_e = _p.get("entry_cents", 0)
            if _p.get("side") == "no":
                _short_exp += (100 - _t_e) * _t_c
            else:
                _long_exp += _t_e * _t_c

        _side_exp = _long_exp if sig.side == "yes" else _short_exp
        _side_cap = balance_cents * (_MAX_LONG_PCT if sig.side == "yes" else _MAX_SHORT_PCT)
        _cap_name = "LONG_LIMIT" if sig.side == "yes" else "SHORT_LIMIT"

        if _side_exp + _sig_cost > _side_cap:
            _remaining = max(0, int(_side_cap - _side_exp))
            _unit_cost = _sig_cost // contracts if contracts > 0 else _sig_cost
            _cap_max   = max(0, _remaining // _unit_cost) if _unit_cost > 0 else 0
            if _cap_max <= 0:
                log.info("%s full (%.0f¢/%.0f¢) — skipping %s",
                         _cap_name, _side_exp, _side_cap, sig.ticker)
                return _rejected(_cap_name)
            log.info("%s: trimming %s %d→%d contracts",
                     _cap_name, sig.ticker, contracts, _cap_max)
            contracts  = _cap_max
            _sig_cost  = _unit_cost * contracts

        if _sig_cost > balance_cents * _MAX_MARKET_PCT:
            log.info("Market limit hit (%.0f¢ > %.0f¢) — skipping %s",
                     _sig_cost, balance_cents * _MAX_MARKET_PCT, sig.ticker)
            return _rejected("MARKET_LIMIT")

    # ── Long-game capital cap ─────────────────────────────────────────────────
    # Positions settling > _LONG_GAME_HORIZON_DAYS out are capped at
    # _LONG_GAME_MAX_PCT of balance so active/daily capital stays available.
    if sig.asset_class == "kalshi" and sig.close_time:
        try:
            _ct = datetime.fromisoformat(sig.close_time.replace("Z", "+00:00"))
            _days_out = (_ct - datetime.now(timezone.utc)).total_seconds() / 86400
            if _days_out > _LONG_GAME_HORIZON_DAYS:
                _lg_deployed = 0
                _lg_all = _all_pos
                for _lp in _lg_all.values():
                    _lp_ct = _lp.get("close_time", "")
                    if not _lp_ct:
                        continue
                    try:
                        _lp_exp  = datetime.fromisoformat(_lp_ct.replace("Z", "+00:00"))
                        _lp_days = (_lp_exp - datetime.now(timezone.utc)).total_seconds() / 86400
                    except Exception:
                        continue
                    if _lp_days > _LONG_GAME_HORIZON_DAYS:
                        _lp_e = _lp.get("entry_cents", 0)
                        _lp_c = int(_lp.get("contracts", 0))
                        _lg_deployed += (100 - _lp_e) * _lp_c if _lp.get("side") == "no" else _lp_e * _lp_c
                _lg_limit = balance_cents * _LONG_GAME_MAX_PCT
                if _lg_deployed + _sig_cost > _lg_limit:
                    _lg_capacity = max(0, int(_lg_limit - _lg_deployed))
                    _unit = _sig_cost // contracts if contracts > 0 else _sig_cost
                    _lg_max = max(0, _lg_capacity // _unit) if _unit > 0 else 0
                    if _lg_max <= 0:
                        log.info("Long-game cap full (%.0f¢/%.0f¢) — skipping %s (%.0fd out)",
                                 _lg_deployed, _lg_limit, sig.ticker, _days_out)
                        return _rejected("LONG_GAME_CAP")
                    if _lg_max < contracts:
                        log.info("Long-game cap: trimming %s %d→%d contracts (%.0fd out)",
                                 sig.ticker, contracts, _lg_max, _days_out)
                        contracts = _lg_max
                        _sig_cost = _unit * contracts
        except Exception as _lg_exc:
            log.debug("Long-game cap check failed for %s: %s", sig.ticker, _lg_exc)

    # ── Risk approval ─────────────────────────────────────────────────────────
    try:
        open_exposure = await positions.total_exposure_cents()
    except Exception:
        log.warning("RISK_GATE_REDIS: Redis unavailable during exposure check — rejecting %s", sig.ticker)
        return _rejected("RISK_GATE_REDIS")
    approved, reason    = risk_engine.approve(sig, contracts, balance_cents, open_exposure)
    if not approved:
        log.info("Risk rejected %s: %s  signal_id=%.8s", sig.ticker, reason, sig.signal_id)
        return _rejected(reason or "RISK_GATE_KALSHI")

    # ── Pre-write pending position (crash protection) ────────────────────────
    # Write before executing so a crash between execute() and positions.open()
    # cannot create a live order with no Redis record.  On failure we delete;
    # on restart, any "pending" entry older than 30 min is cleaned up.
    _entry_cents = (
        int((sig.btc_price or sig.market_price) * BTC_UNIT * 100)
        if sig.asset_class == "btc_spot"
        else int(sig.market_price * 100)
    )
    await positions.open(
        ticker       = sig.ticker,
        side         = sig.side,
        contracts    = contracts,
        entry_cents  = _entry_cents,
        fair_value   = sig.fair_value,
        meeting      = sig.meeting or "",
        outcome      = sig.outcome or "",
        close_time   = sig.close_time or "",
        model_source = sig.model_source or "",
        pending      = True,
    )

    # Store GDPNow at entry for KXGDP positions so we can log it at exit time
    # and calibrate the 0.75pp cut-loss threshold empirically.
    if sig.ticker.startswith("KXGDP-"):
        try:
            _gdpnow_raw = await bus._r.hget("ep:macro", "gdpnow")
            if _gdpnow_raw:
                await positions.update_fields(sig.ticker, {
                    "gdpnow_at_entry": float(_gdpnow_raw)
                })
        except Exception:
            pass

    # ── Execute (route by asset class) ───────────────────────────────────────
    if sig.asset_class == "btc_spot":
        size_str    = f"{contracts * BTC_UNIT:.8f}"
        entry_cents = _entry_cents
        executed    = await _execute_btc(sig, size_str, coinbase)
    elif sig.asset_class == "kalshi" and sig.arb_legs:
        # ── Multi-leg structural arb (butterfly / N-leg) ─────────────────────
        # arb_partner handles 2-leg monotonicity arbs separately (below).
        # This path handles butterfly spreads and any other N-leg arbs where
        # all legs are carried explicitly in sig.arb_legs.
        #
        # Remove the pending pre-write (written above for crash protection) now —
        # each arb leg will be written individually after it is placed, so the
        # pre-write for sig.ticker is redundant and would conflict with leg writes.
        await positions.close(sig.ticker)

        import uuid as _uuid
        arb_id  = str(_uuid.uuid4())[:8]
        _arb_ok = False
        try:
            _leg_order_ids = await asyncio.to_thread(
                executor.execute_arb_legs,
                sig.arb_legs, contracts_per_leg=1, parent_signal=sig,
            )
            _arb_ok = True
            # Write each arb leg into Redis ep:positions with the shared arb_id.
            # Use close() + open() so existing entries (e.g., from a prior arb
            # attempt) are cleanly replaced rather than merged.
            for _i, (_leg, _oid) in enumerate(zip(sig.arb_legs, _leg_order_ids)):
                _leg_ticker = _leg["ticker"]
                _leg_side   = _leg["side"]
                _leg_price  = int(_leg.get("price_cents", 50))
                # entry_cents convention: always YES price.
                # For NO legs, NO price = price_cents; YES price = 100 - no_price.
                _entry_c = (100 - _leg_price) if _leg_side == "no" else _leg_price
                await positions.close(_leg_ticker)   # clear any stale entry first
                await positions.open(
                    ticker       = _leg_ticker,
                    side         = _leg_side,
                    contracts    = 1,
                    entry_cents  = _entry_c,
                    fair_value   = sig.fair_value,
                    meeting      = sig.meeting or "",
                    outcome      = sig.outcome or "",
                    close_time   = sig.close_time or "",
                    model_source = sig.model_source or "",
                    pending      = False,
                )
                await positions.update_fields(_leg_ticker, {
                    "order_id":       _oid,
                    "fill_confirmed": _oid == "paper",   # paper fills are instant
                    "arb_id":         arb_id,
                    "arb_leg_index":  _i,
                })
            log.info(
                "[ARB ENTRY] %d legs registered in Redis  arb_id=%s  signal_id=%.8s",
                len(sig.arb_legs), arb_id, sig.signal_id,
            )
        except ArbRollbackFailed as _arb_exc:
            # Partial unwind: some earlier legs are open on Kalshi with no Redis record.
            # Write each unrecovered leg as an orphaned position so exit_checker can
            # close it on the next cycle.  Then fire a critical alert so the operator
            # knows there is untracked exposure that must be resolved.
            log.error(
                "ARB ROLLBACK PARTIAL: %d unrecovered leg(s) — writing to Redis as orphaned  "
                "signal_id=%.8s",
                len(_arb_exc.unrecovered), sig.signal_id,
            )
            for _ot, _os, _ooid in _arb_exc.unrecovered:
                try:
                    await positions.close(_ot)   # clear stale entry if any
                    await positions.open(
                        ticker       = _ot,
                        side         = _os,
                        contracts    = 1,
                        entry_cents  = 50,        # unknown fill; midpoint is safe default
                        fair_value   = 0.5,
                        model_source = "arb_unrecovered",
                        pending      = False,
                    )
                    await positions.update_fields(_ot, {
                        "order_id":            _ooid,
                        "fill_confirmed":       True,   # we know it filled (cancel returned error)
                        "arb_unrecovered":      True,
                        "immediate_exit":       True,
                        "immediate_exit_reason": "arb_partial_fill",
                    })
                    log.error("Orphaned leg written to Redis: %s %s order_id=%s", _ot, _os, _ooid)
                except Exception as _we:
                    log.error("Failed to write orphaned leg %s to Redis: %s", _ot, _we)
            # Emit critical alert
            _alert_msg = (
                f"ARB ROLLBACK PARTIAL: {len(_arb_exc.unrecovered)} unrecovered leg(s) "
                f"written to Redis as 'arb_unrecovered'. "
                f"Exit checker will attempt to close. "
                f"Verify on Kalshi dashboard. signal={sig.signal_id[:8]}"
            )
            try:
                if bus._r:
                    await bus._r.xadd("ep:alerts", {"payload": json.dumps({
                        "ts":           datetime.now(timezone.utc).isoformat(),
                        "severity":     "critical",
                        "category":     "system",
                        "title":        "Arb unwind partial — untracked exposure",
                        "message":      _alert_msg,
                        "action":       "Check Kalshi dashboard; arb_unrecovered positions queued for exit",
                        "auto_applied": False,
                    })}, maxlen=500, approximate=True)
            except Exception:
                pass
            try:
                await telegram.send_alert(f"[CRITICAL] {_alert_msg}", level="critical")
            except Exception:
                pass
        except RuntimeError as _arb_exc:
            log.warning("Multi-leg arb FAILED (clean rollback): %s  signal_id=%.8s",
                        _arb_exc, sig.signal_id)
        executed = _arb_ok
    elif sig.asset_class == "kalshi":
        if sig.ticker in executor._positions:
            # Executor has an in-memory entry but Redis does not — Redis was wiped.
            # Always restore from executor so dedup blocks re-entry on future cycles.
            # Never clear a live executor entry: if the position is wrong, fill_poll
            # will cancel it; clearing here only triggers immediate re-entry.
            ex_pos = executor._positions[sig.ticker]

            # Exception: if the executor entry has no order_id and is not
            # fill_confirmed, this is a ghost from a failed order placement
            # (executor added the position before the HTTP call that then failed).
            # Drop it and fall through to normal execution so the signal retries.
            if (not ex_pos.get("order_id") and
                    not ex_pos.get("fill_confirmed")):
                log.warning(
                    "State divergence: %s in executor has no order_id "
                    "— dropping ghost position (failed order), allowing retry",
                    sig.ticker,
                )
                _entry_failed_cooldown[sig.ticker] = time.time()
                executor._positions.pop(sig.ticker, None)
                # Leave the pending pre-write in Redis (written above) so normal
                # execution below can confirm or clean it up.
            else:
                # Valid live position — restore to Redis and block re-entry.
                await positions.close(sig.ticker)        # remove the pending pre-write
                log.warning(
                    "State divergence: %s in executor but not Redis "
                    "— restoring to Redis (fill_confirmed=%s)",
                    sig.ticker, ex_pos.get("fill_confirmed", False),
                )
                await positions.open(
                    ticker       = sig.ticker,
                    side         = ex_pos.get("side", "yes"),
                    contracts    = int(ex_pos.get("contracts", 1)),
                    entry_cents  = int(ex_pos.get("entry_cents", 50)),
                    fair_value   = float(ex_pos.get("fair_value", 0.5)),
                    meeting      = ex_pos.get("meeting", ""),
                    outcome      = ex_pos.get("outcome", ""),
                    close_time   = ex_pos.get("close_time", ""),
                    model_source = ex_pos.get("model_source", ""),
                    pending      = False,
                )
                await positions.update_fields(sig.ticker, {
                    "order_id":       ex_pos.get("order_id", ""),
                    "fill_confirmed": ex_pos.get("fill_confirmed", False),
                })
                return _rejected("EXECUTOR_DEDUP")
        exec_signal           = message_to_kalshi_signal(sig)
        exec_signal.contracts = contracts
        order_id              = await asyncio.to_thread(executor.execute, exec_signal)
        executed              = bool(order_id)
        # ── Circuit breaker accounting ────────────────────────────────────────
        if executed:
            _kalshi_api_failures = 0
            _kalshi_api_failure_ts = 0.0
        else:
            # Only count non-409 failures toward the circuit breaker.
            # 409 Conflict is a dedup/state issue, not an API health signal.
            if _kalshi_api_failures == 0:
                _kalshi_api_failure_ts = time.time()
            _kalshi_api_failures += 1
            if _kalshi_api_failures >= _KALSHI_API_FAILURE_THRESHOLD:
                log.warning(
                    "KALSHI_API_CIRCUIT_OPEN: %d consecutive failures — check Kalshi API",
                    _kalshi_api_failures,
                )
                try:
                    await telegram.send_alert(
                        f"KALSHI_API_CIRCUIT_OPEN: {_kalshi_api_failures} consecutive "
                        "executor failures — check Kalshi API health",
                        level="warning",
                    )
                except Exception:
                    pass
    else:
        await positions.close(sig.ticker)       # remove the pending entry
        _entry_failed_cooldown[sig.ticker] = time.time()
        return _rejected("UNKNOWN_ASSET_CLASS")

    if not executed:
        # For arb-legs path the pending primary position was already removed inside
        # the branch above; for other paths remove it here.
        if not sig.arb_legs:
            await positions.close(sig.ticker)       # remove the pending entry
        _entry_failed_cooldown[sig.ticker] = time.time()
        log.warning(
            "Entry failed for %s — suppressing for %.0f min (ENTRY_FAILED_COOLDOWN)  signal_id=%.8s",
            sig.ticker, _ENTRY_FAILED_COOLDOWN_S / 60, sig.signal_id,
        )
        return _rejected("EXECUTOR_REJECTED")

    # ── Multi-leg arb: return early — legs are already in Redis ─────────────
    # The arb_legs branch already wrote each leg into ep:positions individually
    # and removed the pending primary-ticker entry.  Skip the single-leg confirm
    # step, the arb_partner block, and use the summed leg costs for the report.
    if sig.arb_legs and sig.asset_class == "kalshi":
        _arb_cost = sum(
            lg.get("price_cents", 50) for lg in sig.arb_legs
        )  # total cents laid out across all legs (1 contract each)
        _arb_fee  = int(_arb_cost * cfg.FEE_CENTS / 100)
        metrics.signal_published(sig.asset_class, sig.strategy or "fomc_arb", sig.side)
        log.info(
            "ARB executed: %d legs  primary=%s  total_cost=%d¢  signal_id=%.8s",
            len(sig.arb_legs), sig.ticker, _arb_cost, sig.signal_id,
        )
        return ExecutionReport(
            signal_id     = sig.signal_id,
            ticker        = sig.ticker,
            asset_class   = sig.asset_class,
            side          = sig.side,
            contracts     = len(sig.arb_legs),
            fill_price    = sig.market_price,
            status        = "filled",
            mode          = "paper" if cfg.PAPER_TRADE else "live",
            cost_cents    = _arb_cost,
            fee_cents     = _arb_fee,
            edge_captured = sig.edge - (_arb_fee / 100),
        )

    # ── Confirm position (remove pending flag, store order_id) ───────────────
    confirm_fields: dict = {"pending": False}
    if sig.asset_class == "kalshi":
        confirm_fields["order_id"]      = order_id        # UUID or "paper"
        confirm_fields["fill_confirmed"] = False           # poll loop updates this
    await positions.update_fields(sig.ticker, confirm_fields)

    # For NO contracts the actual outlay is the NO price, not the YES price.
    # market_price is always the YES mid; NO cost = (1 - market_price).
    if sig.side == "no" and sig.asset_class == "kalshi":
        cost_cents = int((1.0 - sig.market_price) * 100) * contracts
    else:
        cost_cents = int(sig.market_price * 100) * contracts
    fee_cents  = int(cost_cents * cfg.FEE_CENTS / 100) if sig.asset_class == "kalshi" else 0

    metrics.signal_published(sig.asset_class, sig.strategy or "unknown", sig.side)
    metrics.record_fill_latency(
        sig.asset_class,
        (int(time.time() * 1_000_000) - sig.ts_us) / 1_000_000,
    )
    metrics.record_risk_gate("all_gates", "pass")
    log.info("Executed: %s %s ×%d @ %.4f  cost=$%.2f  signal_id=%.8s",
             sig.ticker, sig.side, contracts, sig.market_price, cost_cents / 100, sig.signal_id)

    try:
        await telegram.send_trade_alert(
            ticker      = sig.ticker,
            side        = sig.side,
            contracts   = contracts,
            entry_cents = _entry_cents,
            strategy    = getattr(sig, "strategy", "") or "",
        )
    except Exception:
        pass

    try:
        await telegram.send_fill(
            ticker      = sig.ticker,
            side        = sig.side,
            contracts   = contracts,
            price_cents = int(sig.market_price * 100),
            mode        = "paper" if cfg.PAPER_TRADE else "live",
            edge        = sig.edge,
            strategy    = getattr(sig, "strategy", "") or "",
        )
    except Exception:
        pass

    # ── ARB partner leg (atomic second leg for monotonicity arb) ─────────────
    # When sig.arb_partner is set, the signal is a two-leg arb:
    #   Primary : sig.ticker      side=yes  buy the underpriced YES
    #   Partner : sig.arb_partner side=no   buy the overpriced NO (= sell YES)
    # Execute the partner immediately in the same task — no await between legs
    # means no other signal can slip between them.
    if sig.arb_partner and "ARB_PARTNER" in (sig.risk_flags or []):
        partner_already_open = await positions.exists(sig.arb_partner)
        if not partner_already_open:
            # Resolve partner NO price from Redis prices (Intel publishes each cycle)
            partner_prices = await bus.get_prices([sig.arb_partner])
            pdata          = partner_prices.get(sig.arb_partner, {})
            # no_price = 1 - yes_price; fall back to complement of primary fair_value
            partner_price = (
                pdata.get("no_price")
                or (1.0 - (pdata.get("yes_price") or sig.fair_value))
            )
            partner_price = max(0.01, min(0.99, float(partner_price)))

            from kalshi_bot.strategy import Signal as KSignal
            partner_sig = KSignal(
                ticker            = sig.arb_partner,
                title             = sig.arb_partner,
                category          = sig.category or "arb",
                side              = "no",
                fair_value        = 1.0 - sig.fair_value,
                market_price      = partner_price,
                edge              = sig.edge,
                fee_adjusted_edge = sig.fee_adjusted_edge,
                contracts         = contracts,
                confidence        = sig.confidence,
                model_source      = f"arb_partner:{sig.ticker}",
                meeting           = sig.meeting or "",
                outcome           = sig.outcome or "",
            )
            executor.reset_cycle()
            partner_order_id = executor.execute(partner_sig)
            if partner_order_id:
                await positions.open(
                    ticker       = sig.arb_partner,
                    side         = "no",
                    contracts    = contracts,
                    entry_cents  = int((1.0 - partner_price) * 100),
                    fair_value   = 1.0 - sig.fair_value,
                    meeting      = sig.meeting or "",
                    outcome      = sig.outcome or "",
                    close_time   = sig.close_time or "",
                    model_source = sig.model_source or "",
                )
                await positions.update_fields(sig.arb_partner, {
                    "order_id":       partner_order_id,
                    "fill_confirmed": False,
                })
                cost_cents += int(partner_price * 100) * contracts
                log.info("ARB partner: %s NO ×%d @ %.4f  (pair: %s)",
                         sig.arb_partner, contracts, partner_price, sig.ticker)
            else:
                log.warning(
                    "ARB partner leg FAILED for %s — unwinding primary %s to stay flat",
                    sig.arb_partner, sig.ticker,
                )
                # Close the primary immediately — net exposure is zero
                primary_pos = {
                    "side": sig.side, "contracts": contracts,
                    "entry_cents": int(sig.market_price * 100),
                    "close_time": sig.close_time or "",
                    "meeting": sig.meeting or "", "outcome": sig.outcome or "",
                }
                executor._exit_position(
                    sig.ticker, primary_pos,
                    int(sig.market_price * 100),
                    "arb_partner_failed",
                )
                await positions.close(sig.ticker)
                return _rejected("ARB_PARTNER_FAILED")
        else:
            log.debug("ARB partner %s already held — skipping second leg", sig.arb_partner)

    return ExecutionReport(
        signal_id     = sig.signal_id,
        ticker        = sig.ticker,
        asset_class   = sig.asset_class,
        side          = sig.side,
        contracts     = contracts,
        fill_price    = sig.market_price,
        status        = "filled",
        mode          = "paper" if cfg.PAPER_TRADE else "live",
        cost_cents    = cost_cents,
        fee_cents     = fee_cents,
        edge_captured = sig.edge - (cfg.FEE_CENTS * contracts) / 100,
    )


async def _cleanup_stale_prices(bus: RedisBus, max_age_s: int = 86400) -> None:
    """Delete entries from ep:prices that have not been updated within max_age_s seconds."""
    try:
        entries = await bus._r.hgetall(EP_PRICES)
        now_us = time.time() * 1_000_000
        stale: list[str] = []
        for ticker, raw in entries.items():
            try:
                data = json.loads(raw)
                ts_us = data.get("ts_us", 0)
                if (now_us - ts_us) > max_age_s * 1_000_000:
                    stale.append(ticker)
            except Exception:
                pass
        if stale:
            await bus._r.hdel(EP_PRICES, *stale)
            log.debug("cleanup_stale_prices: removed %d stale entries from ep:prices", len(stale))
    except Exception as exc:
        log.debug("cleanup_stale_prices: error (non-fatal): %s", exc)


async def _heartbeat_loop(bus: RedisBus, interval: int = 60) -> None:
    """Publish a HEARTBEAT event to ep:system every `interval` seconds."""
    while True:
        await asyncio.sleep(interval)
        sd_notify("WATCHDOG=1")
        await bus.publish_system_event("HEARTBEAT")

        # Update /health endpoint state
        try:
            from ep_pg_audit import audit as _audit
            redis_ok = await bus.ping()
            try:
                q_size = _audit()._queue.qsize()
                pg_status = "ok" if q_size < 5_000 else "degraded"
            except RuntimeError:
                pg_status = "disabled"
                q_size = 0
            metrics.set_health({
                "status":           "ok" if redis_ok else "fail",
                "redis":            "ok" if redis_ok else "fail",
                "postgres":         pg_status,
                "postgres_queue":   q_size,
            })
        except Exception:
            pass


async def _signal_consumer(
    bus:         RedisBus,
    positions:   PositionStore,
    risk_engine: UnifiedRiskEngine,
    executor:    Executor,
    coinbase:    CoinbaseTradeClient,
) -> None:
    """
    Async task: drain ep:signals continuously via XREADGROUP.
    Runs independently of the exit-check task — asyncio.gather handles both.
    """
    consumer_name = f"{NODE_ID}-c1"
    log.info("Signal consumer started (consumer=%s)", consumer_name)

    async for entry_id, sig in bus.consume_signals(consumer_name):
        # Check ops-level halt before processing every signal.
        # Intel also checks this and stops publishing when halted, but signals
        # already queued in ep:signals before the halt must be refused here too.
        if await bus.is_halted():
            log.warning("HALT_TRADING set — acking signal %s without executing", sig.ticker)
            await bus.ack_signal(entry_id)
            continue

        # Clear per-signal held set — Redis ep:positions is the session dedup;
        # _held prevents duplicate entry within a single atomic operation only.
        executor.reset_cycle()
        try:
            # Measure time signal spent waiting in Redis stream before we read it
            _stream_lag_s = (int(time.time() * 1_000_000) - sig.ts_us) / 1_000_000
            metrics.record_stream_lag(sig.asset_class, max(0.0, _stream_lag_s))

            _proc_start = time.monotonic()
            report = await _process_signal(sig, bus, positions, risk_engine, executor, coinbase)
            metrics.record_risk_processing(
                sig.asset_class, time.monotonic() - _proc_start
            )

            # Store exec_id on the position so position_history can link back to executions
            if report.status == "filled" and report.ticker:
                try:
                    await positions.update_fields(report.ticker, {"exec_id": report.exec_id})
                except Exception:
                    pass
            await bus.publish_execution(report)
        except Exception:
            log.exception("Unhandled error processing %s", sig.ticker)
            # Clean up any pending position written before the error so it
            # doesn't linger until the 30-min startup cleanup window.
            try:
                pos_data = (await positions.get_all()).get(sig.ticker, {})
                if pos_data.get("pending"):
                    await positions.close(sig.ticker)
                    log.warning("Cleaned up pending position for %s after unhandled error",
                                sig.ticker)
            except Exception:
                pass   # best-effort; startup cleanup will catch it if this fails
        finally:
            # Always ack — failed signals become audit entries, not retry storms
            await bus.ack_signal(entry_id)


def _kalshi_entry_cents(mp: dict) -> int:
    """
    Compute entry_cents (= YES price at entry) from a Kalshi market_position dict.

    entry_cents convention: always the YES price (0-100) regardless of side.

    When total_traded_dollars / position gives a price outside [1,99] — which
    happens for positions with a complex fill history (partial exits + re-entries) —
    fall back to market_exposure_dollars as a proxy for current market value,
    then derive a current-price entry_cents (makes unrealized_pnl ≈ 0 but keeps
    total_value accurate).
    """
    pos_fp     = float(mp.get("position_fp", 0) or 0)
    contracts  = int(abs(pos_fp))
    if contracts == 0:
        return 50
    side       = "yes" if pos_fp > 0 else "no"
    traded_usd = float(mp.get("total_traded_dollars", 0) or 0)
    avg_price  = traded_usd / contracts if traded_usd > 0 else 0.0

    # avg_price is the per-contract price for the side we hold
    if side == "yes":
        entry = round(avg_price * 100)
    else:
        # avg_price = NO_price paid; YES_price = 1 - NO_price
        entry = round((1.0 - avg_price) * 100)

    if 1 <= entry <= 99:
        return entry

    # Fallback: market_exposure_dollars is the maximum payout (contracts × $1),
    # not the current mark, so it's only reliable for YES positions.
    exposure_usd = float(mp.get("market_exposure_dollars", 0) or 0)
    if exposure_usd > 0 and contracts > 0 and side == "yes":
        cur_price = exposure_usd / contracts
        entry = round(cur_price * 100)
        if 1 <= entry <= 99:
            return entry

    return 50  # last resort


async def _sync_positions_with_kalshi(
    positions: "PositionStore",
    executor:  "Executor",
) -> None:
    """
    Reconcile ep:positions against the live Kalshi portfolio.

    - Adds any Kalshi positions missing from Redis.
    - Updates qty, side, or entry_cents when Redis diverges from Kalshi.
    - Removes (closes) fill_confirmed Redis entries no longer on Kalshi.

    Skips entries with fill_confirmed=False (pending orders).
    """
    try:
        resp = await asyncio.to_thread(executor.client.get, "/portfolio/positions",
                                       params={"limit": 200})
    except Exception as exc:
        log.warning("Position sync: Kalshi API call failed (%s)", exc)
        return

    kalshi_map: dict[str, dict] = {}
    for mp in resp.get("market_positions", []):
        ticker = mp.get("ticker", "")
        pos_fp = float(mp.get("position_fp", 0) or 0)
        if ticker and abs(pos_fp) >= 1:
            kalshi_map[ticker] = mp

    redis_positions = await positions.get_all()
    added = updated = removed = 0

    # ── Add / update from Kalshi source of truth ─────────────────────────────
    for ticker, mp in kalshi_map.items():
        pos_fp    = float(mp["position_fp"])
        k_side    = "yes" if pos_fp > 0 else "no"
        k_qty     = int(abs(pos_fp))
        k_entry   = _kalshi_entry_cents(mp)

        existing = redis_positions.get(ticker, {})
        r_qty    = int(existing.get("contracts", 0))
        r_side   = existing.get("side", "")
        r_entry  = int(existing.get("entry_cents", 0))

        r_cf     = int(existing.get("contracts_filled", 0))
        if r_qty > 0 and r_side == k_side and r_qty == k_qty and abs(r_entry - k_entry) <= 2 and r_cf == k_qty:
            continue  # already correct

        if r_qty == 0:
            if existing:
                # contracts=0 tombstone in Redis — blocked from re-entry, skip.
                continue
            # Genuinely missing from Redis — open fresh
            await positions.open(ticker=ticker, side=k_side, contracts=k_qty,
                                 entry_cents=k_entry, fair_value=k_entry / 100.0,
                                 pending=False)
            await positions.update_fields(ticker, {
                "fill_confirmed": True,
                "order_id":       "",
                "contracts_filled": k_qty,
            })
            added += 1
        else:
            # Update diverged fields in-place — always sync contracts_filled so
            # fill_poll (which skips fill_confirmed=True) can't leave it at 0.
            patch: dict = {"fill_confirmed": True, "contracts_filled": k_qty}
            if r_qty != k_qty:
                patch["contracts"] = k_qty
            if r_side != k_side:
                patch["side"] = k_side
            if abs(r_entry - k_entry) > 2:
                patch["entry_cents"] = k_entry
            await positions.update_fields(ticker, patch)
            updated += 1

        if executor is not None:
            executor._positions[ticker] = {
                "side": k_side, "entry_cents": k_entry,
                "contracts": k_qty, "contracts_filled": k_qty, "fill_confirmed": True,
            }

    # ── Remove stale confirmed positions no longer on Kalshi ─────────────────
    for ticker, pos in redis_positions.items():
        if not pos.get("fill_confirmed", False):
            continue
        if int(pos.get("contracts", 0)) == 0:
            continue
        if ticker in kalshi_map:
            continue
        await positions.close(ticker)
        if executor is not None:
            executor._positions.pop(ticker, None)
        removed += 1
        log.info("Position sync: removed stale %s (not in Kalshi portfolio)", ticker)

    if added or updated or removed:
        log.warning("Position sync: +%d added, ~%d updated, -%d removed",
                    added, updated, removed)
    else:
        log.debug("Position sync: ep:positions matches Kalshi (%d positions)",
                  len(kalshi_map))


async def _exit_checker(
    bus:         RedisBus,
    positions:   PositionStore,
    executor:    Executor,
    risk_engine: UnifiedRiskEngine = None,
    db:          Optional[ResolutionDB] = None,
    coinbase:    Optional["CoinbaseTradeClient"] = None,
) -> None:
    """
    Async task: check open positions for take-profit / stop-loss every
    EXIT_INTERVAL seconds, using prices published by Intel to Redis.
    Runs independently of _signal_consumer.
    """
    log.info("Exit checker started (interval=%ds)", EXIT_INTERVAL)
    _last_price_cleanup   = 0.0
    _last_pos_sync        = 0.0

    while True:
        await asyncio.sleep(EXIT_INTERVAL)

        # ── Hourly stale-price cleanup ────────────────────────────────────────
        _now = time.time()
        if _now - _last_price_cleanup >= 3600:
            await _cleanup_stale_prices(bus)
            _last_price_cleanup = _now

        # ── Every 30 min: sync ep:positions against Kalshi portfolio ─────────
        # Adds missing filled positions and removes stale entries that Kalshi
        # no longer holds (resolved markets, externally closed positions).
        if _now - _last_pos_sync >= 1800:
            try:
                await _sync_positions_with_kalshi(positions, executor)
                _last_pos_sync = time.time()
            except Exception as _sync_exc:
                log.warning("Periodic position sync error: %s", _sync_exc)

        try:
            # ── Auto-tombstone: consume ep:tombstone:{ticker} keys from Intel ──
            # Intel writes these when GDPNow is >2pp below strike AND ≤7 days left.
            # Pattern-scan for matching keys and call cancel_and_tombstone() on each.
            try:
                _tombstone_keys = await bus._r.keys("ep:tombstone:*")
                for _tk in _tombstone_keys:
                    _raw_tk   = _tk.decode() if isinstance(_tk, bytes) else _tk
                    _t_ticker = _raw_tk.replace("ep:tombstone:", "")
                    log.info("Auto-tombstone triggered for %s — calling cancel_and_tombstone",
                             _t_ticker)
                    await bus._r.delete(_raw_tk)   # delete raw key — _safe_key strips dots
                    await cancel_and_tombstone(_t_ticker, executor.client, positions, executor)
            except Exception as _tomb_exc:
                log.debug("Tombstone consumer error (non-fatal): %s", _tomb_exc)

            current_positions = await positions.get_all()
            if not current_positions:
                continue

            # ── Cut-loss consumer: Intel signals a fundamental reversal ──────────
            # ep:cut_loss:{ticker} written by Intel when GDPNow (or future signals)
            # strongly contradict the held position.  Filled positions are sold via
            # the normal exit path; resting orders are canceled and tombstoned.
            _cutloss_tickers: set = set()
            try:
                _cl_keys = await bus._r.keys("ep:cut_loss:*")
                for _clk in _cl_keys:
                    _clk_s  = _clk.decode() if isinstance(_clk, bytes) else _clk
                    _cl_t   = _clk_s.replace("ep:cut_loss:", "")
                    _cl_why = await bus._r.get(_clk_s) or "signal_reversed"
                    await bus._r.delete(_clk_s)
                    _cl_pos = current_positions.get(_cl_t)
                    if not _cl_pos or _cl_pos.get("contracts", 1) == 0:
                        continue
                    if not _cl_pos.get("fill_confirmed", True):
                        log.info("Cut-loss: %s resting order → cancel_and_tombstone (%s)",
                                 _cl_t, _cl_why)
                        await cancel_and_tombstone(_cl_t, executor.client, positions, executor)
                    else:
                        log.warning("Cut-loss queued for exit: %s (%s)", _cl_t, _cl_why)
                        _cutloss_tickers.add(_cl_t)
            except Exception as _cle:
                log.debug("Cut-loss consumer error (non-fatal): %s", _cle)

            # ── Redis config overrides (written by dashboard → ep:config) ─────
            _ov_tp, _ov_sl, _ov_hbc, _ov_ts = await asyncio.gather(
                bus.get_config_override("override_take_profit_cents"),
                bus.get_config_override("override_stop_loss_cents"),
                bus.get_config_override("override_hours_before_close"),
                bus.get_config_override("override_trailing_stop_cents"),
            )
            # Guard against zero/negative and malformed overrides.
            # Malformed values (e.g. "abc" written directly via redis-cli) raise
            # ValueError from int(float(x)) — fall back to cfg default and log.
            try:
                take_profit_cents = max(1, int(float(_ov_tp))) if _ov_tp else cfg.TAKE_PROFIT_CENTS
            except (ValueError, TypeError):
                log.warning("Malformed override_take_profit_cents=%r — using default", _ov_tp)
                take_profit_cents = cfg.TAKE_PROFIT_CENTS
            try:
                stop_loss_cents = max(1, int(float(_ov_sl))) if _ov_sl else cfg.STOP_LOSS_CENTS
            except (ValueError, TypeError):
                log.warning("Malformed override_stop_loss_cents=%r — using default", _ov_sl)
                stop_loss_cents = cfg.STOP_LOSS_CENTS
            try:
                hours_before_close = float(_ov_hbc) if _ov_hbc else cfg.HOURS_BEFORE_CLOSE
            except (ValueError, TypeError):
                log.warning("Malformed override_hours_before_close=%r — using default", _ov_hbc)
                hours_before_close = cfg.HOURS_BEFORE_CLOSE
            try:
                trailing_stop_base = max(1, int(float(_ov_ts))) if _ov_ts else cfg.TRAILING_STOP_CENTS
            except (ValueError, TypeError):
                log.warning("Malformed override_trailing_stop_cents=%r — using default", _ov_ts)
                trailing_stop_base = cfg.TRAILING_STOP_CENTS

            tickers     = list(current_positions.keys())
            prices      = await bus.get_prices(tickers)
            stale_cutoff = int(time.time() * 1_000_000) - 300 * 1_000_000   # 5 min

            for ticker, pos in current_positions.items():
                # ── Skip positions already queued for exit ────────────────────
                # exit_order_id is being polled in _fill_poll_loop.
                # Do not trigger a second exit attempt while one is resting.
                if pos.get("pending_exit"):
                    # Recovery: if pending_exit=True but exit_order_id was never
                    # written (crash between setting the guard and placing the
                    # order), reset so the exit_checker can retry.
                    if not pos.get("exit_order_id"):
                        await positions.update_fields(ticker, {"pending_exit": False})
                        log.warning(
                            "Recovered stuck pending_exit for %s "
                            "(pending=True but no exit_order_id — resetting)",
                            ticker,
                        )
                    else:
                        continue

                price_data   = prices.get(ticker)
                stale_price  = (
                    not price_data
                    or price_data.get("ts_us", 0) < stale_cutoff
                )

                # ── Resolution-driven exit (runs even without fresh prices) ──
                # If the DB already knows the outcome and it's against our position,
                # exit immediately rather than waiting for close_time to elapse.
                if db is not None:
                    _outcome = db.get_outcome(ticker)
                    if _outcome is not None:
                        _pos_side = pos.get("side", "")
                        _resolved_against = (
                            (_outcome == "no"  and _pos_side == "yes") or
                            (_outcome == "yes" and _pos_side == "no")
                        )
                        if _resolved_against:
                            _raw = (price_data or {}).get("last_price") or (price_data or {}).get("yes_price")
                            _exit_cents = _raw if _raw else pos["entry_cents"]
                            log.info(
                                "RESOLVED AGAINST: %s outcome=%s — exiting immediately",
                                ticker, _outcome,
                            )
                            _contracts_r = pos.get("contracts_filled") or pos.get("contracts", 1)
                            try:
                                executor._exit_position(
                                    ticker,
                                    {**pos, "contracts": _contracts_r},
                                    _exit_cents,
                                    f"resolved_against ({_outcome})",
                                )
                            except KeyError:
                                log.warning(
                                    "Resolution exit: %s not in executor._positions "
                                    "— closing Redis only",
                                    ticker,
                                )
                            # Resolution is final — remove from executor immediately
                            # rather than waiting for fill poll (market is over).
                            executor._positions.pop(ticker, None)
                            await positions.close(ticker)
                            _s = pos.get("side", "yes")
                            if _s in ("yes", "buy"):
                                _move_r = _exit_cents - pos["entry_cents"]
                            else:
                                _move_r = pos["entry_cents"] - _exit_cents
                            _pnl_r = (_move_r * _contracts_r - cfg.FEE_CENTS * _contracts_r) / 100
                            await bus.publish_execution(ExecutionReport(
                                ticker        = ticker,
                                asset_class   = "kalshi",
                                side          = "no" if _s == "yes" else "yes",
                                contracts     = _contracts_r,
                                fill_price    = _exit_cents / 100,
                                status        = "filled",
                                mode          = "paper" if cfg.PAPER_TRADE else "live",
                                edge_captured = _pnl_r,
                            ))
                            continue

                # ── Post-resolution cleanup (runs even without fresh prices) ──
                close_time_str = pos.get("close_time", "")
                if close_time_str:
                    try:
                        close_dt    = datetime.fromisoformat(
                            close_time_str.replace("Z", "+00:00")
                        )
                        now_utc     = datetime.now(timezone.utc)
                        hours_past  = (now_utc - close_dt).total_seconds() / 3600
                        if hours_past > 2.0:
                            last_cents = (
                                (price_data or {}).get("last_price")
                                or (price_data or {}).get("yes_price")
                            )
                            if not last_cents:
                                log.warning(
                                    "Post-resolution cleanup: %s closed %.1fh ago but no price available — skipping",
                                    ticker, hours_past,
                                )
                                continue
                            log.info(
                                "Post-resolution cleanup: %s closed %.1fh ago — removing",
                                ticker, hours_past,
                            )
                            executor._exit_position(
                                ticker, pos, last_cents,
                                f"market_resolved ({hours_past:.1f}h past close)",
                            )
                            await positions.close(ticker)
                            _s = pos["side"]
                            if _s in ("yes", "buy"):
                                _move = last_cents - pos["entry_cents"]
                            elif _s == "no":
                                _move = pos["entry_cents"] - last_cents
                            else:   # "sell"
                                _move = pos["entry_cents"] - last_cents
                            _cr = pos.get("contracts_filled") or pos.get("contracts", 1)
                            pnl_cents = _move * _cr
                            await bus.publish_execution(ExecutionReport(
                                ticker        = ticker,
                                asset_class   = "kalshi",
                                side          = "no" if pos["side"] == "yes" else "yes",
                                contracts     = _cr,
                                fill_price    = last_cents / 100,
                                status        = "filled",
                                mode          = "paper" if cfg.PAPER_TRADE else "live",
                                edge_captured = (pnl_cents - cfg.FEE_CENTS * _cr) / 100,
                            ))
                            continue
                    except Exception as exc:
                        log.debug("close_time parse for %s: %s", ticker, exc)

                if stale_price:
                    log.debug("No fresh price for %s — skipping exit check.", ticker)
                    metrics.record_stale_price_skip(ticker)
                    continue

                # BTC prices in Redis are raw USD; normalise to cents-per-BTC_UNIT
                # so move_cents and entry_cents are in the same units.
                raw_price = price_data.get("last_price") or price_data.get("yes_price")
                if not raw_price:
                    log.debug("No price available for exit check %s — skipping", ticker)
                    continue
                current_cents = (
                    int(float(raw_price) * BTC_UNIT * 100)
                    if ticker == "BTC-USD"
                    else raw_price
                )
                entry_cents = pos.get("entry_cents")
                if entry_cents is None:
                    log.warning("Missing entry_cents for %s — skipping", ticker)
                    continue
                entry_cents = int(entry_cents)
                side        = pos["side"]
                contracts   = pos.get("contracts_filled") or pos.get("contracts", 1)
                if contracts == 0:
                    continue  # tombstone — skip silently
                asset_class = "btc_spot" if ticker == "BTC-USD" else "kalshi"

                # Guard: back off from tickers where the last exit API call failed,
                # to avoid hammering Kalshi every 60 s on illiquid markets.
                _last_exit_fail = _exit_api_failed.get(ticker, 0)
                if _last_exit_fail and (time.time() - _last_exit_fail) < _EXIT_API_BACKOFF_S:
                    log.debug(
                        "Skipping exit for %s — exit API backoff (%.0fs remaining)",
                        ticker, _EXIT_API_BACKOFF_S - (time.time() - _last_exit_fail),
                    )
                    continue

                # Guard: don't attempt exits for Kalshi limit orders that haven't
                # been confirmed filled yet.  Trying to place a counter-order on
                # Kalshi before we own the position returns HTTP 400.  The
                # fill_poll_loop will set fill_confirmed=True once the order matches,
                # at which point exit checks resume normally.
                #
                # Exception: if contracts_filled > 0 we already own those contracts
                # and must be able to stop-loss out.  In that case exit checks run
                # on the filled portion; the resting remainder is canceled first in
                # the exit execution block below.
                _is_partial_unconfirmed = (
                    asset_class == "kalshi"
                    and not pos.get("fill_confirmed", True)
                )
                if _is_partial_unconfirmed:
                    if int(pos.get("contracts_filled") or 0) == 0:
                        log.debug(
                            "Skipping exit for %s — no fills yet", ticker
                        )
                        continue
                    # contracts was set to contracts_filled at line above — P&L
                    # and all exit conditions are computed on the filled portion.

                # P&L from our perspective (positive = profit):
                #   YES / BTC-buy:  price rose → win
                #   NO:  YES price fell → current NO = (100 - cur) rose → win
                #   BTC-sell (short): price fell → win
                if side in ("yes", "buy"):
                    move_cents = current_cents - entry_cents
                elif side == "no":
                    move_cents = entry_cents - current_cents
                else:   # "sell" — BTC short
                    move_cents = entry_cents - current_cents

                # ── Trailing stop: update high-water PnL mark ─────────────────
                hwm_pnl = pos.get("high_water_pnl", 0)
                if move_cents > hwm_pnl:
                    hwm_pnl = move_cents
                    await positions.update_fields(ticker, {"high_water_pnl": hwm_pnl})

                exit_reason: Optional[str] = None

                # ── Immediate exit flag (arb partial fills, emergency) ────────
                # When an arb leg opened but its partner failed, or an operator
                # sets immediate_exit=True, bypass all P&L thresholds and exit now.
                if pos.get("immediate_exit"):
                    exit_reason = pos.get("immediate_exit_reason", "immediate_exit")

                # ── Near-certain hold: suppress pre-expiry exits ──────────────
                # Only suppress when we're on the WINNING side of a near-certain
                # outcome — let normal exit logic run when we'd be holding a loser.
                #   YES holder + near-certain NO (YES ≤ 8¢) → hold for full payout
                #   NO  holder + near-certain YES (YES ≥ 92¢) → hold for full payout
                # The inverse cases (wrong-side near-certain) should exit ASAP.
                _near_certain_skip = False
                if asset_class == "kalshi":
                    _nc_thresh = KALSHI_NEAR_CERTAIN_THRESHOLD_CENTS
                    if current_cents <= _nc_thresh and side == "no":
                        # Near-certain NO and we hold NO → let it resolve for full $1
                        log.info(
                            "Near-certain NO detected %s: YES price=%d¢ ≤ %d¢"
                            " — holding NO to resolution",
                            ticker, current_cents, _nc_thresh,
                        )
                        _near_certain_skip = True
                    elif current_cents >= (100 - _nc_thresh) and side in ("yes", "buy"):
                        # Near-certain YES and we hold YES → let it resolve for full $1
                        log.info(
                            "Near-certain YES detected %s: YES price=%d¢ ≥ %d¢"
                            " — holding YES to resolution",
                            ticker, current_cents, 100 - _nc_thresh,
                        )
                        _near_certain_skip = True

                # ── Pre-expiry two-tranche exit ───────────────────────────────
                # Tranche 1 at 2× hours_before_close: exit half (protects gains early)
                # Tranche 2 at 1× hours_before_close: exit remainder
                tranche_done = pos.get("tranche_done", 0)
                if close_time_str and not _near_certain_skip:
                    try:
                        close_dt = datetime.fromisoformat(
                            close_time_str.replace("Z", "+00:00")
                        )
                        hours_remaining = (
                            close_dt - datetime.now(timezone.utc)
                        ).total_seconds() / 3600

                        if 0 < hours_remaining < hours_before_close * 2 and tranche_done == 0 and move_cents > 0:
                            if hours_remaining >= hours_before_close and contracts > 1:
                                # TRANCHE 1 — partial exit (half contracts)
                                half      = contracts // 2
                                remaining = contracts - half
                                try:
                                    executor._exit_position(
                                        ticker, {**pos, "contracts": half},
                                        current_cents,
                                        f"pre_expiry_t1 ({hours_remaining:.1f}h)",
                                    )
                                except Exception as _t1_exc:
                                    log.warning("T1 _exit_position failed for %s: %s", ticker, _t1_exc)
                                    continue
                                await positions.update_fields(ticker, {
                                    "contracts":    remaining,
                                    "tranche_done": 1,
                                })
                                pnl_t1 = move_cents * half
                                log.info(
                                    "TRANCHE 1: %s  half=%d  remaining=%d  pnl=%+d¢",
                                    ticker, half, remaining, pnl_t1,
                                )
                                _t1_fee = cfg.FEE_CENTS * half if asset_class == "kalshi" else 0
                                await bus.publish_execution(ExecutionReport(
                                    ticker        = ticker,
                                    asset_class   = asset_class,
                                    side          = "no" if side == "yes" else "yes",
                                    contracts     = half,
                                    fill_price    = current_cents / 100,
                                    status        = "filled",
                                    mode          = "paper" if cfg.PAPER_TRADE else "live",
                                    edge_captured = (pnl_t1 - _t1_fee) / 100,
                                ))
                                continue   # don't trigger full-exit logic
                            else:
                                # Single contract or already past t2 — full exit now
                                exit_reason = f"pre_expiry ({hours_remaining:.1f}h)"

                        elif 0 < hours_remaining < hours_before_close and tranche_done == 1 and move_cents > 0:
                            # TRANCHE 2 — exit remaining contracts
                            exit_reason = f"pre_expiry_t2 ({hours_remaining:.1f}h)"

                    except Exception as exc:
                        log.debug("close_time parse error for %s: %s", ticker, exc)

                # ── BTC max-hold timeout ──────────────────────────────────────
                # Mean reversion that hasn't worked in N hours is likely a trend.
                # Exit to prevent capital being tied up in a dead position.
                # Not treated as a stop-loss — no cooldown penalty applied.
                if exit_reason is None and asset_class == "btc_spot":
                    _max_hold_h = float(os.getenv("BTC_MAX_HOLD_HOURS", "12"))
                    _entered_str = pos.get("entered_at", "")
                    if _entered_str:
                        try:
                            _entered_dt  = datetime.fromisoformat(
                                _entered_str.replace("Z", "+00:00")
                            )
                            _hours_held  = (
                                datetime.now(timezone.utc) - _entered_dt
                            ).total_seconds() / 3600
                            if _hours_held > _max_hold_h:
                                exit_reason = (
                                    f"max_hold ({_hours_held:.1f}h > {_max_hold_h:.0f}h limit)"
                                )
                        except (ValueError, TypeError):
                            pass

                # ── Hours to close (for tiered exit thresholds) ───────────────
                _hours_to_close: float = float("inf")
                if close_time_str and asset_class == "kalshi":
                    try:
                        _htc_dt = datetime.fromisoformat(
                            close_time_str.replace("Z", "+00:00")
                        )
                        _hours_to_close = (
                            _htc_dt - datetime.now(timezone.utc)
                        ).total_seconds() / 3600
                    except Exception:
                        pass

                # ── Trailing stop ─────────────────────────────────────────────
                if exit_reason is None:
                    trailing_stop_cents = _tiered_trailing_stop(trailing_stop_base, _hours_to_close)
                    if (hwm_pnl >= trailing_stop_cents
                            and (hwm_pnl - move_cents) >= trailing_stop_cents):
                        exit_reason = (
                            f"trailing_stop (peak={hwm_pnl}¢, now={move_cents}¢, ts={trailing_stop_cents}¢)"
                        )

                # ── BTC mean-reversion exit: price crossed back through mid-BB ──
                # Scale out in two tranches:
                #   Tranche 1 (mr_tranche_done=0): price reaches mid_bb while in
                #     profit → exit half, move stop to break-even on remainder.
                #   Tranche 2 (mr_tranche_done=1): let remainder run until trailing
                #     stop, take-profit, or stop-loss fires.  Break-even stop replaces
                #     the normal stop-loss to prevent a winner turning into a loser.
                if exit_reason is None and asset_class == "btc_spot":
                    mid_bb_raw   = price_data.get("btc_mid_bb", 0.0)
                    mr_tranche   = pos.get("mr_tranche_done", 0)
                    if mid_bb_raw and float(mid_bb_raw) > 0:
                        mid_bb_cents = int(float(mid_bb_raw) * BTC_UNIT * 100)
                        _mr_hit = (
                            (side == "buy"  and current_cents >= mid_bb_cents and move_cents > 0)
                            or
                            (side == "sell" and current_cents <= mid_bb_cents and move_cents > 0)
                        )
                        if _mr_hit and mr_tranche == 0 and contracts > 1:
                            # Tranche 1: exit half, keep remainder with break-even stop
                            half      = contracts // 2
                            remaining = contracts - half
                            half_size = f"{half * BTC_UNIT:.8f}"
                            log.info(
                                "BTC MR tranche 1: %s  half=%d  remaining=%d  "
                                "pnl_half=%+d¢  break-even stop set at %d¢",
                                ticker, half, remaining, move_cents * half, entry_cents,
                            )
                            if coinbase is not None:
                                try:
                                    _close_side = "SELL" if side == "buy" else "BUY"
                                    await coinbase.create_market_order(ticker, _close_side, half_size)
                                except Exception as _exc:
                                    log.error("BTC MR tranche 1 order failed: %s", _exc)
                            await positions.update_fields(ticker, {
                                "contracts":        remaining,
                                "mr_tranche_done":  1,
                                "mr_breakeven_cents": entry_cents,
                            })
                            await bus.publish_execution(ExecutionReport(
                                ticker        = ticker,
                                asset_class   = "btc_spot",
                                side          = "sell" if side == "buy" else "buy",
                                contracts     = half,
                                fill_price    = current_cents / 100,
                                status        = "filled",
                                mode          = "paper" if cfg.PAPER_TRADE else "live",
                                edge_captured = move_cents * half / 100,
                            ))
                            continue   # don't trigger full-exit logic this cycle
                        elif _mr_hit and (mr_tranche == 1 or contracts == 1):
                            # Tranche 2 or single contract: full exit
                            exit_reason = (
                                f"mean_reversion_t2 (price={current_cents}¢, mid_bb={mid_bb_cents}¢)"
                            )

                    # ── Break-even stop for remainder after tranche 1 ────────
                    if exit_reason is None and mr_tranche == 1:
                        be_cents = pos.get("mr_breakeven_cents", entry_cents)
                        if side == "buy"  and current_cents < be_cents:
                            exit_reason = f"breakeven_stop ({current_cents}¢ < be={be_cents}¢)"
                        elif side == "sell" and current_cents > be_cents:
                            exit_reason = f"breakeven_stop ({current_cents}¢ > be={be_cents}¢)"

                # ── Cut-loss (Intel fundamental reversal) ────────────────────
                if exit_reason is None and ticker in _cutloss_tickers:
                    exit_reason = f"cut_loss_intel (signal_reversed, pnl={move_cents:+d}¢)"

                # ── Take-profit / stop-loss ───────────────────────────────────
                _effective_tp = _tiered_take_profit(take_profit_cents, _hours_to_close)
                if exit_reason is None and move_cents >= _effective_tp:
                    exit_reason = f"take_profit (+{move_cents}¢, tp={_effective_tp}¢)"
                elif exit_reason is None and move_cents <= -stop_loss_cents:
                    # Suppress stop-loss within N days of resolution on Kalshi
                    # contracts — prediction markets resolve to 0 or 100¢, so
                    # a correct directional bet should hold through noise.
                    # Hard cap: never suppress if loss exceeds 2× stop (catastrophic).
                    try:
                        _nd_override = await bus.get_config_override("kalshi_near_expiry_no_stop_days")
                        _near_days = int(_nd_override) if _nd_override else int(os.getenv("KALSHI_NEAR_EXPIRY_NO_STOP_DAYS", "7"))
                    except Exception:
                        _near_days = int(os.getenv("KALSHI_NEAR_EXPIRY_NO_STOP_DAYS", "7"))
                    _suppress = False
                    _catastrophic = move_cents <= -(stop_loss_cents * 2)
                    if close_time_str and asset_class == "kalshi" and not _catastrophic:
                        try:
                            _ct = datetime.fromisoformat(
                                close_time_str.replace("Z", "+00:00")
                            )
                            _days_left = (
                                _ct - datetime.now(timezone.utc)
                            ).total_seconds() / 86400
                            if 0 < _days_left < _near_days:
                                _suppress = True
                                log.info(
                                    "Stop suppressed: %s  %.1fd to resolution "
                                    "(within %dd no-stop zone)  pnl=%+d¢",
                                    ticker, _days_left, _near_days, move_cents,
                                )
                        except (ValueError, TypeError):
                            pass
                    if not _suppress:
                        exit_reason = f"stop_loss ({move_cents}¢)"

                if exit_reason:
                    log.info("Exit triggered: %s  reason=%s  pnl=%+d¢",
                             ticker, exit_reason, move_cents * contracts)
                    if "cut_loss_intel" in exit_reason and ticker.startswith("KXGDP-"):
                        try:
                            _gdp_entry  = pos.get("gdpnow_at_entry")
                            _gdp_exit_r = await bus._r.hget("ep:macro", "gdpnow")
                            _gdp_exit   = float(_gdp_exit_r) if _gdp_exit_r else None
                            log.info(
                                "KXGDP exit: %-36s  gdpnow_entry=%.2f%%  gdpnow_exit=%.2f%%  "
                                "delta=%.2f%%  pnl=%+d¢",
                                ticker,
                                _gdp_entry or 0,
                                _gdp_exit  or 0,
                                (_gdp_exit or 0) - (_gdp_entry or 0),
                                move_cents * contracts,
                            )
                        except Exception:
                            pass

                    # Set stop-loss cooldown BEFORE any awaits.
                    # _signal_consumer runs in the same event loop; if we set the
                    # cooldown after `await positions.close()`, the consumer can
                    # squeeze in between the deletion and the cooldown assignment,
                    # see no cooldown, and immediately re-enter the position —
                    # causing an exit → re-entry → exit loop every 60s.
                    if "stop_loss" in exit_reason or "trailing_stop" in exit_reason:
                        _exit_cooldown[ticker] = time.time()
                        # Escalate cooldown based on repeated stops (persisted in Redis)
                        try:
                            _cnt_key   = f"ep:stopcnt:{_safe_key(ticker)}"
                            _atomic_incr_expire = """
local cnt = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return cnt
"""
                            _stop_cnt = int(await bus._r.eval(_atomic_incr_expire, 1, _cnt_key, _STOP_COUNT_TTL))
                            if _stop_cnt >= 2:
                                _cd_ttl = _COOLDOWN_TIER_3   # 24h from 2nd stop
                            else:
                                _cd_ttl = _COOLDOWN_TIER_1   # 30 min first stop
                            await bus._r.setex(f"ep:cooldown:{_safe_key(ticker)}", _cd_ttl, "stop_loss")
                        except Exception:
                            _stop_cnt, _cd_ttl = 1, _COOLDOWN_SECONDS
                        log.info(
                            "Cooldown set: %s — no re-entry for %ds (stop #%d)",
                            ticker, _cd_ttl, _stop_cnt,
                        )

                    if asset_class == "btc_spot":
                        # Place reverse market order on Coinbase to close spot position
                        close_side = "SELL" if side in ("buy", "yes") else "BUY"
                        size_str   = f"{contracts * BTC_UNIT:.8f}"
                        if coinbase is not None:
                            try:
                                await coinbase.create_market_order(ticker, close_side, size_str)
                            except Exception as _cb_exc:
                                log.error("BTC exit order failed (%s): %s", exit_reason, _cb_exc)
                        else:
                            log.warning("BTC exit triggered but no Coinbase client — position closed in Redis only")
                    else:
                        # Kalshi: place market sell via executor.
                        # _exit_position returns early (without removing from
                        # executor._positions) when the Kalshi API rejects the order.
                        # Detect this by checking membership after the call so we can
                        # retain the Redis position and retry next cycle, rather than
                        # silently dropping it (which would leave an untracked Kalshi
                        # position and cause the divergence handler to loop forever).
                        # Cancel the resting remainder before exiting filled
                        # contracts so the order can't keep filling mid-exit.
                        if _is_partial_unconfirmed:
                            _rem_order = pos.get("order_id", "")
                            if _rem_order and _rem_order != "paper":
                                try:
                                    await asyncio.to_thread(
                                        executor.client._request, "DELETE",
                                        f"/portfolio/orders/{_rem_order}",
                                    )
                                    log.info(
                                        "Canceled resting remainder before "
                                        "partial-fill exit: %s  order=%s",
                                        ticker, _rem_order,
                                    )
                                except Exception as _pc_exc:
                                    log.debug(
                                        "Remainder cancel failed for %s "
                                        "(proceeding with exit): %s",
                                        ticker, _pc_exc,
                                    )

                        # Set pending_exit=True BEFORE placing the order (all
                        # modes).  If exec crashes between this write and the
                        # Kalshi API call, the recovery check above resets it
                        # (no exit_order_id means order was never placed).
                        if not pos.get("pending_exit"):
                            await positions.update_fields(ticker, {"pending_exit": True})

                        _exit_order_id = ""
                        try:
                            # Pass contracts explicitly so _exit_position uses the
                            # right count even when pos["contracts"]=0 (tombstone)
                            # but contracts_filled is set from a prior fill_poll run.
                            _exit_order_id = executor._exit_position(
                                ticker,
                                {**pos, "contracts": contracts},
                                current_cents,
                                exit_reason,
                            )
                        except KeyError:
                            log.warning(
                                "Exit: %s not in executor._positions "
                                "(state divergence — closing Redis entry only)",
                                ticker,
                            )
                            _exit_order_id = "divergence"  # treat as success

                        if not _exit_order_id:
                            # Kalshi API rejected the exit order. Clear the guard
                            # so the exit_checker can retry after backoff.
                            await positions.update_fields(ticker, {"pending_exit": False})
                            _exit_api_failed[ticker] = time.time()
                            log.warning(
                                "Exit order rejected by Kalshi for %s — "
                                "position retained, retrying in %ds",
                                ticker, _EXIT_API_BACKOFF_S,
                            )
                            if "stop_loss" in exit_reason or "trailing_stop" in exit_reason:
                                _exit_cooldown.pop(ticker, None)
                            continue

                        # Paper / forced close: close immediately
                        if _exit_order_id in ("paper", "divergence") or cfg.PAPER_TRADE:
                            await positions.close(ticker)
                        else:
                            # Live exit order placed — don't close yet.
                            # _fill_poll_loop will confirm the fill and close the
                            # Redis entry once the limit order executes.
                            # Also handles TIF escalation if it rests too long.
                            # (pending_exit=True was already written above.)
                            side_for_offer = pos.get("side", "yes")
                            _offer = (current_cents if side_for_offer == "yes"
                                      else (100 - current_cents))
                            await positions.update_fields(ticker, {
                                "exit_order_id":        _exit_order_id,
                                "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                                "exit_offer_cents":     _offer,
                                "exit_reason":          exit_reason,
                                "exit_widen_count":     0,
                            })
                            log.info(
                                "Exit order resting: %s  offer=%d¢  order_id=%.8s",
                                ticker, _offer, _exit_order_id,
                            )
                            continue  # skip positions.close and arb-group exit below

                    # ── Arb-group atomicity: when any leg of a butterfly stops out,
                    # immediately exit all remaining sibling legs sharing the same
                    # arb_id — leaving them open creates naked unhedged directional risk.
                    _exit_arb_id = pos.get("arb_id")
                    if _exit_arb_id:
                        for _sib_ticker, _sib_pos in list(current_positions.items()):
                            if _sib_ticker == ticker:
                                continue
                            if _sib_pos.get("arb_id") != _exit_arb_id:
                                continue
                            _sib_cts = int(
                                _sib_pos.get("contracts_filled")
                                or _sib_pos.get("contracts", 0)
                            )
                            if _sib_cts <= 0:
                                await positions.close(_sib_ticker)
                                continue
                            _sib_pd    = prices.get(_sib_ticker) or {}
                            _sib_cents = _sib_pd.get("yes_price") or 50
                            _sib_rsn   = (
                                f"arb_group_stop (sibling {ticker} hit {exit_reason})"
                            )
                            log.warning(
                                "[ARB-ATOM] Exiting sibling leg %s  arb_id=%s",
                                _sib_ticker, _exit_arb_id,
                            )
                            _sib_oid = ""
                            try:
                                _sib_oid = executor._exit_position(
                                    _sib_ticker,
                                    {**_sib_pos, "contracts": _sib_cts},
                                    _sib_cents,
                                    _sib_rsn,
                                )
                            except KeyError:
                                _sib_oid = "divergence"
                            except Exception as _sib_exc:
                                log.warning(
                                    "ARB-ATOM sibling exit failed for %s: %s",
                                    _sib_ticker, _sib_exc,
                                )
                            if _sib_oid:
                                if _sib_oid in ("paper", "divergence") or cfg.PAPER_TRADE:
                                    executor._positions.pop(_sib_ticker, None)
                                    await positions.close(_sib_ticker)
                                else:
                                    _sib_offer = (
                                        _sib_cents if _sib_pos.get("side", "yes") == "yes"
                                        else (100 - _sib_cents)
                                    )
                                    await positions.update_fields(_sib_ticker, {
                                        "pending_exit":         True,
                                        "exit_order_id":        _sib_oid,
                                        "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                                        "exit_offer_cents":     _sib_offer,
                                        "exit_reason":          _sib_rsn,
                                        "exit_widen_count":     0,
                                    })
                            else:
                                log.warning(
                                    "ARB-ATOM: Kalshi rejected exit for %s — "
                                    "retaining in Redis for retry",
                                    _sib_ticker,
                                )

                    pnl_cents = move_cents * contracts
                    if risk_engine and asset_class == "btc_spot":
                        risk_engine.record_btc_pnl(pnl_cents)

                    _exit_fee = cfg.FEE_CENTS * contracts if asset_class == "kalshi" else 0
                    exit_report = ExecutionReport(
                        ticker        = ticker,
                        asset_class   = asset_class,
                        side          = "no" if side == "yes" else "yes",
                        contracts     = contracts,
                        fill_price    = current_cents / 100,
                        status        = "filled",
                        mode          = "paper" if cfg.PAPER_TRADE else "live",
                        edge_captured = (pnl_cents - _exit_fee) / 100,
                    )
                    await bus.publish_execution(exit_report)

                    await telegram.send_exit(
                        ticker        = ticker,
                        side          = side,
                        contracts     = contracts,
                        current_cents = current_cents,
                        reason        = exit_reason,
                        pnl_cents     = pnl_cents,
                        mode          = "paper" if cfg.PAPER_TRADE else "live",
                    )
                    if db is not None:
                        db.record_trade_outcome(
                            ticker       = ticker,
                            series       = ticker.split("-")[0] if "-" in ticker else ticker,
                            side         = side,
                            contracts    = contracts,
                            entry_cents  = entry_cents,
                            exit_cents   = current_cents,
                            pnl_cents    = pnl_cents,
                            correct      = pnl_cents > 0,
                            model_source = pos.get("model_source", ""),
                        )

                    # Write to Postgres position_history for Kelly calibration
                    _exec_id = pos.get("exec_id", "")
                    if _exec_id:
                        try:
                            _audit_writer().write("position_history", {
                                "entry_exec_id":     _exec_id,
                                "ticker":            ticker,
                                "side":              side,
                                "contracts":         contracts,
                                "entry_cents":       entry_cents,
                                "exit_cents":        current_cents,
                                "realized_pnl_cents": pnl_cents,
                                "exit_reason":       exit_reason,
                                "entered_at":        pos.get("entered_at"),
                                "exited_at":         datetime.now(timezone.utc).isoformat(),
                            })
                        except Exception:
                            pass

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Exit checker error.")


_FILL_POLL_INTERVAL = 10   # seconds between Kalshi order-status sweeps (was 90 — sub-10s fills matter)


async def _fill_poll_loop(
    positions: PositionStore,
    client:    "KalshiClient",
    executor:  "Executor" = None,
    bus:       "RedisBus"  = None,
    db:        "ResolutionDB" = None,
) -> None:
    """
    Async task: periodically poll Kalshi for fill status of resting limit orders.

    Every _FILL_POLL_INTERVAL seconds, scans ep:positions for Kalshi entries that
    have an order_id but no fill_confirmed flag. For each, fetches
    GET /portfolio/orders/{order_id} and records fill info when the exchange
    reports the order as fully or partially filled.

    Also detects canceled orders (e.g. expired GTD) and removes them from Redis
    so the position store stays accurate.
    """
    log.info("Fill poll loop started (interval=%ds)", _FILL_POLL_INTERVAL)

    while True:
        await asyncio.sleep(_FILL_POLL_INTERVAL)
        try:
            all_pos = await positions.get_all()

            # ── Exit TIF escalation: poll resting exit orders ─────────────────
            for ticker, pos in all_pos.items():
                if not pos.get("pending_exit"):
                    continue
                exit_oid = pos.get("exit_order_id", "")
                if not exit_oid or exit_oid == "paper":
                    continue
                try:
                    _e_resp  = await asyncio.to_thread(
                        client.get, f"/portfolio/orders/{exit_oid}"
                    )
                    _e_order = _e_resp.get("order", {})
                    _e_status     = _e_order.get("status", "")
                    _e_fill_count = float(_e_order.get("fill_count_fp", 0) or 0)
                    _e_total      = float(_e_order.get("initial_count_fp", 1) or 1)

                    if _e_status == "filled" or _e_fill_count >= _e_total:
                        executor._positions.pop(ticker, None)
                        await positions.close(ticker)
                        log.info("EXIT FILLED ✓ %s  order_id=%.8s", ticker, exit_oid)

                        # ── Record trade outcome for Kelly calibration ────────────
                        _lf_contracts = int(_e_fill_count or
                                            pos.get("contracts_filled") or
                                            pos.get("contracts", 1))
                        _lf_side      = pos.get("side", "yes")
                        _lf_entry     = int(pos.get("entry_cents", 50))
                        _lf_reason    = pos.get("exit_reason", "live_exit_filled")
                        # Prefer actual fill price from exchange; fall back to offer
                        _lf_price_key = ("yes_price_dollars" if _lf_side == "yes"
                                         else "no_price_dollars")
                        _lf_raw       = _e_order.get(_lf_price_key)
                        _lf_cents     = (int(float(_lf_raw) * 100) if _lf_raw is not None
                                         else int(pos.get("exit_offer_cents", 50)))
                        _lf_move  = (_lf_cents - _lf_entry if _lf_side in ("yes", "buy")
                                     else _lf_entry - _lf_cents)
                        _lf_pnl   = _lf_move * _lf_contracts

                        if db is not None:
                            try:
                                db.record_trade_outcome(
                                    ticker       = ticker,
                                    series       = ticker.split("-")[0] if "-" in ticker else ticker,
                                    side         = _lf_side,
                                    contracts    = _lf_contracts,
                                    entry_cents  = _lf_entry,
                                    exit_cents   = _lf_cents,
                                    pnl_cents    = _lf_pnl,
                                    correct      = _lf_pnl > 0,
                                    model_source = pos.get("model_source", ""),
                                )
                            except Exception as _lf_db_exc:
                                log.warning("fill_poll DB record failed %s: %s",
                                            ticker, _lf_db_exc)

                        _lf_exec_id = pos.get("exec_id", "")
                        if _lf_exec_id:
                            try:
                                _audit_writer().write("position_history", {
                                    "entry_exec_id":      _lf_exec_id,
                                    "ticker":             ticker,
                                    "side":               _lf_side,
                                    "contracts":          _lf_contracts,
                                    "entry_cents":        _lf_entry,
                                    "exit_cents":         _lf_cents,
                                    "realized_pnl_cents": _lf_pnl,
                                    "exit_reason":        _lf_reason,
                                    "entered_at":         pos.get("entered_at"),
                                    "exited_at":          datetime.now(timezone.utc).isoformat(),
                                })
                            except Exception:
                                pass

                        if bus is not None:
                            try:
                                _lf_fee = cfg.FEE_CENTS * _lf_contracts
                                await bus.publish_execution(ExecutionReport(
                                    ticker        = ticker,
                                    asset_class   = pos.get("asset_class", "kalshi"),
                                    side          = "no" if _lf_side == "yes" else "yes",
                                    contracts     = _lf_contracts,
                                    fill_price    = _lf_cents / 100,
                                    status        = "filled",
                                    mode          = "live",
                                    edge_captured = (_lf_pnl - _lf_fee) / 100,
                                ))
                            except Exception as _lf_rpt_exc:
                                log.warning("fill_poll ExecutionReport failed %s: %s",
                                            ticker, _lf_rpt_exc)

                        try:
                            await telegram.send_exit(
                                ticker        = ticker,
                                side          = _lf_side,
                                contracts     = _lf_contracts,
                                current_cents = _lf_cents,
                                reason        = _lf_reason,
                                pnl_cents     = _lf_pnl,
                                mode          = "live",
                            )
                        except Exception as _lf_tg_exc:
                            log.debug("fill_poll telegram failed %s: %s",
                                      ticker, _lf_tg_exc)

                    elif _e_status == "canceled":
                        # Externally canceled — accept whatever filled, or just close
                        executor._positions.pop(ticker, None)
                        await positions.close(ticker)
                        log.warning(
                            "Exit order canceled externally: %s order_id=%.8s "
                            "fill=%d/%d — closing position",
                            ticker, exit_oid, int(_e_fill_count), int(_e_total),
                        )

                    elif _e_status == "resting":
                        widen_count = int(pos.get("exit_widen_count", 0))
                        placed_at_s = pos.get("exit_order_placed_at", "")
                        if not placed_at_s or widen_count >= _EXIT_TIF_MAX_STEPS:
                            continue
                        try:
                            _placed_dt = datetime.fromisoformat(
                                placed_at_s.replace("Z", "+00:00")
                            )
                            _age_min = (
                                datetime.now(timezone.utc) - _placed_dt
                            ).total_seconds() / 60
                        except (ValueError, TypeError):
                            continue

                        if _age_min < _EXIT_TIF_STEP_MINUTES:
                            continue

                        # Escalate: cancel resting limit, place a new one 2¢ more aggressive
                        cur_offer   = int(pos.get("exit_offer_cents", 50))
                        new_offer   = max(1, cur_offer - _EXIT_TIF_WIDEN_CENTS)
                        pos_side    = pos.get("side", "yes")
                        price_field = "yes_price" if pos_side == "yes" else "no_price"
                        _cts        = int(pos.get("contracts_filled") or pos.get("contracts", 1))

                        try:
                            await asyncio.to_thread(
                                client._request, "DELETE",
                                f"/portfolio/orders/{exit_oid}",
                            )
                        except Exception as _tif_del:
                            log.debug("TIF cancel failed for %s: %s", ticker, _tif_del)

                        payload = {
                            "action": "sell", "type": "limit",
                            "ticker": ticker, "side": pos_side,
                            "count": _cts, price_field: new_offer,
                        }
                        try:
                            _new_resp = await asyncio.to_thread(
                                client.post, "/portfolio/orders", payload
                            )
                            _new_oid = _new_resp.get("order", {}).get("order_id", "") or ""
                        except Exception as _tif_exc:
                            log.error("TIF escalation order failed for %s: %s", ticker, _tif_exc)
                            _new_oid = ""

                        if _new_oid:
                            await positions.update_fields(ticker, {
                                "exit_order_id":        _new_oid,
                                "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                                "exit_offer_cents":     new_offer,
                                "exit_widen_count":     widen_count + 1,
                            })
                            log.info(
                                "TIF escalation: %s  offer %d¢→%d¢  step=%d/%d  "
                                "new_order=%.8s",
                                ticker, cur_offer, new_offer,
                                widen_count + 1, _EXIT_TIF_MAX_STEPS, _new_oid,
                            )
                        else:
                            log.warning(
                                "TIF escalation: new order failed for %s  "
                                "holding at offer=%d¢", ticker, cur_offer,
                            )

                except Exception as _ep_exc:
                    log.debug("Exit poll error for %s: %s", ticker, _ep_exc)

            # ── Entry fill polling: confirm resting entry orders ───────────────
            for ticker, pos in all_pos.items():
                order_id = pos.get("order_id", "")
                # Skip: no order_id, paper trades, BTC, already confirmed, pending exit
                if not order_id or order_id == "paper":
                    continue
                if ticker == "BTC-USD":
                    continue
                if pos.get("fill_confirmed"):
                    continue
                if pos.get("pending_exit"):
                    continue

                try:
                    resp  = await asyncio.to_thread(
                        client.get, f"/portfolio/orders/{order_id}"
                    )
                    order = resp.get("order", {})
                    status      = order.get("status", "")
                    fill_count  = float(order.get("fill_count_fp", 0) or 0)
                    total_count = float(order.get("initial_count_fp", 1) or 1)

                    if status == "filled" or fill_count >= total_count:
                        # Fully filled
                        side      = order.get("side", pos.get("side", "yes"))
                        fill_price_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
                        fill_price_raw = order.get(fill_price_key)
                        updates = {
                            "fill_confirmed":   True,
                            "filled_at":        datetime.now(timezone.utc).isoformat(),
                            "contracts_filled": int(fill_count),
                        }
                        if fill_price_raw is not None:
                            updates["fill_price_cents"] = int(float(fill_price_raw) * 100)
                        await positions.update_fields(ticker, updates)
                        # Sync fill status to in-memory executor so state divergence
                        # handler doesn't reset fill_confirmed back to False on the
                        # next signal cycle (executor._positions is never updated by
                        # positions.update_fields — it only writes Redis).
                        if executor is not None and ticker in executor._positions:
                            executor._positions[ticker].update(updates)
                        elif executor is not None:
                            log.warning("fill_poll sync: %s not in executor._positions", ticker)
                        log.info(
                            "FILL CONFIRMED ✓ %s  order_id=%.8s  qty=%d/%d  status=%s",
                            ticker, order_id, int(fill_count), int(total_count), status,
                        )

                    elif status == "canceled" and fill_count > 0:
                        # Partially filled then manually canceled — confirm what
                        # was actually filled rather than removing the position.
                        updates = {
                            "fill_confirmed":   True,
                            "contracts":        int(fill_count),
                            "contracts_filled": int(fill_count),
                        }
                        await positions.update_fields(ticker, updates)
                        if executor is not None and ticker in executor._positions:
                            executor._positions[ticker].update(updates)
                        elif executor is not None:
                            log.warning("fill_poll sync: %s not in executor._positions", ticker)
                        log.info(
                            "PARTIAL FILL CONFIRMED (canceled): %s  filled=%d/%d  order_id=%.8s",
                            ticker, int(fill_count), int(total_count), order_id,
                        )

                    elif fill_count > 0:
                        # Still resting with partial fills — update count, then
                        # check if the order has been waiting too long.
                        updates = {"contracts_filled": int(fill_count)}
                        await positions.update_fields(ticker, updates)
                        if executor is not None and ticker in executor._positions:
                            executor._positions[ticker].update(updates)
                        elif executor is not None:
                            log.warning("fill_poll sync: %s not in executor._positions", ticker)
                        log.info(
                            "PARTIAL FILL: %s  filled=%d/%d  order_id=%.8s",
                            ticker, int(fill_count), int(total_count), order_id,
                        )
                        # Partial fill timeout: if the order has been resting
                        # for longer than _RESTING_ORDER_MAX_HOURS, cancel the
                        # remainder and confirm whatever was actually filled.
                        _pf_entered = pos.get("entered_at", "")
                        if _pf_entered:
                            try:
                                _pf_dt  = datetime.fromisoformat(
                                    _pf_entered.replace("Z", "+00:00")
                                )
                                _pf_age = (
                                    datetime.now(timezone.utc) - _pf_dt
                                ).total_seconds() / 3600
                                if _pf_age > _RESTING_ORDER_MAX_HOURS:
                                    log.warning(
                                        "PARTIAL FILL TIMEOUT: %s  age=%.1fh  "
                                        "filled=%d/%d — canceling remainder",
                                        ticker, _pf_age, int(fill_count), int(total_count),
                                    )
                                    try:
                                        await asyncio.to_thread(
                                            client._request, "DELETE",
                                            f"/portfolio/orders/{order_id}",
                                        )
                                    except Exception as _pf_del:
                                        log.debug(
                                            "Partial fill cancel failed for %s: %s",
                                            ticker, _pf_del,
                                        )
                                    _pf_updates = {
                                        "fill_confirmed":   True,
                                        "contracts":        int(fill_count),
                                        "contracts_filled": int(fill_count),
                                    }
                                    await positions.update_fields(ticker, _pf_updates)
                                    if executor is not None and ticker in executor._positions:
                                        executor._positions[ticker].update(_pf_updates)
                            except (ValueError, TypeError):
                                pass

                    elif status == "canceled":
                        log.warning(
                            "ORDER CANCELED on exchange: %s  order_id=%.8s — removing position",
                            ticker, order_id,
                        )
                        # Pop from in-memory dict first so exit_checker can't fire
                        # in the window between Redis delete and executor pop.
                        if executor is not None:
                            executor._positions.pop(ticker, None)
                        await positions.close(ticker)

                    elif status == "resting" and fill_count == 0:
                        # Check if this resting order has been waiting too long
                        _entered_str = pos.get("entered_at", "")
                        if _entered_str:
                            try:
                                _entered_dt = datetime.fromisoformat(
                                    _entered_str.replace("Z", "+00:00")
                                )
                                _age_h = (
                                    datetime.now(timezone.utc) - _entered_dt
                                ).total_seconds() / 3600
                                if _age_h > _RESTING_ORDER_MAX_HOURS:
                                    log.warning(
                                        "RESTING ORDER TIMEOUT: %s age=%.1fh — auto-canceled",
                                        ticker, _age_h,
                                    )
                                    try:
                                        await asyncio.to_thread(
                                            client._request, "DELETE",
                                            f"/portfolio/orders/{order_id}",
                                        )
                                    except Exception as _del_exc:
                                        log.debug(
                                            "Resting order DELETE failed for %s: %s",
                                            ticker, _del_exc,
                                        )
                                    if executor is not None:
                                        executor._positions.pop(ticker, None)
                                    await positions.close(ticker)
                                    _entry_failed_cooldown[ticker] = time.time()
                            except (ValueError, TypeError):
                                pass

                except Exception as poll_exc:
                    log.debug("Fill poll error for %s: %s", ticker, poll_exc)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Fill poll loop error.")


async def _reconcile_orphan_orders(
    positions: "PositionStore",
    client:    "KalshiClient",
    executor:  "Executor" = None,
) -> None:
    """
    On startup: fetch all resting Kalshi orders and restore any that have no
    matching Redis entry.  Without this, a restart after a partial execution
    (order placed, process killed before Redis write) leaves orphan resting orders
    that cause HTTP 409 on the next entry attempt for the same market.

    Only writes positions with fill_confirmed=False so fill_poll picks them up and
    marks them confirmed (or canceled) on the next sweep.

    Non-fatal: a Kalshi API error here should not prevent startup.
    """
    try:
        resp = await asyncio.to_thread(
            client.get, "/portfolio/orders", {"status": "resting"}
        )
    except Exception as exc:
        log.warning("Orphan reconciliation: Kalshi API call failed (%s) — skipping.", exc)
        return

    orders = resp.get("orders", [])
    if not orders:
        log.info("Orphan reconciliation: no resting orders found.")
        return

    redis_positions = await positions.get_all()
    restored = 0
    for order in orders:
        ticker   = order.get("ticker", "")
        order_id = order.get("order_id", "")
        if not ticker or not order_id:
            continue
        # Skip tombstones (contracts=0) and already-tracked positions
        existing = redis_positions.get(ticker)
        if existing and int(existing.get("contracts", 0)) == 0:
            continue   # intentional block — do not restore
        if existing:
            continue   # already in Redis — fill_poll handles it

        # Don't restore resting orders for tickers currently on stop-loss cooldown.
        # An entry order still resting after a stop exit should be cancelled, not
        # re-adopted — otherwise it will just hit another stop and escalate the
        # stop counter on the next cycle.
        try:
            _cd_ttl = await bus._r.ttl(f"ep:cooldown:{_safe_key(ticker)}")
            if _cd_ttl > 0:
                log.info(
                    "Orphan recovery skipping %s — stop-loss cooldown active (%ds remaining); "
                    "cancelling resting order %s",
                    ticker, _cd_ttl, order_id,
                )
                try:
                    await asyncio.to_thread(
                        client.delete, f"/portfolio/orders/{order_id}"
                    )
                except Exception as _cancel_exc:
                    log.warning("Could not cancel cooldown-blocked order %s: %s", order_id, _cancel_exc)
                continue
        except Exception:
            pass  # Redis unavailable — proceed with restore

        side         = order.get("side", "yes")
        yes_price    = order.get("yes_price_dollars") or order.get("yes_price", 0)
        no_price     = order.get("no_price_dollars")  or order.get("no_price", 0)
        if side == "no":
            entry_cents = int(float(no_price or (1 - float(yes_price or 0.5))) * 100)
            # Store YES price as entry_cents per convention
            entry_cents = 100 - entry_cents
        else:
            entry_cents = int(float(yes_price or 0.5) * 100)
        contracts    = int(float(order.get("initial_count_fp", 1) or 1))

        await positions.open(
            ticker      = ticker,
            side        = side,
            contracts   = contracts,
            entry_cents = entry_cents,
            fair_value  = float(yes_price or 0.5),
            pending     = False,
        )
        await positions.update_fields(ticker, {
            "order_id":      order_id,
            "fill_confirmed": False,
        })
        # Keep executor._positions in sync so exit_checker can find this position
        if executor is not None:
            executor._positions[ticker] = {
                "side":          side,
                "entry_cents":   entry_cents,
                "contracts":     contracts,
                "fair_value":    float(yes_price or 0.5),
                "meeting":       "",
                "outcome":       "",
                "entered_at":    datetime.now(timezone.utc).isoformat(),
                "order_id":      order_id,
            }
        log.warning(
            "ORPHAN ORDER RESTORED: %s  order_id=%.8s  side=%s  contracts=%d  entry=%d¢",
            ticker, order_id, side, contracts, entry_cents,
        )
        restored += 1

    if restored:
        log.warning("Orphan reconciliation complete: %d order(s) restored to Redis.", restored)
    else:
        log.info("Orphan reconciliation: all resting orders already tracked in Redis.")

    # ── Phase 2: reconcile FILLED positions ──────────────────────────────────
    # Resting-order reconciliation only handles unexecuted orders.  If Redis was
    # cleared AFTER a fill was confirmed, those contracts still exist in the
    # Kalshi account but orphan reconciliation misses them (they're no longer
    # "resting").  Fetch the full portfolio positions and restore any filled
    # positions that are missing from Redis.
    try:
        filled_resp = await asyncio.to_thread(client.get, "/portfolio/positions")
    except Exception as exc:
        log.warning("Orphan reconciliation (filled): Kalshi API call failed (%s) — skipping.", exc)
        return

    mkt_positions = filled_resp.get("market_positions", [])
    # Reload Redis after phase 1 may have added entries
    redis_positions = await positions.get_all()
    restored_filled = 0
    for mp in mkt_positions:
        ticker   = mp.get("ticker", "")
        if not ticker:
            continue
        pos_fp   = float(mp.get("position_fp", 0) or 0)
        if pos_fp == 0:
            continue   # no holdings — settled/no-position market

        # Derive side and contract count from signed position_fp
        # Kalshi convention: positive = YES, negative = NO
        if pos_fp > 0:
            side      = "yes"
            contracts = int(pos_fp)
        else:
            side      = "no"
            contracts = int(abs(pos_fp))

        # Skip tombstones and already-tracked positions
        existing = redis_positions.get(ticker)
        if existing is not None:
            continue   # already tracked (resting or filled)

        entry_cents = _kalshi_entry_cents(mp)

        await positions.open(
            ticker      = ticker,
            side        = side,
            contracts   = contracts,
            entry_cents = entry_cents,
            fair_value  = entry_cents / 100.0,
            pending     = False,
        )
        # Mark as fill_confirmed so exit_checker monitors it immediately
        await positions.update_fields(ticker, {
            "order_id":        "",   # no order_id for already-filled positions
            "fill_confirmed":  True,
            "contracts_filled": contracts,
        })
        if executor is not None:
            executor._positions[ticker] = {
                "side":             side,
                "entry_cents":      entry_cents,
                "contracts":        contracts,
                "contracts_filled": contracts,
                "fair_value":       entry_cents / 100.0,
                "meeting":          "",
                "outcome":          "",
                "entered_at":       datetime.now(timezone.utc).isoformat(),
                "order_id":         "",
                "fill_confirmed":   True,
            }
        log.warning(
            "ORPHAN FILLED POSITION RESTORED: %s  side=%s  contracts=%d  entry=%d¢  "
            "(recovered from Kalshi portfolio — was missing from Redis)",
            ticker, side, contracts, entry_cents,
        )
        restored_filled += 1

    if restored_filled:
        log.warning(
            "Orphan reconciliation (filled): %d filled position(s) restored to Redis.",
            restored_filled,
        )
    else:
        log.info("Orphan reconciliation (filled): no missing filled positions detected.")


async def cancel_and_tombstone(
    ticker:   str,
    client:   "KalshiClient",
    positions: "PositionStore",
    executor:  "Executor",
) -> bool:
    """
    Cancel a resting Kalshi order and write a contracts=0 tombstone to Redis.

    A tombstone (contracts=0) permanently blocks re-entry for this ticker:
      - _reconcile_orphan_orders skips restored tombstones
      - _process_signal deduplicates on positions.exists()

    Steps:
      1. Fetch the order_id from Redis ep:positions
      2. DELETE /portfolio/orders/{order_id} on Kalshi (cancel the live order)
      3. Write contracts=0 tombstone via positions.open()
      4. Remove from executor._positions (prevents exit_checker from firing)
      5. Log WARNING so the action is always visible in the log stream

    Returns True on success, False if any step fails.

    Usage (from operator console or a future admin command handler):
        from ep_exec import cancel_and_tombstone
        ok = await cancel_and_tombstone(
            "KXGDP-26APR30-T2.5", client, positions, executor
        )
    """
    try:
        all_pos  = await positions.get_all()
        pos_data = all_pos.get(ticker)
        if not pos_data:
            log.warning("cancel_and_tombstone: %s not found in Redis positions", ticker)
            return False

        order_id = pos_data.get("order_id", "")

        # ── Step 1: Cancel on Kalshi (best-effort — tombstone is written regardless) ──
        if order_id and order_id != "paper":
            try:
                await asyncio.to_thread(
                    client._request, "DELETE", f"/portfolio/orders/{order_id}"
                )
                log.info(
                    "cancel_and_tombstone: DELETE /portfolio/orders/%s OK (%s)",
                    order_id, ticker,
                )
            except Exception as _del_exc:
                # Log but continue — the tombstone still blocks re-entry even if
                # the Kalshi cancel API call failed (e.g. already filled/canceled).
                log.warning(
                    "cancel_and_tombstone: Kalshi cancel failed for %s order=%s: %s "
                    "— writing tombstone anyway",
                    ticker, order_id, _del_exc,
                )
        else:
            log.info(
                "cancel_and_tombstone: %s has no live order_id (%r) — skipping DELETE",
                ticker, order_id,
            )

        # ── Step 2: Write contracts=0 tombstone ──────────────────────────────
        # Remove the existing entry first so positions.open() creates a fresh record.
        await positions.close(ticker)
        await positions.open(
            ticker      = ticker,
            side        = "yes",
            contracts   = 0,
            entry_cents = 0,
            fair_value  = 0,
            pending     = False,
        )

        # ── Step 3: Remove from executor in-memory dict ───────────────────────
        executor._positions.pop(ticker, None)

        log.warning(
            "TOMBSTONED: %s — order canceled and position blocked", ticker
        )
        return True

    except Exception as exc:
        log.error("cancel_and_tombstone: unexpected error for %s: %s", ticker, exc)
        return False


async def _business_health_loop(bus: RedisBus, interval: int = 300) -> None:
    """Check business-logic invariants every 5 min; surface issues via /health."""
    log.info("Business health check started (interval=%ds)", interval)
    while True:
        await asyncio.sleep(interval)
        issues: list = []
        try:
            # Position prices stale?
            # Only flag tickers that the ob_depth/price feed actually tracks.
            # Series markets (NBA, NFL, etc.) have no real-time feed and will
            # never have an ep:prices entry — skip them silently.
            _PRICED_PREFIXES = ("KXFED", "KXCPI", "KXGDP", "KXHIGHCHI",
                                "KXINFLATION", "KXWEATHER", "KXOIL")
            positions = await bus.get_all_positions()
            now = time.time()
            for ticker, pos in positions.items():
                if not ticker.startswith(_PRICED_PREFIXES):
                    continue
                prices = await bus.get_prices([ticker])
                p = prices.get(ticker)
                if p is None:
                    issues.append(f"{ticker}: no price in ep:prices")
                elif (now - p.get("ts_us", 0) / 1_000_000) > 600:
                    issues.append(f"{ticker}: price stale >10min")

            # Intel publishing signals?
            try:
                last = await bus._r.xrevrange("ep:signals", count=1)
                if last:
                    entry_id, fields = last[0]
                    ts_key = b"payload" if b"payload" in fields else "payload"
                    import json as _json
                    sig_payload = _json.loads(fields[ts_key])
                    age_s = now - sig_payload.get("ts_us", 0) / 1_000_000
                    if age_s > 300:
                        issues.append(f"No signals published in {age_s:.0f}s")
            except Exception:
                pass

            # Drawdown halt stuck >36h?
            try:
                halt = await bus._r.hget("ep:config", "HALT_TRADING")
                if halt in (b"1", "1"):
                    halt_info = await bus._r.hgetall("ep:halt")
                    if halt_info:
                        ts_key = b"tripped_at_us" if b"tripped_at_us" in halt_info else "tripped_at_us"
                        tripped_at = int(halt_info.get(ts_key, 0) or 0) / 1_000_000
                        age_h = (now - tripped_at) / 3600
                        if age_h > 36:
                            issues.append(f"Drawdown halt active {age_h:.1f}h (>36h)")
            except Exception:
                pass

            # entry_cents invariant (must be 0–100 for Kalshi)
            for ticker, pos in positions.items():
                if pos.get("asset_class") == "kalshi":
                    ec = pos.get("entry_cents", 0)
                    if not (0 <= ec <= 100):
                        issues.append(f"{ticker}: INVARIANT FAIL entry_cents={ec}")

            # Merge into /health state without overwriting infra keys
            existing = metrics._health.copy()
            existing["business_issues"] = issues
            existing["business_ok"]     = len(issues) == 0
            if issues:
                existing["status"] = "degraded"
                log.warning("Business health: %s", "; ".join(issues))
            metrics.set_health(existing)

        except Exception as exc:
            log.debug("_business_health_loop: %s", exc)


async def _performance_publisher_loop(bus: RedisBus, interval: int = 3600) -> None:
    """
    Publish exec-node performance summary to Redis ep:performance every hour.
    Intel node reads this key to display real trade stats from the exec node's CSV.
    """
    from ep_resolution_db import get_performance_summary
    log.info("Performance publisher started (interval=%ds)", interval)
    while True:
        try:
            perf = await get_performance_summary(days=30)
            if perf["total_trades"] > 0:
                await bus._r.set("ep:performance", json.dumps(perf), ex=90000)
                log.info(
                    "Performance (30d): win_rate=%.1f%% pnl=%+.2f$ trades=%d",
                    perf["win_rate"] * 100,
                    perf["total_pnl_cents"] / 100,
                    perf["total_trades"],
                )
        except Exception as exc:
            log.debug("Performance publisher error (non-fatal): %s", exc)
        await asyncio.sleep(interval)


async def _wait_for_dependencies() -> None:
    """Retry Redis and Postgres until reachable before marking the service ready."""
    import redis.asyncio as _aioredis
    import asyncpg as _asyncpg

    _r = await _aioredis.from_url(REDIS_URL, socket_connect_timeout=3)
    try:
        for attempt in range(30):
            try:
                await _r.ping()
                log.info("Redis ready")
                break
            except Exception as exc:
                log.info("Waiting for Redis (%d/30): %s", attempt + 1, exc)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("Redis unreachable after 60s")
    finally:
        await _r.aclose()

    dsn = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if dsn:
        for attempt in range(15):
            try:
                _pg = await _asyncpg.connect(dsn, timeout=5)
                await _pg.execute("SELECT 1")
                await _pg.close()
                log.info("Postgres ready")
                break
            except Exception as exc:
                log.info("Waiting for Postgres (%d/15): %s", attempt + 1, exc)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("Postgres unreachable after 30s")


async def exec_main() -> None:
    setup_logging(cfg.OUTPUT_DIR / "logs")
    cfg.validate()
    await _wait_for_dependencies()

    mode_label = "PAPER" if cfg.PAPER_TRADE else "LIVE"
    log.info("=" * 60)
    log.info("EdgePulse Exec   node=%s  mode=%s", NODE_ID, mode_label)
    log.info("=" * 60)

    # ── Auth + clients ────────────────────────────────────────────────────────
    auth   = NoAuth() if (cfg.PAPER_TRADE and not cfg.API_KEY_ID) else \
             KalshiAuth(api_key_id=cfg.API_KEY_ID, private_key_path=cfg.PRIVATE_KEY_PATH)
    client = KalshiClient(
        base_url    = cfg.BASE_URL,
        auth        = auth,
        timeout     = cfg.HTTP_TIMEOUT,
        max_retries = cfg.MAX_RETRIES,
        backoff     = cfg.RETRY_BACKOFF,
        concurrency = cfg.CONCURRENCY,
    )

    # ── Redis ─────────────────────────────────────────────────────────────────
    bus = RedisBus(REDIS_URL, NODE_ID)
    await bus.connect()
    await init_audit_writer()
    await bus.publish_system_event("EXEC_START", f"mode={mode_label}")
    sd_notify("READY=1")

    # ── Shared state ──────────────────────────────────────────────────────────
    positions   = PositionStore(bus)
    kalshi_risk = RiskManager(RiskConfig(
        max_contracts        = cfg.MAX_CONTRACTS,
        kelly_fraction       = cfg.KELLY_FRACTION,
        max_market_exposure  = cfg.MAX_MARKET_EXPOSURE,
        max_total_exposure   = cfg.MAX_TOTAL_EXPOSURE,
        daily_drawdown_limit = cfg.DAILY_DRAWDOWN_LIMIT,
        max_spread_cents     = cfg.MAX_SPREAD_CENTS,
        fee_cents            = cfg.FEE_CENTS,
    ))
    risk_engine = UnifiedRiskEngine(kalshi_risk)

    # ── Prometheus metrics (Exec node scrape target) ──────────────────────────
    exec_metrics_port = int(os.getenv("METRICS_PORT", "9092"))
    metrics.start(port=exec_metrics_port)

    # ── Executor (existing Kalshi order placement + CSV log) ──────────────────
    executor = Executor(
        client             = client,
        trades_csv         = cfg.TRADES_CSV,
        paper              = cfg.PAPER_TRADE,
        take_profit_cents  = cfg.TAKE_PROFIT_CENTS,
        stop_loss_cents    = cfg.STOP_LOSS_CENTS,
        hours_before_close = cfg.HOURS_BEFORE_CLOSE,
        state              = None,   # no BotState on Exec — state lives in Redis
    )

    # Sync executor in-memory positions from Redis on startup.
    # Redis is authoritative; the file-backed positions may be stale after a crash.
    redis_positions = await positions.get_all()

    # ── Cleanup: stale pending positions from a prior crash ───────────────────
    # A pending=True entry means Exec wrote the position to Redis but crashed
    # before confirming (or failing) the execute call.  Entries older than 30 min
    # are safe to remove — any real order would have filled or timed out by then.
    _now_utc = datetime.now(timezone.utc)
    _pending_threshold_s = 30 * 60   # 30 minutes
    for _ticker, _pos in list(redis_positions.items()):
        if _pos.get("pending"):
            try:
                _entered = datetime.fromisoformat(
                    _pos.get("entered_at", "").replace("Z", "+00:00")
                )
                _age_s = (_now_utc - _entered).total_seconds()
            except Exception:
                _age_s = _pending_threshold_s + 1   # unknown age → clean up
            if _age_s > _pending_threshold_s:
                await positions.close(_ticker)
                del redis_positions[_ticker]
                log.warning(
                    "Startup cleanup: removed stale pending position %s (age=%.0fs)",
                    _ticker, _age_s,
                )

    # ── Cleanup: ghost positions — confirmed but with no order_id and no fills ──
    # These result from failed order placements (executor added to _positions
    # before the HTTP call that returned 4xx/5xx).  They have pending=False but
    # order_id="" and fill_confirmed=False.  fill_poll skips them (no order_id),
    # exit_checker skips them (not fill_confirmed), so they would live forever.
    # Contracts=0 tombstones are intentional blocks — do NOT remove them.
    for _ticker, _pos in list(redis_positions.items()):
        if (not _pos.get("order_id")
                and not _pos.get("fill_confirmed")
                and not _pos.get("pending")
                and int(_pos.get("contracts", 0)) > 0):
            _entry_failed_cooldown[_ticker] = time.time()
            await positions.close(_ticker)
            del redis_positions[_ticker]
            log.warning(
                "Startup cleanup: removed ghost position %s "
                "(no order_id, not confirmed, contracts=%s)",
                _ticker, _pos.get("contracts"),
            )

    if redis_positions:
        executor._positions = redis_positions
        executor._save_paper_positions()
        log.info("Startup: synced %d positions from Redis → executor", len(redis_positions))
    else:
        log.info("Startup: no Redis positions — executor loaded %d from disk",
                 len(executor._positions))
        # Restore disk positions to Redis so Intel dedup and concentration checks work.
        # Without this, Redis stays empty after restart and every signal is re-evaluated
        # as new — causing a flood of EXECUTOR_DEDUP rejections each cycle.
        if executor._positions:
            for _ticker, _pos in executor._positions.items():
                await positions.open(
                    ticker       = _ticker,
                    side         = _pos.get("side", "yes"),
                    contracts    = int(_pos.get("contracts", 1)),
                    entry_cents  = int(_pos.get("entry_cents", 50)),
                    fair_value   = float(_pos.get("fair_value", 0.5)),
                    meeting      = _pos.get("meeting", ""),
                    outcome      = _pos.get("outcome", ""),
                    close_time   = _pos.get("close_time", ""),
                    model_source = _pos.get("model_source", ""),
                    pending      = False,
                )
            log.info("Startup: synced %d disk positions → Redis", len(executor._positions))

    # ── Orphan order reconciliation ───────────────────────────────────────────
    # Restore any Kalshi resting orders that have no matching Redis entry.
    # Must run after the Redis cleanup above so tombstones are respected.
    if not cfg.PAPER_TRADE:
        await _reconcile_orphan_orders(positions, client, executor)
        # Full position sync: fixes qty/side/entry_cents divergence that
        # can accumulate between reconcile runs.
        await _sync_positions_with_kalshi(positions, executor)
    else:
        log.info("Paper trade mode — skipping orphan order reconciliation.")

    # ── Coinbase client (BTC execution) ──────────────────────────────────────
    coinbase    = CoinbaseTradeClient()
    _cb_usd     = await coinbase.get_usd_balance_cents()
    _btc_spot   = await fetch_btc_spot_usd()
    _cb_total   = await coinbase.get_total_balance_cents(btc_price_usd=_btc_spot)
    log.info(
        "Coinbase connectivity: USD=$%.2f  total=$%.2f  BTC spot=$%.0f",
        (_cb_usd   or 0) / 100,
        (_cb_total or 0) / 100,
        _btc_spot,
    )

    # ── Resolution DB (SQLite — records market outcomes for behavioral module) ─
    db = ResolutionDB()
    db.init()

    try:
        # Five concurrent tasks — none blocks the others
        await asyncio.gather(
            _signal_consumer(bus, positions, risk_engine, executor, coinbase),
            _exit_checker(bus, positions, executor, risk_engine, db, coinbase),
            _heartbeat_loop(bus),
            poll_resolutions_loop(client, bus, db),
            _fill_poll_loop(positions, client, executor, bus, db),
            _performance_publisher_loop(bus),
            _business_health_loop(bus),
            kelly_calib_loop(bus),
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Exec loop cancelled.")
    finally:
        await bus.publish_system_event("EXEC_STOP")
        await bus.close()
        await stop_audit_writer()
        log.info("Exec node shutdown complete.")


if __name__ == "__main__":
    import signal as _signal

    async def _run():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(exec_main())

        def _handle_sigterm():
            log.info("SIGTERM received — initiating graceful shutdown")
            task.cancel()

        loop.add_signal_handler(_signal.SIGTERM, _handle_sigterm)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
