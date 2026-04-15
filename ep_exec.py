"""
ep_exec.py — Exec helpers and main loop (runs on QuantVPS Chicago).

Two concurrent async tasks (no threads needed):
  _signal_consumer — drains ep:signals stream, executes orders
  _exit_checker    — periodic exit checks using Redis price state from Intel

Both run under asyncio.gather() — neither blocks the other.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional

from ep_config import cfg, NODE_ID, REDIS_URL, EXIT_INTERVAL, log
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
from ep_metrics import metrics


def _execute_btc_paper(sig: SignalMessage, contracts: int) -> bool:
    """
    Paper-mode BTC trade: log the fill without placing a real exchange order.
    Phase 2: replace this with Coinbase Advanced API order placement.
    """
    cost_usd = sig.market_price * contracts
    log.info(
        "BTC PAPER %s: %s ×%d @ $%.2f  cost=$%.2f  z=%.2f  RSI=%.1f",
        sig.side.upper(),
        sig.ticker,
        contracts,
        sig.market_price,
        cost_usd,
        sig.btc_z_score or 0.0,
        0.0,   # RSI not stored in signal (indicator lives on Intel)
    )
    return True


async def _process_signal(
    sig:         SignalMessage,
    bus:         RedisBus,
    positions:   PositionStore,
    risk_engine: UnifiedRiskEngine,
    executor:    Executor,
) -> ExecutionReport:
    """
    Run one SignalMessage through:  TTL check → dedup → risk gate → execute.

    Always returns an ExecutionReport — even on rejection — so Intel can
    audit every signal's fate via the ep:executions stream.
    """

    def _rejected(reason: str) -> ExecutionReport:
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
        log.debug("Discarding expired signal %s (age=%dms > ttl=%dms)",
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

    # ── Balance (read from Redis — Intel publishes this each cycle) ───────────
    balance_cents = 100_000   # paper default ($1,000)
    if not cfg.PAPER_TRADE:
        balances  = await bus.get_all_balances()
        intel_bal = next(
            (v for k, v in balances.items() if "intel" in k.lower()),
            None,
        )
        if intel_bal is None:
            return _rejected("BALANCE_UNKNOWN")
        balance_cents = intel_bal.get("balance_cents", 0)

    # ── LLM policy overrides (written each cycle by llm_agent.py → ep:config) ─
    llm_kelly, llm_scale = await asyncio.gather(
        bus.get_config_override("llm_kelly_fraction"),
        bus.get_config_override("llm_scale_factor"),
    )

    # ── Kelly sizing ──────────────────────────────────────────────────────────
    # Apply llm_kelly_fraction if the LLM has set one. asyncio is cooperative —
    # no await occurs between the override and restore, so this is race-free.
    orig_kelly = risk_engine._kalshi.cfg.kelly_fraction
    if llm_kelly:
        risk_engine._kalshi.cfg.kelly_fraction = float(llm_kelly)
    contracts = risk_engine.size(sig, balance_cents)
    risk_engine._kalshi.cfg.kelly_fraction = orig_kelly   # always restore

    # Apply LLM scale factor after Kelly sizing (0.5 = half size, 1.5 = +50%)
    if llm_scale and contracts > 0:
        contracts = max(1, int(contracts * float(llm_scale)))

    if contracts <= 0:
        return _rejected("RISK_GATE_SIZE")

    # ── Risk approval ─────────────────────────────────────────────────────────
    open_exposure       = await positions.total_exposure_cents()
    approved, reason    = risk_engine.approve(sig, contracts, balance_cents, open_exposure)
    if not approved:
        log.info("Risk rejected %s: %s", sig.ticker, reason)
        return _rejected(reason or "RISK_GATE_KALSHI")

    # ── Execute (route by asset class) ───────────────────────────────────────
    if sig.asset_class == "btc_spot":
        executed = _execute_btc_paper(sig, contracts)
    elif sig.asset_class == "kalshi":
        exec_signal           = message_to_kalshi_signal(sig)
        exec_signal.contracts = contracts
        executed              = executor.execute(exec_signal)
    else:
        return _rejected("UNKNOWN_ASSET_CLASS")

    if not executed:
        return _rejected("HTTP_ERROR")

    # ── Write primary position to Redis ──────────────────────────────────────
    await positions.open(
        ticker      = sig.ticker,
        side        = sig.side,
        contracts   = contracts,
        entry_cents = int(sig.market_price * 100),
        fair_value  = sig.fair_value,
        meeting     = sig.meeting or "",
        outcome     = sig.outcome or "",
        close_time  = sig.close_time or "",
    )

    cost_cents = int(sig.market_price * 100) * contracts
    fee_cents  = int(cost_cents * cfg.FEE_CENTS / 100) if sig.asset_class == "kalshi" else 0

    metrics.signal_published(sig.asset_class, sig.strategy or "unknown", sig.side)
    log.info("Executed: %s %s ×%d @ %.4f  cost=$%.2f",
             sig.ticker, sig.side, contracts, sig.market_price, cost_cents / 100)

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
            partner_ok = executor.execute(partner_sig)
            if partner_ok:
                await positions.open(
                    ticker      = sig.arb_partner,
                    side        = "no",
                    contracts   = contracts,
                    entry_cents = int(partner_price * 100),
                    fair_value  = 1.0 - sig.fair_value,
                    meeting     = sig.meeting or "",
                    outcome     = sig.outcome or "",
                    close_time  = sig.close_time or "",
                )
                cost_cents += int(partner_price * 100) * contracts
                log.info("ARB partner: %s NO ×%d @ %.4f  (pair: %s)",
                         sig.arb_partner, contracts, partner_price, sig.ticker)
            else:
                log.warning(
                    "ARB partner leg FAILED for %s — primary %s is now an unhedged leg",
                    sig.arb_partner, sig.ticker,
                )
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
        edge_captured = sig.edge,
    )


async def _signal_consumer(
    bus:         RedisBus,
    positions:   PositionStore,
    risk_engine: UnifiedRiskEngine,
    executor:    Executor,
) -> None:
    """
    Async task: drain ep:signals continuously via XREADGROUP.
    Runs independently of the exit-check task — asyncio.gather handles both.
    """
    consumer_name = f"{NODE_ID}-c1"
    log.info("Signal consumer started (consumer=%s)", consumer_name)

    async for entry_id, sig in bus.consume_signals(consumer_name):
        # Clear per-signal held set — Redis ep:positions is the session dedup;
        # _held prevents duplicate entry within a single atomic operation only.
        executor.reset_cycle()
        try:
            report = await _process_signal(sig, bus, positions, risk_engine, executor)
            await bus.publish_execution(report)
        except Exception:
            log.exception("Unhandled error processing %s", sig.ticker)
        finally:
            # Always ack — failed signals become audit entries, not retry storms
            await bus.ack_signal(entry_id)


async def _exit_checker(
    bus:         RedisBus,
    positions:   PositionStore,
    executor:    Executor,
    risk_engine: UnifiedRiskEngine = None,
) -> None:
    """
    Async task: check open positions for take-profit / stop-loss every
    EXIT_INTERVAL seconds, using prices published by Intel to Redis.
    Runs independently of _signal_consumer.
    """
    log.info("Exit checker started (interval=%ds)", EXIT_INTERVAL)

    while True:
        await asyncio.sleep(EXIT_INTERVAL)

        try:
            current_positions = await positions.get_all()
            if not current_positions:
                continue

            tickers     = list(current_positions.keys())
            prices      = await bus.get_prices(tickers)
            stale_cutoff = int(time.time() * 1_000_000) - 300 * 1_000_000   # 5 min

            for ticker, pos in current_positions.items():
                price_data = prices.get(ticker)
                if not price_data:
                    log.debug("No price data for %s — skipping exit check.", ticker)
                    continue
                if price_data.get("ts_us", 0) < stale_cutoff:
                    log.debug("Stale price for %s — skipping exit check.", ticker)
                    continue

                current_cents = price_data.get("last_price") or price_data.get("yes_price", 50)
                entry_cents   = pos["entry_cents"]
                side          = pos["side"]
                contracts     = pos["contracts"]

                move_cents = (current_cents - entry_cents) if side == "yes" \
                             else (entry_cents - current_cents)

                exit_reason: Optional[str] = None

                # ── Pre-expiry check ──────────────────────────────────────────
                close_time_str = pos.get("close_time", "")
                if close_time_str:
                    try:
                        close_dt = datetime.fromisoformat(
                            close_time_str.replace("Z", "+00:00")
                        )
                        hours_remaining = (
                            close_dt - datetime.now(timezone.utc)
                        ).total_seconds() / 3600
                        if 0 < hours_remaining < cfg.HOURS_BEFORE_CLOSE:
                            exit_reason = (
                                f"pre_expiry ({hours_remaining:.1f}h remaining)"
                            )
                    except Exception as exc:
                        log.debug("close_time parse error for %s: %s", ticker, exc)

                if exit_reason is None and move_cents >= cfg.TAKE_PROFIT_CENTS:
                    exit_reason = f"take_profit (+{move_cents}¢)"
                elif exit_reason is None and move_cents <= -cfg.STOP_LOSS_CENTS:
                    exit_reason = f"stop_loss ({move_cents}¢)"

                if exit_reason:
                    log.info("Exit triggered: %s  reason=%s  pnl=%+d¢",
                             ticker, exit_reason, move_cents * contracts)
                    executor._exit_position(ticker, pos, current_cents, exit_reason)
                    await positions.close(ticker)

                    # Infer asset_class from position (BTC positions have no meeting)
                    asset_class = (
                        "btc_spot" if ticker == "BTC-USD" else "kalshi"
                    )
                    pnl_cents = move_cents * contracts
                    if risk_engine and asset_class == "btc_spot":
                        risk_engine.record_btc_pnl(pnl_cents)

                    exit_report = ExecutionReport(
                        ticker        = ticker,
                        asset_class   = asset_class,
                        side          = "no" if side == "yes" else "yes",
                        contracts     = contracts,
                        fill_price    = current_cents / 100,
                        status        = "filled",
                        mode          = "paper" if cfg.PAPER_TRADE else "live",
                        edge_captured = move_cents / 100,
                    )
                    await bus.publish_execution(exit_report)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Exit checker error.")


async def exec_main() -> None:
    setup_logging(cfg.OUTPUT_DIR / "logs")

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
    await bus.publish_system_event("EXEC_START", f"mode={mode_label}")

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
    if redis_positions:
        executor._positions = redis_positions
        executor._save_paper_positions()
        log.info("Startup: synced %d positions from Redis → executor", len(redis_positions))
    else:
        log.info("Startup: no Redis positions — executor loaded %d from disk",
                 len(executor._positions))

    try:
        # Two concurrent tasks — neither blocks the other
        await asyncio.gather(
            _signal_consumer(bus, positions, risk_engine, executor),
            _exit_checker(bus, positions, executor, risk_engine),
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Exec loop cancelled.")
    finally:
        await bus.publish_system_event("EXEC_STOP")
        await bus.close()
        log.info("Exec node shutdown complete.")
