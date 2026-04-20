# EdgePulse — Complete System Overview

---

## 1. What It Is

EdgePulse is a two-node automated trading system that generates signals on prediction markets (Kalshi) and BTC spot (Coinbase). It runs fully in **paper mode** by default — no real money moves until both `KALSHI_PAPER_TRADE=false` and `COINBASE_PAPER=false` are set.

The system is the successor to a single-process Kalshi bot. The key architectural change is separating signal generation from order execution across two physical machines, communicating through Redis streams.

---

## 2. Infrastructure

| Node | Machine | Location | Service |
|---|---|---|---|
| **Intel** | DigitalOcean Droplet (2vCPU / 4GB) | NYC3 | `edgepulse.service` |
| **Exec** | QuantVPS | Chicago | `edgepulse-exec.service` |
| **Redis** | Runs on Intel node | localhost:6379 | Exec connects to 167.71.27.43:6379 |

**SSH:** `ssh quantvps` (configured in `/root/.ssh/config`)

**Deploy (always use deploy.sh — never scp individual files):**
```bash
./deploy.sh           # sync + restart both nodes (standard)
./deploy.sh --intel   # restart Intel only
./deploy.sh --exec    # sync + restart Exec only
./deploy.sh --sync    # sync without restarting
```

`deploy.sh` rsyncs `ep_*.py`, `kalshi_bot/`, and `edgepulse_launch.py` to quantvps, verifies `ep_exec.py` checksum, then restarts both services.

Never manually run `python3 edgepulse_launch.py` — always use systemd. The exec service has `ExecStartPre=-/usr/bin/fuser -k 9092/tcp` which kills any stale process on port 9092 before starting, preventing duplicate process bugs.

**Logs:**
```
Intel: /root/EdgePulse/output/logs/edgepulse.log
Exec:  /root/EdgePulse/output/logs/exec.log  (via ssh quantvps)
```

---

## 3. Architecture — Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│                        INTEL NODE (NYC3)                    │
│                                                             │
│  Kalshi WebSocket ──► BotState (live prices)               │
│  Kalshi REST API  ──► markets_cache (all 1595 markets)     │
│  FRED API         ──► FOMC model, GDP model, Fed rate      │
│  Atlanta Fed      ──► GDPNow estimate                       │
│  Coinbase Exchange──► BTC candles (5m, 100 candles)        │
│  Coinbase public  ──► BTC spot price                       │
│  Binance public   ──► BTC candles/spot (backup)            │
│  OKX public       ──► BTC funding rate                     │
│  alternative.me   ──► Crypto Fear & Greed Index            │
│  ESPN public      ──► Sports scores/schedules              │
│  NOAA NWS         ──► Weather data                         │
│  Polymarket Gamma ──► Cross-market arb prices              │
│                                                             │
│  Every 120s:                                                │
│    1. Fetch signals from all models                         │
│    2. Filter already-held positions (dedup)                 │
│    3. Publish SignalMessages → ep:signals (Redis Stream)    │
│    4. Publish prices → ep:prices (Redis Hash)              │
│    5. Read execution reports ← ep:executions               │
└───────────────────────────┬─────────────────────────────────┘
                            │ Redis Streams (ep:signals)
                            │ (167.71.27.43:6379)
┌───────────────────────────▼─────────────────────────────────┐
│                        EXEC NODE (Chicago)                  │
│                                                             │
│  Signal Consumer ──► TTL check → dedup → risk gate         │
│                  ──► Kalshi REST API (order placement)      │
│                  ──► Coinbase Advanced Trade API (BTC)      │
│                                                             │
│  Exit Checker (every 60s):                                  │
│    Reads ep:prices ──► stop-loss / take-profit / trailing   │
│                    ──► mean-reversion (mid-BB cross)        │
│                    ──► break-even stop (after tranche 1)    │
│                    ──► max-hold timeout (12h BTC)           │
│                    ──► pre-expiry tranches (Kalshi)         │
│                                                             │
│  Publishes ExecutionReports → ep:executions                 │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Redis Keys

