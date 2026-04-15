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
import math
import os
import statistics
import time
from collections import deque
from typing import List, Optional

from ep_config import cfg, NODE_ID, REDIS_URL, EP_PRICES, log
from kalshi_bot.auth      import KalshiAuth, NoAuth
from kalshi_bot.client    import KalshiClient
from kalshi_bot.state     import BotState
from kalshi_bot.websocket import KalshiWebSocket
from kalshi_bot.strategy  import fetch_signals_async, scan_all_markets, Signal, fetch_treasury_2y_yield
from kalshi_bot.logger    import setup_logging, DailySummary
from ep_schema import PriceSnapshot, SignalMessage
from ep_bus import RedisBus
from ep_adapters import kalshi_signal_to_message
from ep_btc import BTCMeanReversionStrategy
from ep_metrics import metrics
from ep_behavioral import record_volume, is_late_money_spike, recency_bias_adj
from ep_polymarket import polymarket
from kalshi_bot.models.fomc import inject_kalshi_prices as _fomc_inject_prices
from ep_health import health as _src_health


async def _fetch_fed_rate(fred_api_key: str, fallback: float) -> float:
    """
    Fetch the current Fed Funds upper target rate from FRED (DFEDTARU).
    Daily series — updated same day as each FOMC decision.
    Returns fallback on any error.
    """
    if not fred_api_key:
        return fallback
    try:
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DFEDTARU&api_key={fred_api_key}"
            "&file_type=json&sort_order=desc&limit=1"
        )
        async with __import__("httpx").AsyncClient(timeout=8.0) as http:
            resp = await http.get(url)
        if resp.status_code == 200:
            obs = [o for o in resp.json().get("observations", []) if o.get("value") != "."]
            if obs:
                rate = float(obs[0]["value"])
                log.info("FRED DFEDTARU: current fed funds upper target = %.2f%%", rate)
                return rate
    except Exception as exc:
        log.warning("FRED rate fetch failed: %s — using fallback %.2f%%", exc, fallback)
    return fallback


# ── Order-book imbalance filter ────────────────────────────────────────────────
# Minimum ratio of directional depth to opposing depth before a signal is kept.
# YES signal: yes_bid_depth / no_bid_depth >= _MIN_OB_IMBALANCE
# NO  signal: no_bid_depth  / yes_bid_depth >= _MIN_OB_IMBALANCE
# Set to 0.0 via env to disable (passes all signals through).
_MIN_OB_IMBALANCE = float(os.getenv("MIN_OB_IMBALANCE", "0.70"))

# ── BTC realized-vol threshold adjuster ────────────────────────────────────────
# Rolling in-memory price buffer — no Redis read needed; populated each cycle.
# 240 entries × ~60 s cycle = ~4 h of history.
_btc_price_buf: deque = deque(maxlen=240)


