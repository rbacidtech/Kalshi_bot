"""
ep_intel.py — Intel main loop (runs on DO Droplet NYC3).

Responsibilities:
  1. Maintain WebSocket price feed → BotState
  2. Every POLL_INTERVAL seconds:
     a. Publish price snapshot to Redis  (Exec uses this for exit checks)
     b. Call fetch_signals_async() directly  (no asyncio.run() wrapper)
     c. Filter out tickers already held in Redis positions
     d. Publish new SignalMessages to ep:signals
     e. Drain execution reports → update stats / dashboard
"""

import asyncio
import json
import os
import time
from typing import List, Optional

from ep_config import cfg, NODE_ID, REDIS_URL, EP_PRICES, log
from kalshi_bot.auth      import KalshiAuth, NoAuth
from kalshi_bot.client    import KalshiClient
from kalshi_bot.state     import BotState
from kalshi_bot.websocket import KalshiWebSocket
from kalshi_bot.strategy  import fetch_signals_async, scan_all_markets, Signal
from kalshi_bot.logger    import setup_logging, DailySummary
from ep_schema import PriceSnapshot, SignalMessage
from ep_bus import RedisBus
from ep_adapters import kalshi_signal_to_message
from ep_btc import BTCMeanReversionStrategy
from ep_metrics import metrics