| Key | Type | Written by | Read by | Purpose |
|---|---|---|---|---|
| `ep:signals` | Stream | Intel | Exec | Trade signals (consumer group: exec-consumers) |
| `ep:executions` | Stream | Exec | Intel | Fill/reject reports (consumer group: intel-consumers) |
| `ep:positions` | Hash | Exec | Intel + Exec | Open positions (authoritative source of truth) |
| `ep:prices` | Hash | Intel | Exec | Per-ticker price snapshots with timestamp |
| `ep:balance` | Hash | Intel | Exec | Kalshi account balance (used for sizing) |
| `ep:config` | Hash | LLM agent / ops | Intel + Exec | Runtime overrides (Kelly fraction, halt flag, etc.) |
| `ep:health` | Hash | Intel | Dashboard | Data source health registry (node_id → JSON) |
| `ep:system` | Stream | Both | Monitoring | Lifecycle events (INTEL_START, EXEC_STOP, HEARTBEAT) |
| `ep:performance` | String | Exec + Intel | Dashboard | Hourly P&L summary JSON (TTL 25h) |
| `ep:cooldown:{ticker}` | String (TTL) | Exec | Exec | Stop-loss re-entry cooldown (30min/2h/24h) |
| `ep:stopcnt:{ticker}` | String (TTL 7d) | Exec | Exec | Stop-loss escalation counter |
| `ep:cut_loss:{ticker}` | String (TTL 300s) | Intel | Exec | Fundamental cut-loss signal (GDP reversal) |
| `ep:tombstone:{ticker}` | String | Intel | Exec | Cancel resting order + block re-entry |
| `ep:bot:config` | String | Dashboard | Dashboard | UI state JSON written by the SaaS dashboard |

---

## 5. Core Modules

### `ep_config.py`
Shared bootstrap. Loads `.env`, sets up `sys.path` so `kalshi_bot/` is importable, defines all Redis key names and consumer group names. Every other module imports this first.

**Key constants:**
- `SIGNAL_TTL = 30,000ms` — signals older than 30s are discarded by Exec
- `EXIT_INTERVAL = 60s` — how often the exit checker runs
- `STREAM_BLOCK = 5,000ms` — Redis blocking read timeout

---

### `ep_schema.py`
Three dataclasses for Redis stream messages:

- **`SignalMessage`** — everything needed to place a trade: ticker, side, fair_value, market_price, edge, fee_adjusted_edge, confidence, contracts, asset_class (`kalshi` or `btc_spot`), strategy name, BTC-specific fields (btc_price, btc_z_score, btc_lookback_m), TTL timestamp
- **`ExecutionReport`** — fill confirmation or rejection: ticker, side, contracts, fill_price, status (filled/rejected/expired), mode (paper/live), edge_captured, reject_reason
- **`PriceSnapshot`** — batch of ticker→price pairs published by Intel each cycle

---

### `ep_bus.py` — `RedisBus`
All Redis I/O goes through this single class. Manages:
- Stream publish/consume (`publish_signal`, `consume_signals`, `publish_execution`, `consume_executions`)
- Position read/write (`set_position`, `delete_position`, `get_all_positions`)
- Price read/write (`publish_prices`, `get_prices`)
- Balance reporting (`report_balance`)
- Config overrides (`get_config_override`)
- Consumer group lifecycle (auto-creates groups, handles "group already exists" gracefully)
- `push_btc_history` — rolling BTC price/RSI/z history for dashboard

---

### `ep_positions.py` — `PositionStore`
Thin wrapper around `RedisBus` position methods. Adds:
- `open()` — writes position with `entered_at` timestamp and `pending=True` flag (crash protection)
- `close()` — HDEL from Redis, returns the stored dict
- `update_fields()` — merge partial updates without closing (used for tranche tracking, high-water mark, break-even stop)
- `total_exposure_cents()` — sum of (entry_cents × contracts) across all open positions

---

### `ep_intel.py` — Intel main loop
Runs on the Intel node. Core cycle (every 120s):

1. Fetch all Kalshi markets via REST → `markets_cache`
2. Run all signal scanners (FOMC directional, FOMC butterfly arb, calendar spread arb, cross-series GDP-FOMC coherence, GDP, crypto price, economic+ADP, sports, weather, Polymarket divergence)
3. Run `BTCMeanReversionStrategy.generate()` — fetches candles + spot + sentiment concurrently
4. Filter signals: drop tickers already in `ep:positions`
5. Apply orderbook imbalance filter (`MIN_OB_IMBALANCE=0.70`)
6. Publish price snapshots to `ep:prices` (all signal tickers + held tickers not in signals)
7. Publish `SignalMessage`s to `ep:signals` stream
8. Drain `ep:executions` — log fills, update internal stats
9. Publish BTC price + indicators (spot, RSI, z-score, mid-BB) to `ep:prices["BTC-USD"]`
10. Publish Kalshi balance to `ep:balance`
11. Sleep for remainder of 120s cycle