async def _enrich_orderbook_imbalance(
    signals: List[Signal],
    client:  KalshiClient,
    min_imb: float = _MIN_OB_IMBALANCE,
) -> List[Signal]:
    """
    Fetch Kalshi orderbooks for candidate signals and drop those where the
    order book pushes against the signal direction.

    Kalshi orderbook structure:
      {"orderbook": {"yes": [[price_cents, qty], ...],
                     "no":  [[price_cents, qty], ...]}}

    yes[] = bids to buy YES (bullish pressure)
    no[]  = bids to buy NO  (bearish / sell-YES pressure)

    For a YES signal: yes_depth / no_depth must be >= min_imb.
    For a NO  signal: no_depth  / yes_depth must be >= min_imb.

    Arb-pair signals (arb_partner set) are passed through without filtering
    — they're balance-neutral by construction.
    Signals with no orderbook data (API error) are also passed through.
    """
    if not signals or min_imb <= 0.0:
        return signals

    # Arb signals bypass the filter; non-arb signals get enriched
    arb      = [s for s in signals if getattr(s, "arb_partner", None)]
    to_check = [s for s in signals if not getattr(s, "arb_partner", None)]

    if not to_check:
        return signals

    paths = [f"/markets/{s.ticker}/orderbook" for s in to_check]
    try:
        books = await asyncio.wait_for(client.get_many(paths), timeout=8.0)
    except Exception as exc:
        log.warning("Orderbook batch fetch failed — skipping imbalance filter: %s", exc)
        return signals

    kept = []
    for sig, ob in zip(to_check, books):
        if ob is None:
            # No data — don't block the signal
            kept.append(sig)
            continue

        book       = ob.get("orderbook", {})
        yes_levels = book.get("yes", [])   # YES bids: [[price, qty], ...]
        no_levels  = book.get("no",  [])   # NO  bids: [[price, qty], ...]

        # Sum top-5 levels; qty is element [1] of each pair
        yes_depth = sum(int(row[1]) for row in yes_levels[:5]) if yes_levels else 0
        no_depth  = sum(int(row[1]) for row in no_levels[:5])  if no_levels  else 0

        # Populate existing book_depth field with total visible liquidity
        sig.book_depth = yes_depth + no_depth

        if yes_depth == 0 and no_depth == 0:
            kept.append(sig)   # empty book — don't block
            continue

        imbalance = (
            yes_depth / max(no_depth,  1) if sig.side == "yes"
            else no_depth  / max(yes_depth, 1)
        )

        if imbalance >= min_imb:
            kept.append(sig)
        else:
            log.info(
                "OB filter dropped %-38s  side=%-3s  imbalance=%.2f < %.2f"
                "  (yes_depth=%d  no_depth=%d)",
                sig.ticker[:38], sig.side, imbalance, min_imb, yes_depth, no_depth,
            )

    return kept + arb


def _compute_vol_mult(buf: deque) -> tuple:
    """
    Compute a threshold multiplier from recent BTC realized volatility.

    Returns (multiplier: float, regime: str).

    Calibration (per-sample log-return std for a 60-120 s cycle):
      std = annualized_vol / sqrt(525_600 / cycle_seconds)

      calm    std < 0.0004  (< ~30 % annualized) → mult 0.85  (calm market, accept smaller edges)
      normal  std < 0.0013  (30-95 % annualized) → mult 1.00  (default — covers typical BTC)
      high    std < 0.0020  (95-145 % annualized)→ mult 1.30  (require bigger edge)
      extreme std >= 0.0020 (> 145 % annualized) → mult 1.65  (very selective)

    Falls back to (1.0, "insufficient_data") when the buffer has < 10 prices.
    """
    arr = list(buf)
    if len(arr) < 10:
        return 1.0, "insufficient_data"

    returns = [
        math.log(arr[i] / arr[i - 1])
        for i in range(1, len(arr))
        if arr[i - 1] > 0
    ]
    if len(returns) < 5:
        return 1.0, "insufficient_returns"

    try:
        std = statistics.stdev(returns)
    except statistics.StatisticsError:
        return 1.0, "stdev_error"

    if std < 0.0004:
        return 0.85, "calm"
    elif std < 0.0013:
        return 1.00, "normal"
    elif std < 0.0020:
        return 1.30, "high"
    else:
        return 1.65, "extreme"