async def intel_main() -> None:
    setup_logging(cfg.OUTPUT_DIR / "logs")
    cfg.validate()

    mode_label = "PAPER" if cfg.PAPER_TRADE else "LIVE"
    log.info("=" * 60)
    log.info("EdgePulse Intel  node=%s  mode=%s", NODE_ID, mode_label)
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

    # ── Shared in-process state (dashboard + WebSocket) ───────────────────────
    state      = BotState()
    state.mode = "paper" if cfg.PAPER_TRADE else "live"

    # ── Redis ─────────────────────────────────────────────────────────────────
    bus = RedisBus(REDIS_URL, NODE_ID)
    await bus.connect()
    await bus.publish_system_event("INTEL_START", f"mode={mode_label}")

    # ── BTC mean-reversion strategy (only if Polygon API key is configured) ───
    polygon_key = os.getenv("POLYGON_API_KEY", "")
    btc_strategy: Optional[BTCMeanReversionStrategy] = (
        BTCMeanReversionStrategy(
            polygon_api_key = polygon_key,
            source_node     = NODE_ID,
        )
        if polygon_key
        else None
    )
    if btc_strategy:
        log.info("BTC mean-reversion enabled (Polygon key configured).")
    else:
        log.info("BTC mean-reversion disabled — set POLYGON_API_KEY in .env to enable.")

    # ── Prometheus metrics server ─────────────────────────────────────────────
    metrics_port = int(os.getenv("METRICS_PORT", "9091"))
    metrics.start(port=metrics_port)

    # ── WebSocket price feed (daemon thread — does NOT use asyncio) ───────────
    # WS endpoint follows BASE_URL, not PAPER_TRADE — the two are independent:
    #   PAPER_TRADE=true  → simulates orders (no real money at risk)
    #   BASE_URL=live     → always reads real market data via live WebSocket
    # If BASE_URL is the demo endpoint the WS also uses demo; otherwise live.
    ws_paper = "demo" in cfg.BASE_URL
    ws = KalshiWebSocket(state=state, auth=auth, paper=ws_paper)
    ws.start()

    # Dashboard runs as a separate Streamlit process (start.sh screen dash).

    summary:           DailySummary   = DailySummary()
    markets_cache:     List[dict]     = []
    markets_last_scan: float          = 0.0
    intel_consumer     = f"{NODE_ID}-intel"

    try:
        while True:
            cycle_start = time.monotonic()
            state.record_cycle()
            summary.record_cycle()

            # ── Check ops halt flag ───────────────────────────────────────────
            if await bus.is_halted():
                log.warning("HALT_TRADING flag set in Redis — sleeping 60s.")
                await asyncio.sleep(60)
                continue

            # ── Balance ───────────────────────────────────────────────────────
            balance_cents = 100_000   # paper default ($1,000)
            if not cfg.PAPER_TRADE:
                try:
                    bal           = client.get("/portfolio/balance")
                    balance_cents = bal.get("balance", 0)
                except Exception:
                    log.warning("Balance fetch failed — using last known value.")
            state.set_balance(balance_cents)
            await bus.set_balance(balance_cents, state.mode)
            metrics.update_balance(balance_cents)

            # ── Market cache (full rescan every 20 min) ───────────────────────
            if time.monotonic() - markets_last_scan > 1200:
                try:
                    markets_cache     = scan_all_markets(client)
                    markets_last_scan = time.monotonic()
                    ws.subscribe_tickers([m["ticker"] for m in markets_cache])
                    log.info("Market rescan: %d markets", len(markets_cache))
                except Exception:
                    log.exception("Market scan failed.")

            # ── Publish price snapshot to Redis (Exec uses this for exits) ────
            snapshot = PriceSnapshot(source_node=NODE_ID)
            with state._lock:
                for ticker, mkt in state.markets.items():
                    snapshot.prices[ticker] = {
                        "yes_price":  mkt.yes_price,
                        "no_price":   mkt.no_price,
                        "spread":     mkt.spread,
                        "last_price": mkt.last_price,
                    }
            await bus.publish_prices(snapshot)

            # ── Signal generation ─────────────────────────────────────────────
            # Direct await — intel_main() IS the running event loop;
            # no asyncio.run() wrapper needed (or allowed) here.
            signals: List[Signal] = []
            try:
                signals = await fetch_signals_async(
                    client          = client,
                    edge_threshold  = cfg.EDGE_THRESHOLD,
                    max_contracts   = cfg.MAX_CONTRACTS,
                    min_confidence  = cfg.MIN_CONFIDENCE,
                    fred_api_key    = os.getenv("FRED_API_KEY", ""),
                    current_rate    = float(os.getenv("CURRENT_FED_RATE", "3.75")),
                    enable_fomc     = True,
                    enable_weather  = os.getenv("ENABLE_WEATHER", "true") == "true",
                    enable_economic = os.getenv("ENABLE_ECONOMIC", "true") == "true",
                    enable_sports   = os.getenv("ENABLE_SPORTS", "true") == "true",
                    markets_cache   = markets_cache,
                )
                state.set_signals([{
                    "ticker":       s.ticker,       "side":        s.side,
                    "fair_value":   s.fair_value,   "market_price": s.market_price,
                    "edge":         s.edge,         "confidence":  s.confidence,
                    "contracts":    s.contracts,    "model_source": s.model_source,
                    "spread_cents": s.spread_cents,
                } for s in signals])
                for s in signals:
                    state.update_fair_value(s.ticker, s.fair_value, s.edge, s.confidence)
            except Exception:
                log.exception("Signal generation failed.")

            # ── Dedup: skip tickers already held in Redis positions ───────────
            current_positions = await bus.get_all_positions()
            new_signals = [s for s in signals if s.ticker not in current_positions]

            # ── BTC mean-reversion signals ────────────────────────────────────
            if btc_strategy:
                # Read LLM policy overrides from Redis and apply to strategy
                rsi_os_str = await bus.get_config_override("llm_rsi_oversold")
                rsi_ob_str = await bus.get_config_override("llm_rsi_overbought")
                z_str      = await bus.get_config_override("llm_z_threshold")
                if rsi_os_str:
                    btc_strategy.rsi_os  = float(rsi_os_str)
                if rsi_ob_str:
                    btc_strategy.rsi_ob  = float(rsi_ob_str)
                if z_str:
                    btc_strategy.z_thresh = float(z_str)

                try:
                    btc_msgs: List[SignalMessage] = await btc_strategy.generate()
                    # Dedup: skip BTC-USD if already held
                    new_btc = [m for m in btc_msgs if m.ticker not in current_positions]

                    # Publish BTC price + indicators to Redis for Exec exit checks
                    if btc_strategy.last_spot:
                        await bus._r.hset(EP_PRICES, "BTC-USD", json.dumps({
                            "last_price":  btc_strategy.last_spot,
                            "yes_price":   btc_strategy.last_spot,
                            "no_price":    btc_strategy.last_spot,
                            "spread":      0,
                            "btc_z_score": btc_strategy.last_z or 0.0,
                            "btc_rsi":     btc_strategy.last_rsi or 50.0,
                            "ts_us":       int(time.time() * 1_000_000),
                        }))
                        # Rolling history for dashboard price chart
                        await bus.push_btc_history(
                            btc_strategy.last_spot,
                            btc_strategy.last_rsi,
                            btc_strategy.last_z,
                        )
                        metrics.update_btc(
                            price = btc_strategy.last_spot,
                            rsi   = btc_strategy.last_rsi,
                            z     = btc_strategy.last_z,
                        )

                    # Publish BTC signals directly (already SignalMessage objects)
                    for msg in new_btc:
                        try:
                            await bus.publish_signal(msg)
                            metrics.signal_published(msg.asset_class, msg.strategy, msg.side)
                        except Exception as exc:
                            log.warning("Failed to publish BTC signal: %s", exc)

                    if new_btc:
                        log.info("Intel: published %d BTC signal(s)", len(new_btc))

                except Exception:
                    log.exception("BTC signal generation failed.")

            # ── Publish Kalshi signals to Redis ───────────────────────────────
            # Pre-build close_time lookup from markets_cache (avoids O(n²) scan)
            close_time_map = {
                m["ticker"]: m.get("close_time") or m.get("expiration_time")
                for m in markets_cache
            }

            published = 0
            for sig in new_signals:
                try:
                    msg = kalshi_signal_to_message(sig, NODE_ID)
                    msg.close_time = close_time_map.get(sig.ticker)
                    await bus.publish_signal(msg)
                    metrics.signal_published(msg.asset_class, msg.strategy, msg.side)
                    summary.record(sig, executed=False)   # Intel just publishes
                    published += 1
                except Exception as exc:
                    log.warning("Failed to publish %s: %s", sig.ticker, exc)

            if published:
                log.info(
                    "Intel: published %d Kalshi signal(s)  (%d total, %d already held)",
                    published, len(signals), len(current_positions),
                )

            # ── Open positions count for metrics ──────────────────────────────
            metrics.update_positions(len(current_positions))

            # ── Drain execution reports → log fills + update metrics ──────────
            reports = await bus.consume_executions(intel_consumer)
            for r in reports:
                metrics.execution_received(r.status, r.asset_class)
                if r.status == "filled":
                    metrics.add_pnl(r.edge_captured)
                    log.info("Fill confirmed: %s %s ×%d @ %.4f  order=%s",
                             r.ticker, r.side, r.contracts, r.fill_price, r.order_id)
                elif r.status == "rejected":
                    log.debug("Rejected: %s  reason=%s", r.ticker, r.reject_reason)

            # ── Cycle timing ──────────────────────────────────────────────────
            elapsed = time.monotonic() - cycle_start
            metrics.cycle_duration.observe(elapsed) if not metrics._null else None
            sleep_s = max(0.0, cfg.POLL_INTERVAL - elapsed)
            log.info("Intel cycle %.1fs — sleeping %.0fs", elapsed, sleep_s)
            await asyncio.sleep(sleep_s)

    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Intel loop cancelled.")
    finally:
        ws.stop()
        await bus.publish_system_event("INTEL_STOP")
        await bus.close()
        summary.print_summary()
        log.info("Intel node shutdown complete.")