**Notable Intel-side behavior:**
- Held positions are deduped (no duplicate entry signals)
- Gap-fill: held tickers below `MIN_EDGE_GROSS` (won't appear in signals) are back-filled into `ep:prices` from `markets_cache` so Exec's exit checker doesn't see stale prices
- Vol-adjusted BTC z-threshold: rolling 4h price buffer → realized vol → `vol_mult` raises z_thresh in volatile conditions
- LLM policy overrides read from `ep:config` each cycle (RSI thresholds, z-threshold, Kelly)

---

### `ep_exec.py` — Exec main loop
Runs on the Exec node. Four concurrent async tasks:

#### `_signal_consumer`
Continuously drains `ep:signals` via `XREADGROUP`. For each signal:
1. Check `HALT_TRADING` flag
2. TTL check (discard if > 30s old)
3. Dedup (skip if ticker already in `ep:positions`)
4. Stop-loss cooldown check (skip if stopped out within last 30 min) — **set before any await to prevent race condition**
5. Meeting concentration limit (max 4 positions per FOMC meeting date)
6. Fetch balance (Coinbase USD balance for BTC; Kalshi balance from Redis for Kalshi)
7. LLM kill switches (`llm_btc_enabled`, `llm_kalshi_enabled`)
8. Kelly sizing via `UnifiedRiskEngine`
9. Category/series/market exposure limits (Kalshi only)
10. Risk approval gate
11. Write pending position to Redis (crash protection)
12. Execute: `_execute_btc()` → Coinbase, or `executor.execute()` → Kalshi
13. Confirm position (clear pending flag)
14. Publish `ExecutionReport`

#### `_exit_checker`
Runs every 60s. For each open position:
1. Consume `ep:tombstone:{ticker}` keys — cancel resting order + tombstone position
2. Consume `ep:cut_loss:{ticker}` keys — for filled positions: place sell-limit at market; for resting orders: cancel_and_tombstone
3. Fetch price from `ep:prices` — skip if stale (> 300s old)
4. **Near-certain skip** (Kalshi only) — if YES price ≤ `KALSHI_NEAR_CERTAIN_THRESHOLD_CENTS` (8¢) or ≥ 92¢, skip pre-expiry exit; hold to auto-resolution at 0¢/100¢
5. Check pre-expiry tranches (Kalshi only — T-24h exit 50%, T-2h exit remainder); skipped for near-certain positions
6. **Near-expiry stop suppression** — if `days_left < KALSHI_NEAR_EXPIRY_NO_STOP_DAYS` (7), stop-loss and trailing stop are suppressed; contract resolves naturally
7. BTC max-hold timeout (12h) — exits without cooldown penalty
8. Trailing stop (fires if drawdown from HWM ≥ `TRAILING_STOP_CENTS`; suppressed within 7 days of expiry)
9. BTC mean-reversion tranche 1 (price ≥ mid-BB while in profit → exit half, set break-even stop)
10. Break-even stop on remainder (exit if price falls back to entry)
11. BTC mean-reversion tranche 2 (exit remainder at mid-BB)
12. Take-profit (`TAKE_PROFIT_CENTS=40`)
13. Stop-loss (`STOP_LOSS_CENTS=30`) — sets Redis-persisted cooldown (30min/2h/24h escalation); suppressed within 7 days of expiry
14. Publish `ExecutionReport`, notify Telegram

#### `_heartbeat_loop`
Publishes `HEARTBEAT` event to `ep:system` every 30s. Used by monitoring.

#### `poll_resolutions_loop`
Queries Kalshi `/markets?status=settled` hourly. Records outcomes in SQLite, feeds `ep_behavioral.py` recency bias.

---

## 6. Trading Models

### FOMC Directional (`kalshi_bot/models/fomc.py`, `kalshi_bot/strategy.py`)

**What it does:** Prices Kalshi KXFED contracts (e.g., "Will the Fed Funds rate be above X% after the June 2026 meeting?") by computing the probability of each rate outcome.

**Data sources (priority order):**
1. **Kalshi-implied prices** (primary) — extracts market consensus directly from existing KXFED contract prices via monotonicity-constrained inversion
2. **FRED `DFEDTARU`** (fallback) — current Fed Funds upper target rate
3. **CME FedWatch** (optional, frequently down) — market-implied probabilities
4. **2Y Treasury yield** — additional signal

**Signal generation:** For each KXFED ticker, computes `fair_value` = model probability that rate will be above the contract's threshold. If `fair_value - market_price > MIN_EDGE_GROSS (0.12)`, generates a YES signal; if `market_price - fair_value > 0.12`, generates a NO signal.

**Concentration limit:** Max 4 positions per FOMC meeting date (`MAX_POSITIONS_PER_MEETING=4`). Once all 8 tracked meetings are full, all FOMC signals are rejected as `MEETING_CONCENTRATION`.

**Current model label:** `kalshi_implied+fred`

---

### GDP (`scan_gdp_markets` in `strategy.py`)

**What it does:** Prices KXGDP contracts (e.g., "Will Q1 2026 GDP growth exceed 2.5%?") using the Atlanta Fed GDPNow real-time estimate.

**Data source:** FRED series `GDPNOW` — fetches last 4 quarterly observations.

**Quarter matching:** Each market ticker encodes its expiry (KXGDP-26APR30 = April 30, 2026 = Q1 2026 GDP report). The scanner matches market expiry to the correct GDPNow quarter. APR30 → Q1, JUL30 → Q2 (skipped if no Q2 estimate available yet).

**Signal math:** For each threshold T, `p_above = Φ((GDPNow - T) / 0.9)` where 0.9pp is GDPNow's RMSE. If `p_above - market_price > 0.04`, generates signal.

**Fallback:** If GDPNow fetch fails, uses weighted average of last 4 BEA quarters (`A191RL1Q225SBEA`) with recency weighting.

---

### BTC Mean Reversion (`ep_btc.py`, wired in `ep_intel.py`)

**What it does:** Generates BTC spot buy/sell signals when price is statistically overextended, expecting reversion to the rolling mean.

**Entry — all conditions must pass:**

| Condition | Long (buy) | Short (sell) |
|---|---|---|
| RSI-14 | < 35 (oversold) | > 65 (overbought) |
| Bollinger Band (20, 2σ) | price < lower band | price > upper band |
| Z-score (20-period) | < −1.5 (vol-adjusted) | > +1.5 (vol-adjusted) |
| Volume filter | latest candle volume < 1.5× 20-candle avg | same |
| Trend filter | 20-SMA within 1.5% of 50-SMA | same |
| Sentiment skip | F&G ≥ 75 AND funding rate > 0.15% → skip long | F&G ≤ 25 AND funding rate < −0.15% → skip short |

**Sentiment adjustments (confidence ±):**
- Fear & Greed ≤ 20: +0.10 to long confidence (extreme fear = good long)
- Fear & Greed ≥ 80: −0.12 from long confidence (crowded)
- Funding rate < −0.05%: +0.06 (shorts crowded = mean reversion stronger)
- Funding rate > 0.10%: −0.08 (longs crowded = fade caution)

**Exit waterfall:**
1. Max-hold timeout — 12h (BTC_MAX_HOLD_HOURS), no cooldown
2. Trailing stop — configurable, default TRAILING_STOP_CENTS
3. Mean-reversion tranche 1 — exit half at mid-BB (while in profit), set break-even stop
4. Break-even stop — remaining half can't lose (exits if price falls to entry)
5. Mean-reversion tranche 2 — exit remainder at mid-BB
6. Take-profit — fixed fallback (TAKE_PROFIT_CENTS=20)
7. Stop-loss — STOP_LOSS_CENTS=15, sets 30-min re-entry cooldown

**Data sources:**
- Primary candles: Coinbase Exchange (free, unauthenticated, 5-min OHLCV)
- Backup candles: Binance (BTCUSDT, free)
- Spot price: Coinbase public API → Binance fallback
- Funding rate: OKX perpetual swap (free)
- Fear & Greed: alternative.me (free)
- Polygon.io: optional (set POLYGON_API_KEY for premium candles)

**Execution:** `CoinbaseTradeClient` on Exec node, IOC market orders via Coinbase Advanced Trade API (JWT ES256 auth).

---

### Crypto Price Markets (`scan_crypto_price_markets` in `strategy.py`)

**What it does:** Scores KXBTC and similar crypto prediction markets on Kalshi ("Will BTC be above $X on date Y?") using live spot prices.

**Status:** Consistently produces 0 signals from ~365 markets. Markets are typically too efficiently priced for the current model to find edge above MIN_EDGE_GROSS.

---

### Economic Markets (`scan_economic_markets` in `strategy.py`)

**What it does:** Prices CPI, unemployment, and payroll threshold markets on Kalshi using FRED data series.

**Important:** KXGDP tickers are explicitly excluded from this scanner (`not ticker.startswith("KXGDP")`) — those go through the dedicated GDP scanner instead.

**FRED series used:** CPIAUCSL (CPI), UNRATE (unemployment), PAYEMS (nonfarm payrolls), CPILFESL (core CPI).

**Scales:** CPI ±0.30, UNRATE ±0.20, PAYEMS ±50.0.

---

### Sports (`scan_sports_markets` in `strategy.py`)

**Data source:** ESPN public API (no key required).

**Series covered:** KXNBA (basketball), KXNFL (football), KXMLB (baseball), KXNHL (hockey), KXSOC (soccer).

**Status:** Produces 0 signals unless active games are in progress with exploitable pricing.

---

### Weather (`scan_weather_markets` in `strategy.py`)

**Data source:** NOAA NWS free API.

**Series covered:** KXSNOW (snowfall), KXRAIN (precipitation), KXTEMP (temperature).

**Status:** Generates signals seasonally when NOAA forecasts strongly contradict Kalshi market pricing.

---

### Polymarket Arbitrage (`ep_polymarket.py`)

**What it does:** Compares Kalshi prices to equivalent Polymarket markets. If divergence > 4 cents (`DIVERGENCE_THRESHOLD=0.04`), generates an arb signal on Kalshi.

**Data source:** Polymarket Gamma API (free, no auth). Cache TTL = 60s.

**Matching:** Keyword-based series matching (e.g., KXFED → "federal reserve rate").

---

## 7. Risk Management

### Kalshi risk (`ep_risk.py`, `kalshi_bot/risk.py`)

| Parameter | Value | Env var |
|---|---|---|
| Kelly fraction | 0.25 | KALSHI_KELLY_FRACTION |
| Max contracts per signal | 15 | KALSHI_MAX_CONTRACTS |
| Min edge (gross) | 0.10 (10¢) | KALSHI_EDGE_THRESHOLD |
| Min confidence | 0.60 | KALSHI_MIN_CONFIDENCE |
| Min YES entry price | 0.60 (KXFED only) | KALSHI_MIN_YES_ENTRY_PRICE |
| Fallback-only edge threshold | 0.25 (25¢) | KALSHI_FALLBACK_ONLY_EDGE_THRESHOLD |
| Max spread | 10¢ | KALSHI_MAX_SPREAD_CENTS |
| Fee per trade | 7% of winnings | KALSHI_FEE_CENTS |
| Max single market exposure | 20% of balance | KALSHI_MAX_MARKET_EXPOSURE |
| Max total exposure | 80% of balance | KALSHI_MAX_TOTAL_EXPOSURE |
| Max category exposure | 60% of balance | hardcoded _MAX_CATEGORY_PCT |
| Max series exposure | 40% of balance | hardcoded _MAX_SERIES_PCT |
| Max positions per FOMC meeting | 4 | hardcoded MAX_POSITIONS_PER_MEETING |
| Daily drawdown limit | 20% | KALSHI_DAILY_DRAWDOWN_LIMIT |
| Take profit | 40¢ | KALSHI_TAKE_PROFIT_CENTS |
| Stop loss | 30¢ | KALSHI_STOP_LOSS_CENTS |
| Trailing stop | 12¢ | KALSHI_TRAILING_STOP_CENTS |
| Near-certain threshold | 8¢ (skip pre-expiry exit) | KALSHI_NEAR_CERTAIN_THRESHOLD_CENTS |
| Near-expiry stop suppression | 7 days before close_time | KALSHI_NEAR_EXPIRY_NO_STOP_DAYS |
| Pre-expiry exit | T-24h full, T-48h half | KALSHI_HOURS_BEFORE_CLOSE=24 |
| Stop-loss cooldown | 30min / 2h / 24h (escalating) | Redis-persisted ep:cooldown:{ticker} |
| Min Kelly trades (terminal) | 10 | KALSHI_MIN_KELLY_TRADES |

### BTC risk

| Parameter | Value | Env var |
|---|---|---|
| Risk per trade | 2% of Coinbase USD balance | COINBASE_BTC_RISK_FRAC |
| Min order size | 0.000016 BTC (~$1.20) | COINBASE_BTC_MIN_SIZE |
| Contract unit | 0.0001 BTC | hardcoded BTC_UNIT |
| Daily loss cap | 5% of balance | hardcoded |
| Max total BTC exposure | 30% of balance | hardcoded |
| Max hold duration | 12 hours | BTC_MAX_HOLD_HOURS |

---

## 8. LLM Policy Agent (`llm_agent.py`)

Runs standalone (not in the hot path). Every 4 hours, reads Redis state and calls Claude claude-opus-4-6 to review current positions, model signals, and market context.

**Writes to `ep:config` (all with `llm_` prefix):**
- `llm_rsi_oversold`, `llm_rsi_overbought` — BTC entry thresholds
- `llm_z_threshold` — BTC z-score entry threshold
- `llm_kelly_fraction` — Kalshi Kelly multiplier
- `llm_scale_factor` — post-sizing multiplier (0.5 = half size)
- `llm_btc_enabled` / `llm_kalshi_enabled` — asset class kill switches
- `halt_trading` — full system halt

Intel and Exec read these overrides at the start of each cycle.

---

## 9. Supporting Modules

### `ep_behavioral.py`
Two in-memory signals that adjust confidence:

- **Late money spike** — if per-cycle volume growth exceeds 3× rolling average, flags the market as potentially being front-run near resolution. Reduces confidence.
- **Recency bias** — reads `ep:resolutions` Redis hash. If last trade on a series was a high-confidence wrong call (surprise), returns −0.04 confidence adjustment. If a high-confidence correct call, returns +0.02.

### `ep_health.py`
Tracks up/down state for all external data sources. Distinguishes critical (Kalshi WebSocket, Kalshi REST) from optional (CME FedWatch, ESPN). Summary logged each Intel cycle.

### `ep_metrics.py`
Prometheus metrics on port 9091 (Intel) and 9092 (Exec):
- Signals published / executions filled / rejections by reason
- Balance, P&L, open position count
- BTC price, RSI, z-score
- Cycle duration histogram

### `ep_telegram.py`
Sends Telegram alerts on fills, exits, and critical warnings. Disabled by default until `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHANNEL_ID` are set.

### `ep_resolution_db.py`
SQLite at `output/resolutions.db`. Records resolved Kalshi markets and trade outcomes. Used by `ep_behavioral.py` for recency bias and by ops for win-rate analysis by series (`get_series_win_rate(series, days=30)`).

### `ep_adapters.py`
Translation layer between the legacy `kalshi_bot.strategy.Signal` format and the new `SignalMessage` schema. Adds risk flags (WIDE_SPREAD, HIGH_CONFIDENCE, ARB_PARTNER) and strips fields the executor doesn't need.

### `dashboard/` — React SaaS Dashboard
React + Vite + Tailwind dashboard served via the FastAPI backend (`api/`) on port 8502. Pages: Dashboard, Controls, Performance, Keys, Admin. All live data sourced from Redis via FastAPI proxy endpoints. Auth via JWT (bcrypt 4.0.1). `dashboard.py` (legacy Streamlit) has been removed.

### `ep_pnl_snapshots.py`
asyncpg module for writing P&L snapshots to Postgres table `pnl_snapshots`. Called from Intel's heartbeat loop; read by the FastAPI `performance` router to serve historical equity curves to the dashboard.

### `stats.py`
Standalone script for querying current Kalshi account balance and position P&L. Run manually: `python3 stats.py`.

---

## 10. Execution Path — Kalshi Orders

Kalshi uses RSA-signed REST API (not WebSocket for orders).

1. Signal arrives at Exec `_signal_consumer`
2. Passes all gates → `executor.execute(signal)` in `kalshi_bot/executor.py`
3. `Executor` places a **limit order** (`action=buy`, `side=yes/no`) at `signal.market_price` via `POST /portfolio/orders`
4. In paper mode: logs `[PAPER ENTRY]`, writes to `paper_positions.json`, updates `executor._positions`
5. In live mode: actual HTTP POST to `https://api.elections.kalshi.com/trade-api/v2/portfolio/orders`
6. Exit orders: **sell-limit** (`action=sell`, `side=<same side as entry>`) with `yes_price` / `no_price` field set to current market price. Kalshi has no true market orders; sell-limit at market is the correct exit mechanism.

Auth: RSA private key at `KALSHI_PRIVATE_KEY_PATH`, key ID `KALSHI_API_KEY_ID`.

---

## 11. Execution Path — Coinbase BTC Orders

1. BTC `SignalMessage` arrives at Exec
2. `_process_signal` fetches live Coinbase USD balance (`get_usd_balance_cents()`)
3. Sizes trade: `floor((balance * 0.02) / (btc_price * 0.0001))` contract units
4. `_execute_btc()` → `CoinbaseTradeClient.create_market_order(ticker, side, base_size)`
5. IOC market order via `POST /api/v3/brokerage/orders`
6. In paper mode: synthetic success response, no network call
7. Exits: `_exit_checker` calls `coinbase.create_market_order(ticker, reverse_side, size)` directly

Auth: CDP API key, ES256 JWT signed with EC P-256 private key at `COINBASE_PRIVATE_KEY_PATH`.

---

## 12. Position State — Two Sources of Truth

| Store | Who writes | Who reads | Contents |
|---|---|---|---|
| Redis `ep:positions` | Exec (PositionStore) | Intel (dedup), Exec (exit checker) | All open positions as JSON |
| `paper_positions.json` (disk) | Exec (executor._save_paper_positions) | Exec (startup load) | Backup for Kalshi paper positions |

**Startup sync:** Exec reads Redis first. If Redis has positions, it overwrites the disk file (`executor._positions = redis_positions`). If Redis is empty (first boot or after Redis flush), it reads from disk and restores to Redis. Redis is authoritative.

---

## 13. Live Mode Status

Both asset classes are **live** (real money):

| Setting | Value | Effect |
|---|---|---|
| `KALSHI_PAPER_TRADE=false` | false | Kalshi orders sent to exchange |
| `COINBASE_PAPER=false` | false | BTC orders sent to Coinbase Advanced Trade |

To revert to paper mode:
1. Set `KALSHI_PAPER_TRADE=true` in Exec `.env`
2. Set `COINBASE_PAPER=true` in Exec `.env`
3. `./deploy.sh --exec` to restart Exec only

**Note:** Coinbase BTC sell orders are currently blocked — API key `94ac0230` is missing the Trade scope. Fix via portal.cdp.coinbase.com → enable Trade scope.

---

## 14. What Is NOT Implemented

- **Model reversal exit** — when the FOMC model flips direction (e.g., was pricing CUT, now pricing HOLD), existing opposing positions are not automatically exited. They wait for stop-loss, take-profit, or pre-expiry.
- **Kalshi partial exits** — the two-tranche pre-expiry exit is the only partial exit. No mid-trade scale-out for Kalshi (BTC has it).
- **Cross-market Kalshi arb execution** — Polymarket divergence generates a Kalshi-side signal only. No simultaneous Polymarket order.
- **Live Coinbase balance for Kalshi sizing** — Kalshi trades are sized against Kalshi account balance (from Redis). No cross-asset balance netting.
- **CME BTC basis** — `UnifiedRiskEngine` has a stub for `cme_btc_basis` asset class; not yet implemented.
- **WebSocket order status** — order fills are not confirmed via WebSocket; relies on the position being written optimistically.

---

## 15. Known Historical Bugs (Fixed)

| Bug | Symptom | Fix |
|---|---|---|
| Wrong P&L formula for NO positions | All NO positions hit immediate stop-loss | `move_cents = entry_cents - current_cents` (not `(100-current) - entry`) |
| Duplicate exec processes | Stale code running alongside new deployment | `ExecStartPre fuser -k 9092/tcp` in systemd unit |
| `scan_economic_markets` matching KXGDP | KXGDP entered with `fair_value=0.98` | Added `and not ticker.startswith("KXGDP")` to econ filter |
| Held positions going stale in ep:prices | Exit checker skipped low-priced held tickers | Gap-fill loop in Intel backfills held tickers not in signal list |
| BTC exit not placing Coinbase order | Stop-loss closed Redis but left Coinbase position open | `_exit_checker` routes BTC exits to `coinbase.create_market_order()` |
| BTC sized against Kalshi balance | BTC trade sizing used wrong account | `get_usd_balance_cents()` added to `CoinbaseTradeClient` |
| GDP wrong quarter (JUL30 using Q1 data) | Q2 GDP markets priced with Q1 GDPNow | Quarter-matched per market; JUL30 skipped when Q2 unavailable |
| Stop-loss → re-entry → stop-loss loop | Same position stopped out every 60s | Cooldown set before `await positions.close()` — eliminates asyncio race |

---

## 16. Environment Variables — Full Reference

```
# Kalshi
KALSHI_API_KEY_ID           Key ID for Kalshi RSA auth
KALSHI_PRIVATE_KEY_PATH     Path to RSA private key PEM
KALSHI_PAPER_TRADE          true/false (default true)
KALSHI_BASE_URL             https://api.elections.kalshi.com/trade-api/v2
KALSHI_EDGE_THRESHOLD                0.10 — min gross edge for signal
KALSHI_FALLBACK_ONLY_EDGE_THRESHOLD  0.25 — higher bar when only FRED static is available
KALSHI_MAX_CONTRACTS                 15 — max contracts per order
KALSHI_POLL_INTERVAL                 120 — seconds between Intel cycles
KALSHI_MIN_CONFIDENCE                0.60
KALSHI_MIN_YES_ENTRY_PRICE           0.60 — suppress KXFED YES below 60¢ market price
KALSHI_KELLY_FRACTION                0.25
KALSHI_MIN_KELLY_TRADES              10 — min terminal trades before terminal-filtered Kelly
KALSHI_MAX_MARKET_EXPOSURE           0.20
KALSHI_MAX_TOTAL_EXPOSURE            0.80
KALSHI_DAILY_DRAWDOWN_LIMIT          0.20
KALSHI_MAX_SPREAD_CENTS              10
KALSHI_FEE_CENTS                     7
KALSHI_TAKE_PROFIT_CENTS             40
KALSHI_STOP_LOSS_CENTS               30
KALSHI_TRAILING_STOP_CENTS           12
KALSHI_NEAR_CERTAIN_THRESHOLD_CENTS  8 — skip pre-expiry exit if YES ≤ 8¢ or ≥ 92¢
KALSHI_NEAR_EXPIRY_NO_STOP_DAYS      7 — suppress stops within 7 days of close_time
KALSHI_HOURS_BEFORE_CLOSE            24.0 — pre-expiry exit window

# Coinbase BTC
COINBASE_API_KEY_NAME       CDP key name (organizations/.../apiKeys/...)
COINBASE_PRIVATE_KEY_PATH   Path to EC P-256 PEM
COINBASE_PAPER              true/false (default true)
COINBASE_BTC_RISK_FRAC      0.02 — 2% of balance per trade
COINBASE_BTC_MIN_SIZE       0.000016 BTC minimum order

# BTC Strategy
BTC_RSI_PERIOD              14
BTC_BB_PERIOD               20
BTC_BB_STD                  2.0
BTC_RSI_OVERSOLD            35
BTC_RSI_OVERBOUGHT          65
BTC_Z_THRESHOLD             1.5
BTC_CANDLE_MIN              5 (minutes)
BTC_CANDLE_COUNT            100 (candles fetched)
BTC_VOL_SPIKE_MULT          1.5 (skip if volume > 1.5× MA)
BTC_TREND_THRESH            0.015 (skip if 20-SMA deviated 1.5%+ from 50-SMA)
BTC_TREND_SMA               50 (candles for trend SMA)
BTC_MAX_HOLD_HOURS          12

# Infrastructure
MODE                        intel / exec
NODE_ID                     intel-do-nyc3 / exec-qvps-chi
REDIS_URL                   redis://localhost:6379/0 (Intel) or redis://167.71.27.43:6379/0 (Exec)
EP_SIGNAL_TTL_MS            30000
EP_EXIT_INTERVAL_S          60

# Data sources
FRED_API_KEY                FRED API key (GDPNow, fed rate, CPI, unemployment)
CURRENT_FED_RATE            3.75 — fallback if FRED unavailable
POLYGON_API_KEY             Optional — premium BTC candles
MIN_OB_IMBALANCE            0.70 — order book depth filter

# Feature flags
ENABLE_SPORTS               true/false
ENABLE_CRYPTO_PRICE         true/false
ENABLE_GDP                  true/false

# Notifications
TELEGRAM_BOT_TOKEN          Bot token for fill/exit alerts
TELEGRAM_CHANNEL_ID         Channel or chat ID
TELEGRAM_ADMIN_ID           Admin user ID for critical alerts

# LLM Agent
CLAUDE_MODEL                claude-opus-4-6
LLM_INTERVAL_HOURS          4

# Dashboard (Streamlit — run separately via screen/systemd)
DASHBOARD_REFRESH_S         3          # auto-refresh interval (default 3s)
NODE_STALE_S                120        # seconds before node flagged stale in UI
```

---

## 17. Directory Structure

```
/root/EdgePulse/
├── edgepulse_launch.py     Entry point — routes to intel or exec main loop
├── ep_config.py            Shared config, Redis keys, sys.path bootstrap
├── ep_schema.py            SignalMessage / ExecutionReport / PriceSnapshot
├── ep_bus.py               RedisBus — all stream and hash I/O
├── ep_positions.py         PositionStore — position lifecycle wrapper
├── ep_intel.py             Intel main loop (120s cycles, signal generation)
├── ep_exec.py              Exec main loop (signal consumer + exit checker)
├── ep_btc.py               BTC mean-reversion strategy + data clients
├── ep_coinbase.py          Coinbase Advanced Trade client (orders + balance)
├── ep_risk.py              UnifiedRiskEngine (Kalshi + BTC sizing + approval)
├── ep_adapters.py          Signal ↔ SignalMessage translation
├── ep_health.py            Data source health registry
├── ep_metrics.py           Prometheus metrics
├── ep_behavioral.py        Late-money spike + recency bias detectors
├── ep_polymarket.py        Polymarket price feed + arb signal generator
├── ep_pnl_snapshots.py     P&L snapshot writer to Postgres (asyncpg)
├── ep_telegram.py          Telegram alert wrapper
├── ep_resolution_db.py     SQLite resolution tracker + Sharpe ratio
├── deploy.sh               Sync + restart both nodes with checksum verify
├── llm_agent.py            Claude policy agent (runs every 4h)
├── stats.py                Manual balance/P&L query script
├── kalshi_bot/
│   ├── strategy.py         All Kalshi market scanners
│   ├── models/fomc.py      FOMC rate probability model
│   ├── executor.py         Kalshi order placement
│   ├── client.py           Kalshi REST client
│   ├── auth.py             Kalshi RSA auth
│   ├── state.py            BotState (in-memory market state)
│   └── websocket.py        Kalshi WebSocket price feed
├── .env                    All configuration (not committed)
├── private_key.pem         Kalshi RSA private key
├── coinbase_key.pem        Coinbase EC private key
├── dashboard/              React + Vite + Tailwind SaaS dashboard
│   ├── src/pages/          DashboardPage, ControlsPage, PerformancePage, etc.
│   └── postcss.config.js   Required for Tailwind CSS processing
├── api/                    FastAPI SaaS backend (port 8502)
│   └── routers/            controls, performance, microsoft, keys, admin
└── output/
    ├── logs/
    │   ├── edgepulse.log   Intel log
    │   └── exec.log        Exec log (on quantvps)
    ├── resolutions.db      SQLite market outcomes
    └── paper_positions.json Kalshi paper position backup
```