async def _heartbeat_loop(bus: RedisBus, interval: int = 60) -> None:
    """Publish a HEARTBEAT event to ep:system every `interval` seconds."""
    while True:
        await asyncio.sleep(interval)
        await bus.publish_system_event("HEARTBEAT")


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
    heartbeat_task = asyncio.create_task(_heartbeat_loop(bus))

    # ── BTC mean-reversion strategy ───────────────────────────────────────────
    # Candle data: uses Polygon if POLYGON_API_KEY is set, otherwise falls back
    # to the free Coinbase Exchange public OHLC API (no key required).
    polygon_key  = os.getenv("POLYGON_API_KEY", "")
    btc_strategy: Optional[BTCMeanReversionStrategy] = BTCMeanReversionStrategy(
        polygon_api_key = polygon_key,
        source_node     = NODE_ID,
    )
    if polygon_key:
        log.info("BTC mean-reversion enabled (Polygon candles).")
    else:
        log.info("BTC mean-reversion enabled (Coinbase Exchange candles — free tier).")

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
    fomc_cache:        List[dict]     = []   # KXFED markets — refreshed with markets_cache
    markets_last_scan: float          = 0.0

    # Baseline BTC z-threshold from env (before LLM/vol overrides are applied)
    _btc_z_base: float = btc_strategy.z_thresh if btc_strategy else 1.5

    # ── Fed Funds rate + 2Y Treasury (FRED, refreshed daily) ────────────────
    _fred_key          = os.getenv("FRED_API_KEY", "")
    _rate_fallback     = float(os.getenv("CURRENT_FED_RATE", "4.25"))
    current_fed_rate, current_treasury_2y = await asyncio.gather(
        _fetch_fed_rate(_fred_key, _rate_fallback),
        fetch_treasury_2y_yield(_fred_key),
    )
    _rate_last_day: str   = __import__("datetime").date.today().isoformat()
    intel_consumer        = f"{NODE_ID}-intel"
    _last_balance_cents: Optional[int] = None   # persists last successful fetch

    try:
        while True:
            cycle_start = time.monotonic()
            state.record_cycle()
            summary.record_cycle()

            # ── Refresh FRED data once per calendar day ───────────────────────
            _today = __import__("datetime").date.today().isoformat()
            if _today != _rate_last_day:
                current_fed_rate, current_treasury_2y = await asyncio.gather(
                    _fetch_fed_rate(_fred_key, current_fed_rate),
                    fetch_treasury_2y_yield(_fred_key),
                )
                _rate_last_day = _today

            # ── Check ops halt flag ───────────────────────────────────────────
            if await bus.is_halted():
                log.warning("HALT_TRADING flag set in Redis — sleeping 60s.")
                await asyncio.sleep(60)
                continue

            # ── Balance ───────────────────────────────────────────────────────
            balance_cents = 100_000   # paper default ($1,000)
            if not cfg.PAPER_TRADE:
                try:
                    bal                  = client.get("/portfolio/balance")
                    balance_cents        = bal.get("balance", 0)
                    _last_balance_cents  = balance_cents   # save for fallback
                except Exception:
                    if _last_balance_cents is not None:
                        balance_cents = _last_balance_cents
                        log.warning("Balance fetch failed — using last known value (%d¢).",
                                    _last_balance_cents)
                    else:
                        log.warning("Balance fetch failed — no prior value; skipping publish.")
                        balance_cents = None   # type: ignore[assignment]
            if balance_cents is not None:
                state.set_balance(balance_cents)
                await bus.set_balance(balance_cents, state.mode)
                metrics.update_balance(balance_cents)

            # ── Market cache (full rescan every 20 min) ───────────────────────
            if time.monotonic() - markets_last_scan > 1200:
                try:
                    markets_cache     = scan_all_markets(client)
                    markets_last_scan = time.monotonic()
                    # Only subscribe WebSocket to tradeable markets (skip sports/novelty
                    # series that generate millions of sub-penny ticks we never trade)
                    _WS_PREFIXES = ("KXFED", "KXBTC", "KXETH", "INX", "NASDAQ", "CPI", "JOBS")
                    ws_tickers = [
                        m["ticker"] for m in markets_cache
                        if any(m["ticker"].startswith(p) for p in _WS_PREFIXES)
                    ]
                    ws.subscribe_tickers(ws_tickers)
                    log.info("Market rescan: %d markets (%d WS subscriptions)",
                             len(markets_cache), len(ws_tickers))
                except Exception:
                    log.exception("Market scan failed.")
                try:
                    kxfed_resp = client.get(
                        "/markets",
                        params={"status": "open", "series_ticker": "KXFED", "limit": 200},
                    )
                    fomc_cache = kxfed_resp.get("markets", [])
                    log.debug("FOMC cache refreshed: %d markets", len(fomc_cache))
                except Exception:
                    log.debug("FOMC cache refresh failed — close_time may be null")

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

            # ── Inject live KXFED prices into FOMC model ──────────────────────
            # Primary: WebSocket snapshot (real-time ticks)
            # Fallback: fomc_cache REST prices (refreshed every 20 min) — covers
            #           thin markets that rarely trade and thus never get WS ticks.
            _kxfed_snap: dict[str, int] = {}
            for _t, _p in snapshot.prices.items():
                if _t.startswith("KXFED-") and isinstance(_p, dict):
                    _yp = _p.get("yes_price")
                    if isinstance(_yp, (int, float)) and _yp > 0:
                        _kxfed_snap[_t] = int(_yp)
            # Augment from REST fomc_cache for tickers not in WS snapshot.
            # Kalshi REST API v2 uses _dollars suffix for price fields.
            for _m in fomc_cache:
                _ft = _m.get("ticker", "")
                if not _ft.startswith("KXFED-") or _ft in _kxfed_snap:
                    continue
                # Try _dollars fields (REST API v2) then legacy field names
                _mp = (
                    _m.get("last_price_dollars")
                    or _m.get("yes_bid_dollars")
                    or _m.get("last_price")
                    or _m.get("yes_bid")
                    or _m.get("market_price")
                )
                if _mp and float(_mp or 0) > 0:
                    _kxfed_snap[_ft] = round(float(_mp) * 100)
            if _kxfed_snap:
                _fomc_inject_prices(_kxfed_snap)
            else:
                _src_health.mark_fail("kalshi_implied",
                                      "no KXFED prices in WS snapshot or fomc_cache")

            # ── Health tracking for core infrastructure ───────────────────────
            # kalshi_ws: mark OK if WS is alive (connected) OR if ep:prices has
            # data — WS snapshot is empty for thin prediction markets that don't
            # trade every minute, but that is normal and expected behaviour.
            _ws_has_prices = bool(snapshot.prices)
            _redis_has_prices = bool(_kxfed_snap)
            if _ws_has_prices or _redis_has_prices:
                _src_health.mark_ok("kalshi_ws",
                                    f"ws={len(snapshot.prices)} rest={len(_kxfed_snap)}")
            else:
                _src_health.mark_fail("kalshi_ws", "no prices from WS or REST")
            _src_health.mark_ok("redis")
            _src_health.log_cycle_summary()

            # ── Redis config overrides (dashboard writes these to ep:config) ────
            _ov_edge   = await bus.get_config_override("override_edge_threshold")
            _ov_maxc   = await bus.get_config_override("override_max_contracts")
            _ov_conf   = await bus.get_config_override("override_min_confidence")
            _ov_hbc    = await bus.get_config_override("override_hours_before_close")
            _ov_rate   = await bus.get_config_override("CURRENT_FED_RATE")

            try:
                edge_threshold = float(_ov_edge) if _ov_edge else cfg.EDGE_THRESHOLD
            except (ValueError, TypeError):
                log.warning("Malformed override_edge_threshold=%r — using default", _ov_edge)
                edge_threshold = cfg.EDGE_THRESHOLD
            try:
                max_contracts = int(float(_ov_maxc)) if _ov_maxc else cfg.MAX_CONTRACTS
            except (ValueError, TypeError):
                log.warning("Malformed override_max_contracts=%r — using default", _ov_maxc)
                max_contracts = cfg.MAX_CONTRACTS
            try:
                min_confidence = float(_ov_conf) if _ov_conf else cfg.MIN_CONFIDENCE
            except (ValueError, TypeError):
                log.warning("Malformed override_min_confidence=%r — using default", _ov_conf)
                min_confidence = cfg.MIN_CONFIDENCE
            # Only override the FRED-fetched rate if the key is explicitly set
            if _ov_rate:
                try:
                    current_fed_rate = float(_ov_rate)
                except (ValueError, TypeError):
                    log.warning("Malformed CURRENT_FED_RATE=%r — keeping FRED value", _ov_rate)

            # ── Vol-adjusted Kalshi edge threshold ────────────────────────────
            # Scale edge_threshold up during high BTC realized vol — serves as a
            # macro-uncertainty proxy (high crypto vol → require larger Kalshi edge).
            # vol_mult / vol_regime are also used later for BTC z_thresh.
            vol_mult, vol_regime = _compute_vol_mult(_btc_price_buf)
            if vol_mult != 1.0:
                _pre_vol_edge = edge_threshold
                edge_threshold = round(edge_threshold * vol_mult, 4)
                log.debug(
                    "Vol-adj edge_threshold %.4f → %.4f (regime=%s)",
                    _pre_vol_edge, edge_threshold, vol_regime,
                )

            # ── Volume recording (behavioral late-money detector) ─────────────
            # Build a per-ticker volume map for late-money spike detection below.
            # record_volume() updates the in-memory ring buffer; no Redis I/O.
            _market_vol_map: dict = {}
            for _m in markets_cache:
                _vol = float(_m.get("volume", 0) or 0)
                record_volume(_m["ticker"], _vol)
                _market_vol_map[_m["ticker"]] = _vol

            # ── Signal generation ─────────────────────────────────────────────
            # Direct await — intel_main() IS the running event loop;
            # no asyncio.run() wrapper needed (or allowed) here.
            signals: List[Signal] = []
            try:
                signals = await fetch_signals_async(
                    client               = client,
                    edge_threshold       = edge_threshold,
                    max_contracts        = max_contracts,
                    min_confidence       = min_confidence,
                    fred_api_key         = _fred_key,
                    current_rate         = current_fed_rate,
                    treasury_2y          = current_treasury_2y,
                    enable_fomc          = True,
                    enable_weather       = os.getenv("ENABLE_WEATHER", "true") == "true",
                    enable_economic      = os.getenv("ENABLE_ECONOMIC", "true") == "true",
                    enable_sports        = os.getenv("ENABLE_SPORTS", "true") == "true",
                    enable_crypto_price  = os.getenv("ENABLE_CRYPTO_PRICE", "true") == "true",
                    enable_gdp           = os.getenv("ENABLE_GDP", "true") == "true",
                    markets_cache        = markets_cache,
                    btc_spot             = btc_strategy.last_spot if btc_strategy else None,
                )

                # ── Orderbook imbalance filter ─────────────────────────────────
                # Drop signals where the live order book contradicts the direction
                # (e.g., a YES signal when NO buyers outnumber YES buyers ≥ 1.4×).
                # Runs after strategy filtering so we only hit the orderbook API
                # for the small set of already-qualified candidates.
                if signals:
                    _before = len(signals)
                    signals = await _enrich_orderbook_imbalance(signals, client)
                    _dropped = _before - len(signals)
                    if _dropped:
                        log.info("OB filter: dropped %d/%d signals (imbalance)", _dropped, _before)

                # ── Behavioral adjustments (late-money + recency bias) ─────────
                # Applied after OB filter so adjustments only hit candidate signals.
                for _sig in signals:
                    # Late-money spike: accelerating volume → market may be crowded
                    _cur_vol = _market_vol_map.get(_sig.ticker, 0.0)
                    if is_late_money_spike(_sig.ticker, _cur_vol):
                        _sig.confidence = max(0.10, _sig.confidence * 0.70)
                        log.info(
                            "Late-money spike: %-38s  confidence → %.2f",
                            _sig.ticker[:38], _sig.confidence,
                        )
                    # Recency bias: recent surprise outcome → temper fair value
                    _series = _sig.ticker.split("-")[0] if "-" in _sig.ticker else _sig.ticker
                    _bias   = await recency_bias_adj(_series, bus)
                    if _bias != 0.0:
                        _sig.fair_value = max(0.01, min(0.99, _sig.fair_value + _bias))
                        _sig.edge       = _sig.fair_value - _sig.market_price

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

            # ── Publish REST-derived Kalshi prices to Redis ───────────────────
            # The WebSocket only delivers ticks when a trade occurs.  On thin
            # FOMC markets (few trades per day) ep:prices never gets populated,
            # so exec's exit checker skips every Kalshi position.  Backfill with
            # the REST market_price (bid/ask mid) from each signal — good enough
            # for take-profit / stop-loss decisions.
            if signals:
                ts_now = int(time.time() * 1_000_000)
                kalshi_price_patch: dict = {}
                for _s in signals:
                    if _s.market_price and _s.ticker:
                        # market_price is 0–1 scale; ep:prices uses 0–100 integer
                        # cents to match BotState (mkt.yes_price is int cents).
                        mp_cents = round(_s.market_price * 100)
                        kalshi_price_patch[_s.ticker] = json.dumps({
                            "yes_price":  mp_cents,
                            "no_price":   100 - mp_cents,
                            "spread":     _s.spread_cents or 0,
                            "last_price": mp_cents,
                            "ts_us":      ts_now,
                        })
                if kalshi_price_patch:
                    await bus._r.hset(EP_PRICES, mapping=kalshi_price_patch)
                    log.debug("Published REST prices for %d Kalshi tickers to ep:prices",
                              len(kalshi_price_patch))

            # ── Price backfill for held positions below signal edge threshold ──
            # Positions can have stale prices if their edge falls below MIN_EDGE_GROSS
            # (e.g. KXGDP YES at 3¢ won't appear in signals). The exit checker skips
            # tickers whose ep:prices entry is >5 min old. Patch from markets_cache
            # (already in memory) to keep all held positions fresh.
            _held_set    = set(await bus.get_all_positions())
            _sig_tickers = {s.ticker for s in signals}
            _unheld_miss = _held_set - _sig_tickers
            if _unheld_miss:
                _markets_by_ticker = {
                    m["ticker"]: m for m in markets_cache if "ticker" in m
                }
                _ts_now = int(time.time() * 1_000_000)
                _gap_patch: dict = {}
                for _t in _unheld_miss:
                    _m = _markets_by_ticker.get(_t)
                    if not _m:
                        continue
                    _mp = (
                        _m.get("last_price_dollars")
                        or _m.get("yes_bid_dollars")
                        or _m.get("last_price")
                        or _m.get("yes_bid")
                        or _m.get("market_price")
                    )
                    if _mp and float(_mp or 0) > 0:
                        _yp = round(float(_mp) * 100)
                        _gap_patch[_t] = json.dumps({
                            "yes_price":  _yp,
                            "no_price":   100 - _yp,
                            "spread":     0,
                            "last_price": _yp,
                            "ts_us":      _ts_now,
                        })
                if _gap_patch:
                    await bus._r.hset(EP_PRICES, mapping=_gap_patch)
                    log.debug(
                        "Price backfill: %d held tickers not in signals → ep:prices",
                        len(_gap_patch),
                    )

            # ── Dedup: skip tickers already held in Redis positions ───────────
            # For arb signals both legs must be held to consider the pair complete.
            # If only the primary is held (partner failed), re-publish so Exec can
            # place the missing partner leg.
            current_positions = await bus.get_all_positions()
            new_signals = [
                s for s in signals
                if s.ticker not in current_positions
                or (
                    getattr(s, "arb_partner", None)
                    and s.arb_partner not in current_positions
                )
            ]

            # ── Polymarket divergence signals ─────────────────────────────────
            # Refresh Polymarket cache (no-op if within CACHE_TTL=60s).
            # Generates Signal objects for Kalshi markets that diverge >4¢ from
            # their Polymarket peer — these flow through the same publish path below.
            await polymarket.refresh()
            _poly_sigs = polymarket.divergence_signals(signals)
            for _ps in _poly_sigs:
                if _ps.ticker not in current_positions:
                    new_signals.append(_ps)
            if _poly_sigs:
                log.info("Polymarket: %d divergence signal(s) added", len(_poly_sigs))

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

                # ── Vol-adjusted BTC z_thresh ──────────────────────────────────
                # LLM policy sets the strategic baseline; vol_mult (computed above
                # from the price buffer) scales it upward in volatile conditions so
                # we only enter mean-reversion trades on truly extreme dislocations.
                _z_base = float(z_str) if z_str else _btc_z_base
                btc_strategy.z_thresh = round(_z_base * vol_mult, 2)
                if vol_regime != "normal" and vol_regime != "insufficient_data":
                    log.debug(
                        "Vol-adj z_thresh: %.2f  (base=%.2f  mult=%.2f  regime=%s)",
                        btc_strategy.z_thresh, _z_base, vol_mult, vol_regime,
                    )

                try:
                    btc_msgs: List[SignalMessage] = await asyncio.wait_for(
                        btc_strategy.generate(), timeout=30.0
                    )
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
                        # Feed the vol-threshold buffer (1-cycle lag is intentional)
                        _btc_price_buf.append(btc_strategy.last_spot)

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
            # Pre-build close_time lookup from both the generic market cache and
            # the FOMC-specific cache (KXFED tickers come from a separate targeted
            # fetch, not the generic scan_all_markets page).
            close_time_map = {
                m["ticker"]: m.get("close_time") or m.get("expiration_time")
                for m in (*markets_cache, *fomc_cache)
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

            already_held = len(signals) - len(new_signals)
            if published:
                log.info(
                    "Intel: published %d Kalshi signal(s)  (%d total, %d deduped)",
                    published, len(signals), already_held,
                )

            # ── Open positions count for metrics ──────────────────────────────
            metrics.update_positions(len(current_positions))

            # ── Drain execution reports → log fills + update metrics ──────────
            # Tally rejection reasons for a one-line cycle summary (reduces log spam
            # from dozens of DUPLICATE/RISK_GATE entries).
            reports     = await bus.consume_executions(intel_consumer)
            reject_tally: dict[str, int] = {}
            for r in reports:
                metrics.execution_received(r.status, r.asset_class)
                if r.status == "filled":
                    metrics.add_pnl(r.edge_captured)
                    log.info("Fill confirmed: %s %s ×%d @ %.4f  order=%s",
                             r.ticker, r.side, r.contracts, r.fill_price, r.order_id)
                elif r.status == "rejected":
                    reason = r.reject_reason or "UNKNOWN"
                    reject_tally[reason] = reject_tally.get(reason, 0) + 1

            if reject_tally:
                # Surface non-trivial rejections (anything except pure DUPLICATE noise)
                non_dup = {k: v for k, v in reject_tally.items() if k != "DUPLICATE"}
                if non_dup:
                    log.info("Exec rejections this cycle: %s", non_dup)
                else:
                    log.debug("Exec rejections this cycle: %s", reject_tally)

            # ── Cycle timing ──────────────────────────────────────────────────
            elapsed = time.monotonic() - cycle_start
            metrics.observe_cycle(elapsed)
            sleep_s = max(0.0, cfg.POLL_INTERVAL - elapsed)
            log.info("Intel cycle %.1fs — sleeping %.0fs", elapsed, sleep_s)
            await asyncio.sleep(sleep_s)

    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Intel loop cancelled.")
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        ws.stop()
        await bus.publish_system_event("INTEL_STOP")
        await bus.close()
        summary.print_summary()
        log.info("Intel node shutdown complete.")
