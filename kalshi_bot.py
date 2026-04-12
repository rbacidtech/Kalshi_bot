"""
kalshi_bot.py — FOMC Fed Funds trading bot.

Now includes:
  - Shared BotState (single source of truth)
  - WebSocket real-time price feed (replaces REST polling for prices)
  - Multi-meeting arbitrage scanner
  - Alert system (SMS/email)
  - Live browser dashboard at http://localhost:5050

Run:
    python kalshi_bot.py
    # Then open http://localhost:5050 in your browser
"""

import os
import datetime
from datetime import timezone
import logging
import time

import requests

import kalshi_bot.config as cfg
from kalshi_bot.auth      import KalshiAuth, NoAuth
from kalshi_bot.client    import KalshiClient
from kalshi_bot.state     import BotState, PositionState, TradeEvent
from kalshi_bot.websocket import KalshiWebSocket
from kalshi_bot.strategy  import fetch_signals, scan_all_markets
from kalshi_bot.executor  import Executor
from kalshi_bot.risk      import RiskManager, RiskConfig
from kalshi_bot.logger    import setup_logging, DailySummary, CycleTimer
from kalshi_bot.alerts    import AlertManager
from kalshi_bot.dashboard import start_dashboard
from kalshi_bot.models.backtester import LiveBacktester
from kalshi_bot.models    import arb
from kalshi_bot.models    import fomc as _fomc_mod
from kalshi_bot           import portfolio

log = logging.getLogger("kalshi_bot.main")


def build_auth():
    if cfg.PAPER_TRADE and not cfg.API_KEY_ID:
        return NoAuth()
    return KalshiAuth(api_key_id=cfg.API_KEY_ID,
                      private_key_path=cfg.PRIVATE_KEY_PATH)


def build_client(auth) -> KalshiClient:
    return KalshiClient(
        base_url    = cfg.BASE_URL,
        auth        = auth,
        timeout     = cfg.HTTP_TIMEOUT,
        max_retries = cfg.MAX_RETRIES,
        backoff     = cfg.RETRY_BACKOFF,
        concurrency = cfg.CONCURRENCY,
    )


def build_risk() -> RiskManager:
    return RiskManager(RiskConfig(
        max_contracts        = cfg.MAX_CONTRACTS,
        kelly_fraction       = cfg.KELLY_FRACTION,
        max_market_exposure  = cfg.MAX_MARKET_EXPOSURE,
        max_total_exposure   = cfg.MAX_TOTAL_EXPOSURE,
        daily_drawdown_limit = cfg.DAILY_DRAWDOWN_LIMIT,
        max_spread_cents     = cfg.MAX_SPREAD_CENTS,
        fee_cents            = cfg.FEE_CENTS,
    ))


def run():
    setup_logging(cfg.OUTPUT_DIR / "logs")
    cfg.validate()

    mode = "PAPER" if cfg.PAPER_TRADE else "LIVE"
    log.info("=" * 60)
    log.info("Kalshi FOMC Bot  mode=%s  edge=%.2f  min_conf=%.2f",
             mode, cfg.EDGE_THRESHOLD, cfg.MIN_CONFIDENCE)
    log.info("=" * 60)

    auth   = build_auth()
    client = build_client(auth)
    risk   = build_risk()
    state  = BotState()
    state.mode = "paper" if cfg.PAPER_TRADE else "live"

    executor = Executor(
        client             = client,
        trades_csv         = cfg.TRADES_CSV,
        paper              = cfg.PAPER_TRADE,
        take_profit_cents  = cfg.TAKE_PROFIT_CENTS,
        stop_loss_cents    = cfg.STOP_LOSS_CENTS,
        hours_before_close = cfg.HOURS_BEFORE_CLOSE,
        state              = state,
    )

    # WebSocket — real-time price feed
    ws = KalshiWebSocket(state=state, auth=auth, paper=cfg.PAPER_TRADE)
    ws.start()

    # Dashboard — open http://localhost:5050
    dashboard_port = int(os.getenv("KALSHI_DASHBOARD_PORT", "5050"))
    if os.getenv("KALSHI_DASHBOARD", "true").lower() == "true":
        start_dashboard(state, port=dashboard_port)
        log.info("Dashboard: http://localhost:%d", dashboard_port)

    # Alerts
    alerts = AlertManager(
        state             = state,
        twilio_sid        = os.getenv("TWILIO_ACCOUNT_SID"),
        twilio_token      = os.getenv("TWILIO_AUTH_TOKEN"),
        twilio_from       = os.getenv("TWILIO_FROM_NUMBER"),
        alert_to_phone    = os.getenv("ALERT_PHONE_NUMBER"),
        smtp_host         = os.getenv("ALERT_SMTP_HOST"),
        smtp_port         = int(os.getenv("ALERT_SMTP_PORT", "587")),
        smtp_user         = os.getenv("ALERT_SMTP_USER"),
        smtp_password     = os.getenv("ALERT_SMTP_PASSWORD"),
        alert_from_email  = os.getenv("ALERT_FROM_EMAIL"),
        alert_to_email    = os.getenv("ALERT_TO_EMAIL"),
        min_edge_cents    = cfg.EDGE_THRESHOLD * 100,
    )

    summary    = DailySummary()
    backtester = LiveBacktester(cfg.TRADES_CSV, fee_cents=cfg.FEE_CENTS)

    cycle = 0
    try:
        while True:
            cycle += 1
            summary.record_cycle()
            state.record_cycle()

            with CycleTimer(cycle):

                # Balance
                balance_cents = 0
                try:
                    bal           = client.get("/portfolio/balance")
                    balance_cents = bal.get("balance", 0)
                    risk.set_balance(balance_cents)
                    state.set_balance(balance_cents)
                except Exception:
                    log.warning("Balance fetch failed.")

                portfolio.print_summary(client, balance_cents=balance_cents)
                executor.reset_cycle()

                # Scan markets + register with WebSocket
                markets = []
                try:
                    markets = scan_all_markets(client)
                    ws.subscribe_tickers([m["ticker"] for m in markets])
                    for m in markets:
                        state.update_market(
                            m["ticker"],
                            title      = m.get("title", ""),
                            last_price = int(float(m.get("last_price_dollars") or m.get("yes_bid_dollars") or "0.50") * 100),
                        )
                except Exception:
                    log.exception("Market scan failed.")

                # FedWatch signals
                signals = []
                try:
                    signals = fetch_signals(
                        client,
                        edge_threshold  = cfg.EDGE_THRESHOLD,
                        fred_api_key    = os.getenv("FRED_API_KEY", "1f665e6cab7f604a5c4a9092c90ca0c1"),
                        current_rate    = 3.75,
                        enable_fomc     = True,
                        enable_weather  = True,
                        enable_economic = True,
                        enable_sports   = True,
                        max_contracts  = cfg.MAX_CONTRACTS,
                        min_confidence = cfg.MIN_CONFIDENCE,
                    )
                    state.set_signals([{
                        "ticker":       s.ticker,
                        "title":        s.title,
                        "meeting":      s.meeting,
                        "outcome":      s.outcome,
                        "side":         s.side,
                        "fair_value":   s.fair_value,
                        "market_price": s.market_price,
                        "edge":         s.edge,
                        "contracts":    s.contracts,
                        "confidence":   s.confidence,
                        "model_source": s.model_source,
                        "spread_cents": s.spread_cents,
                    } for s in signals])
                    for s in signals:
                        state.update_fair_value(
                            s.ticker, s.fair_value, s.edge, s.confidence
                        )
                except requests.ConnectionError:
                    log.warning("Network error.")
                except Exception:
                    log.exception("Signal fetch failed.")

                # Arbitrage scan (enable with KALSHI_ARB_ENABLED=true in .env)
                if os.getenv("KALSHI_ARB_ENABLED", "false").lower() == "true":
                    try:
                        arb_signals = arb.detect_arb_signals(
                            markets        = markets,
                            fomc_probs     = _fomc_mod._meeting_probs if _fomc_mod._meeting_probs else {},
                            min_edge_cents = cfg.EDGE_THRESHOLD * 100,
                        )
                        for asig in arb_signals:
                            log.info("[ARB] %s  side=%s  edge=%.1f¢  %s",
                                     asig.ticker, asig.side,
                                     asig.edge_cents, asig.description)
                            # To activate arb execution: set KALSHI_ARB_ENABLED=true
                            # after validating signals in paper mode
                    except Exception as exc:
                        log.debug('Arb scan error: %s', exc)

                # Exposure estimate
                open_exposure = 0
                try:
                    pos_data  = client.get("/portfolio/positions", params={"limit": 100})
                    positions = pos_data.get("market_positions", [])
                    # Use actual market price per contract rather than flat 50¢ proxy
                    for p in positions:
                        tk    = p.get("market_ticker", "")
                        qty   = abs(p.get("position", 0))
                        mkt   = state.markets.get(tk)
                        price = (mkt.last_price if mkt else 50)  # cents
                        open_exposure += qty * price
                    state.update_unrealized_pnl()
                except Exception as exc:
                    log.debug('Exposure fetch error: %s', exc)

                # Exit check
                try:
                    executor.check_exits(markets, signals)
                except Exception:
                    log.warning("Exit check failed.")

                # Execute signals
                for signal in signals:
                    contracts = risk.size(
                        edge          = signal.edge,
                        market_price  = signal.market_price,
                        balance_cents = balance_cents,
                        confidence    = signal.confidence,
                    )
                    if contracts == 0:
                        summary.record(signal, executed=False)
                        continue

                    signal.contracts = contracts

                    if risk.approve(
                        ticker              = signal.ticker,
                        contracts           = contracts,
                        market_price        = signal.market_price,
                        balance_cents       = balance_cents,
                        open_exposure_cents = open_exposure,
                        spread_cents        = signal.spread_cents,
                    ):
                        executed = executor.execute(signal)
                        summary.record(signal, executed=executed)
                        if executed:
                            open_exposure += int(signal.market_price * 100) * contracts
                            state.open_position(PositionState(
                                ticker      = signal.ticker,
                                side        = signal.side,
                                contracts   = contracts,
                                entry_cents = int(signal.market_price * 100),
                                entry_time  = datetime.datetime.now(timezone.utc),
                                fair_value  = signal.fair_value,
                            ))
                            state.add_trade(TradeEvent(
                                timestamp = datetime.datetime.now(timezone.utc),
                                ticker    = signal.ticker,
                                action    = "entry",
                                side      = signal.side,
                                contracts = contracts,
                                price     = int(signal.market_price * 100),
                                edge      = signal.edge,
                                mode      = state.mode,
                            ))
                    else:
                        summary.record(signal, executed=False)

            log.info("Sleeping %ds...\n", cfg.POLL_INTERVAL)
            time.sleep(cfg.POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        ws.stop()
        summary.print_summary()
        alerts.send_daily_summary()
        log.info("--- Backtest report ---")
        backtester.print_report()


if __name__ == "__main__":
    run()
