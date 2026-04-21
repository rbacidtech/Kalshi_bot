# EdgePulse

Distributed algorithmic trading system for CFTC-regulated prediction markets (Kalshi) and BTC spot (Coinbase). Separates signal generation from order execution across two nodes connected by a Redis message bus.

**Live status:** Both nodes running. ~23 open Kalshi positions. BTC pipeline enabled.

---

## Architecture

```
┌─────────────────────────────────┐         ┌─────────────────────────────────┐
│         INTEL NODE              │         │          EXEC NODE              │
│   DigitalOcean NYC3             │         │   QuantVPS Chicago              │
│   167.71.27.43                  │         │   172.93.213.88                 │
│                                 │         │                                 │
│  ep_intel.py  (120s cycles)     │──────▶  │  ep_exec.py  (event-driven)     │
│  ├─ FOMC model (6 sources)      │ep:signals│  ├─ 13-gate signal filter       │
│  ├─ BTC mean-reversion          │         │  ├─ Kelly sizing                │
│  ├─ KXBTC/KXETH log-normal      │         │  ├─ Kalshi order execution      │
│  ├─ GDP/CPI/NFP scanners        │         │  ├─ Coinbase BTC execution      │
│  └─ Macro regime classifier     │◀────────│  └─ Exit checker (60s)          │
│                                 │ep:executions                              │
└─────────────────────────────────┘         └─────────────────────────────────┘
                    │                                       │
                    └──────────────┬────────────────────────┘
                                   │
                          ┌────────▼────────┐
                          │  Redis (Docker) │
                          │  Intel node     │
                          │                 │
                          │  ep:signals     │  stream: Intel → Exec
                          │  ep:executions  │  stream: Exec → Intel
                          │  ep:positions   │  hash:   open positions
                          │  ep:prices      │  hash:   latest prices
                          │  ep:balance     │  hash:   per-node balance
                          │  ep:config      │  hash:   runtime overrides
                          │  ep:health      │  hash:   data source health
                          │  ep:cooldown:*  │  keys:   stop-loss cooldowns
                          │  ep:stopcnt:*   │  keys:   escalation counters
                          │  ep:cut_loss:*  │  keys:   fundamental cut-loss signals (300s TTL)
                          │  ep:tombstone:* │  keys:   cancel-resting-order signals
                          │  ep:bot:config  │  string: dashboard UI state (full JSON)
                          └─────────────────┘
```

---

## Signal Flow

```
Intel (every 120s):
  1. Fetch all data sources (parallelised)
  2. Run FOMC model → generate directional + arb signals
  3. Run BTC mean-reversion → 0 or 1 signal
  4. Run KXBTC/KXETH log-normal scanner → 0–N signals
  5. Run GDP/economic scanners → 0–N signals
  6. Dedup against ep:positions (skip held tickers)
  7. Sort by signal_quality_score (0–1)
  8. Publish survivors to ep:signals stream

Exec (event-driven):
  1. Consume signal from ep:signals
  2. Run 13 sequential gates (TTL → dedup → cooldown → circuit breaker →
     balance → LLM overrides → Kelly → category/series/market limits →
     risk approval)
  3. Execute order (Kalshi REST or Coinbase IOC)
  4. Write position to ep:positions (pending=True)
  5. fill_poll_loop confirms fill every 90s → fill_confirmed=True
  6. Exit checker (every 60s): take-profit / stop-loss / pre-expiry /
     resolution-driven / cut-loss-intel exits
```

---

## Data Sources

### FOMC Model (6-source fusion)

| Source | Auth | TTL | Notes |
|--------|------|-----|-------|
| Kalshi-implied prices | API key | 5 min | Primary; highest liquidity |
| CME FedWatch | OAuth2 (client_credentials) | 5 min | Per-meeting requests to avoid WAF |
| CME SR1 SOFR futures | OAuth2 | 1 min | 1-month SOFR |
| SOFR SR3 futures | Yahoo Finance | 10 min | 3-month, no auth |
| FRED FF1/FF2/FF3 | Query param (accepted risk; FRED requires it) | 1 hour | 30-day fed funds futures |
| FRED DFEDTARU + heuristic | Query param | 1 hour | Last resort; static current rate |

Confidence scoring: 0.92 (Kalshi-implied) → 0.70 (single stale source). Model divergence > 4¢ adds a penalty.

### FRED Macro Indicators (cached 3600s)

`DFEDTARU` · `CPILFESL` · `PCEPI` · `ICSA` · `T10Y2Y` · `T5YIFR` · `UNRATE` · `VIXCLS` · `DGS10`

Note: FRED API requires the key as a URL query parameter — no header auth option exists. This is a documented accepted risk; URLs are never logged.

### BTC Data

| Source | Purpose | Fallback |
|--------|---------|---------|
| Polygon.io | 5-minute OHLCV candles (primary) | Coinbase Exchange OHLC |
| Coinbase Exchange | OHLC candles (secondary) | Binance |
| Binance | Emergency candle fallback | — |
| Coinbase public API | Real-time spot price | Binance spot |
| alternative.me | Fear & Greed Index (0–100) | Neutral default (50) |
| OKX | BTC perpetual funding rate | None |
| Deribit | DVOL (30-day implied vol for KXBTC model) | 80% static fallback |

### Other Sources

| Source | Purpose |
|--------|---------|
| GDPNow (Atlanta Fed) | GDP threshold market signals |
| PredictIt | FOMC market cross-reference |
| Kalshi WebSocket | Live bid/ask/last prices (persistent) |
| Kalshi REST | Market scanner, order placement, fill polling |

---

## Signal Categories

| Category | Markets | Model |
|----------|---------|-------|
| `fomc_directional` | KXFED-* | FOMC probability fusion × regime adjustment |
| `fomc_arb` | KXFED-* pairs | Monotonicity arbitrage (T2.75/T3.00/T3.25 YES) |
| `fomc_butterfly_arb` | KXFED-* triplets | Convexity violation: P(A)+P(C)−2×P(B) < −0.04 |
| `calendar_spread_arb` | KXFED-* same strike | Rate-path arb: NO if later_yes > earlier_yes + 0.10 |
| `cross_series_coherence` | KXFED-* (45+ days out) | GDP-FOMC coherence: low GDPNow → YES on distant cut strikes |
| `crypto_price` | KXBTC-*, KXETH-* | Log-normal binary option pricing (Deribit DVOL) |
| `gdp` | KXGDP-* | GDPNow vs. strike comparison |
| `economic` | KXCPI-*, KXNFP-* | BLS data vs. strike; ADP leading indicator |
| `mean_reversion` | BTC-USD (Coinbase) | RSI + Bollinger Bands + z-score |

---

## BTC Mean-Reversion Strategy

### Entry Conditions

**LONG** (buy BTC, expect price to rise back to mean):
- RSI-14 < 35 (oversold)
- Price < lower Bollinger Band (20-period, 2σ)
- z-score < −1.5 (price 1.5 std-devs below 20-period mean)
- No volume spike (latest vol ≤ 1.5 × 20-candle MA)
- No sustained downtrend (20-SMA within 1.5% of 50-SMA)
- Sentiment: skipped if F&G ≥ 75 AND funding rate > 0.0015

**SHORT** (sell BTC, expect price to revert to mean):
- RSI-14 > 65 (overbought)
- Price > upper Bollinger Band
- z-score > +1.5
- No volume spike
- No sustained uptrend
- Sentiment: skipped if F&G ≤ 25 AND funding rate < −0.0015

### Edge & Fee Calculation

```
edge = |mid_bb - spot| / spot
fee_adjusted_edge = max(0.0, edge - COINBASE_TAKER_FEE)   # default 0.6% taker
```

Edge sanity guards: skips if `edge ≤ 0` or `edge > 0.15` (data anomaly protection).

### Configurable Thresholds

| Env Var | Default | Description |
|---------|---------|-------------|
| `BTC_Z_THRESHOLD` | `1.5` | z-score trigger |
| `BTC_RSI_OVERSOLD` | `35` | RSI oversold level |
| `BTC_RSI_OVERBOUGHT` | `65` | RSI overbought level |
| `BTC_BB_PERIOD` | `20` | Bollinger Band lookback |
| `BTC_BB_STD` | `2.0` | Bollinger Band width |
| `BTC_CANDLE_MIN` | `5` | Candle width in minutes |
| `BTC_CANDLE_COUNT` | `100` | Candles fetched (= 8.3h history) |
| `BTC_VOL_SPIKE_MULT` | `1.5` | Volume spike threshold |
| `COINBASE_TAKER_FEE` | `0.006` | 0.6% taker fee (low-volume tier) |

---

## Risk Management

### Kalshi Signal Gates (applied on Exec, in order)

| Gate | Condition | Reject Reason |
|------|-----------|---------------|
| TTL | Signal age > 30s | `EXPIRED` |
| Dedup | Ticker in open positions | `DUPLICATE` |
| Entry cooldown | Recent order failure (30m / 2h / 24h tiers) | `ENTRY_FAILED_COOLDOWN` |
| Circuit breaker | ≥5 consecutive executor failures | `KALSHI_API_CIRCUIT` |
| Stop-loss cooldown | Recently stopped out (Redis-persisted) | `STOP_COOLDOWN` |
| LLM kill switch | `llm_kalshi_enabled = "0"` in ep:config | `LLM_KALSHI_DISABLED` |
| Kelly sizing | Kelly → 0 contracts | `RISK_GATE_SIZE` |
| Category limit | >60% of balance in one category | `CATEGORY_LIMIT` |
| Series limit | >40% of balance in one series | `SERIES_LIMIT` |
| Market limit | >15% of balance in one market | `MARKET_LIMIT` |
| Spread | Spread > `KALSHI_MAX_SPREAD_CENTS` | `RISK_GATE_SPREAD` |
| Exposure | Open exposure > max_total_exposure | `RISK_GATE_EXPOSURE` |
| Drawdown halt | Daily loss > `KALSHI_DAILY_DRAWDOWN_LIMIT` | `RISK_GATE_DRAWDOWN` |
| Redis failure | ep:positions unavailable (fail closed) | `RISK_GATE_REDIS` |

### Kelly Sizing

```
# Category lookup: arb | coherence | economic | directional
bucket          = _kelly_bucket(model_source)
base_kelly      = kelly_by_category.get(bucket, global_kelly)  # default 0.25
effective_kelly = base_kelly × confidence × vol_multiplier
net_edge        = edge − fee_cents
kelly_f         = net_edge / (1 − market_price)    # YES side
bet_fraction    = kelly_f × effective_kelly
contracts       = min(
    floor(balance × bet_fraction / price_cents),
    floor(balance × max_market_exposure / price_cents),
    max_contracts                                  # hard cap: 500
)
```

Kelly is calibrated from the last 90 days of **terminal** trades (exit price ∈ {0, 100}), with **14¢ round-trip fee subtracted** from each trade's P&L before win/loss classification. Four per-category fractions (arb, coherence, economic, directional) are derived from separate buckets; each falls back to the global fraction if < 10 qualifying trades are in that bucket.

**Volatility multiplier** (`vol_multiplier`) for economic release markets:
- `0.70` — within 7 days before a CPI/GDP print (high uncertainty → size down)
- `1.40` — within 48h after a confirmed print (uncertainty resolved → size up)
- `1.00` — default (no release proximity detected)

### Stop-Loss Escalation (Redis-persisted, survives restarts)

| Stop count | Cooldown |
|------------|----------|
| 1st | 30 minutes |
| 2nd | 2 hours |
| 3rd+ | 24 hours |

Counter resets after 7 days (`ep:stopcnt:{ticker}` TTL).

### BTC Risk Limits

| Limit | Value |
|-------|-------|
| Risk per trade | 2% of Coinbase portfolio |
| Daily loss cap | 5% of balance |
| Max total BTC exposure | 30% of balance |
| Min order size | 0.000016 BTC (~$1) |

---

## Exit Logic

Exec checks all open positions every 60 seconds:

| Mode | Condition | Action |
|------|-----------|--------|
| Take-profit | Current price ≥ entry + `KALSHI_TAKE_PROFIT_CENTS` (20¢) | Close 100% |
| Stop-loss | Current price ≤ entry − `KALSHI_STOP_LOSS_CENTS` (15¢) | Close 100%, start cooldown |
| Cut-loss (intel) | Intel writes `ep:cut_loss:{ticker}` (fundamental reversal) | Sell limit at market; cancel+tombstone if resting |
| Pre-expiry tranche 1 | ≤24h to close_time | Close 50% |
| Pre-expiry tranche 2 | ≤2h to close_time | Close remaining |
| Resolution-driven | Known outcome against position | Exit immediately |
| Post-resolution cleanup | Market closed >2h | Remove stale position |

**Cut-loss signals** are written by Intel (currently GDP only) when GDPNow diverges >0.75pp against the held position within 14 days of expiry. The key has a 300s TTL; Exec consumes it on the next 60s exit-checker tick. For filled positions: places a `sell limit` order at the current market price. For resting orders: `cancel_and_tombstone`.

**P&L convention:** `entry_cents` always stores the YES-market price × 100 for both YES and NO positions. For NO positions: `move_cents = entry_cents − current_yes_price_cents`.

---

## LLM / Operator Overrides

Write directly to `ep:config` Redis hash — takes effect on next Exec cycle, no restart needed:

```bash
# Halt all new entries
redis-cli hset ep:config HALT_TRADING "1"

# Resume
redis-cli hset ep:config HALT_TRADING "0"

# Override Kelly fraction (clamped 0.05–0.50)
redis-cli hset ep:config llm_kelly_fraction "0.15"

# Scale all position sizes (clamped 0.1–3.0)
redis-cli hset ep:config llm_scale_factor "0.5"

# Disable BTC entries only
redis-cli hset ep:config llm_btc_enabled "0"

# Disable Kalshi entries only
redis-cli hset ep:config llm_kalshi_enabled "0"

# Raise BTC z-score threshold (fewer, higher-conviction signals)
redis-cli hset ep:config BTC_Z_THRESHOLD "2.0"
```

---

## Deployment

### Prerequisites

- Python 3.12, Docker + Docker Compose
- Two VPS nodes with SSH key access between them

### Intel Node

```bash
git clone <repo> /root/EdgePulse && cd /root/EdgePulse
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys + MODE=intel
docker compose up -d
cp edgepulse.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now edgepulse
```

### Exec Node

```bash
# Sync code from Intel (never sync .env)
rsync -avz --checksum /root/EdgePulse/ep_*.py quantvps:/root/EdgePulse/
rsync -avz --checksum /root/EdgePulse/kalshi_bot/ quantvps:/root/EdgePulse/kalshi_bot/

# On exec: configure .env with MODE=exec, REDIS_URL pointing to Intel, Coinbase keys
cp edgepulse-exec.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now edgepulse-exec
```

### Ongoing Code Sync

Always use `deploy.sh` — never `scp` individual files manually, as node divergence causes silent bugs.

```bash
# Sync + restart both nodes (standard deploy)
./deploy.sh

# Restart Intel only (config change, no code change)
./deploy.sh --intel

# Sync + restart Exec only
./deploy.sh --exec

# Sync without restarting (inspect before restart)
./deploy.sh --sync
```

`deploy.sh` rsyncs `ep_*.py`, `kalshi_bot/`, and `edgepulse_launch.py` to quantvps, verifies `ep_exec.py` checksum, then restarts both systemd services.

---

## Configuration Reference

### Kalshi Trading

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_PAPER_TRADE` | `true` | Set `false` for live trading |
| `KALSHI_EDGE_THRESHOLD` | `0.10` | Minimum edge in dollars (10¢) |
| `KALSHI_MIN_CONFIDENCE` | `0.60` | Minimum model confidence |
| `KALSHI_KELLY_FRACTION` | `0.25` | Quarter-Kelly sizing |
| `KALSHI_MAX_CONTRACTS` | `5` | Hard cap per order |
| `KALSHI_MAX_MARKET_EXPOSURE` | `0.10` | Max % of balance per market |
| `KALSHI_MAX_TOTAL_EXPOSURE` | `0.80` | Max % of balance deployed total |
| `KALSHI_DAILY_DRAWDOWN_LIMIT` | `0.20` | Halt at 20% daily loss |
| `KALSHI_MAX_SPREAD_CENTS` | `10` | Skip markets wider than 10¢ |
| `KALSHI_FEE_CENTS` | `7` | Kalshi 7% fee modelled as 7¢/contract |
| `KALSHI_TAKE_PROFIT_CENTS` | `20` | Exit at +20¢ per contract |
| `KALSHI_STOP_LOSS_CENTS` | `15` | Exit at −15¢ per contract |
| `KALSHI_HOURS_BEFORE_CLOSE` | `24.0` | Start pre-expiry exits N hours before expiry |
| `KALSHI_POLL_INTERVAL` | `120` | Seconds between Intel cycles |

### Coinbase / BTC

| Variable | Default | Description |
|----------|---------|-------------|
| `COINBASE_PAPER` | same as `KALSHI_PAPER_TRADE` | Paper mode for BTC |
| `COINBASE_API_KEY_NAME` | — | CDP key name |
| `COINBASE_PRIVATE_KEY_PATH` | — | EC P-256 PEM path (chmod 600) |
| `COINBASE_BTC_RISK_FRAC` | `0.02` | 2% of portfolio per BTC trade |
| `COINBASE_BTC_MIN_SIZE` | `0.000016` | Minimum BTC order size |
| `COINBASE_TAKER_FEE` | `0.006` | 0.6% taker fee (low-volume tier) |

### Required API Keys

| Key | Node | Used For |
|-----|------|---------|
| `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` | Both | RSA-PSS signed REST + WebSocket |
| `FRED_API_KEY` | Intel | All FRED macro series |
| `CME_FEDWATCH_API_KEY_NAME` + `CME_FEDWATCH_API_PASSWORD` | Intel | FedWatch OAuth2 |
| `COINBASE_API_KEY_NAME` + `COINBASE_PRIVATE_KEY_PATH` | Exec | Advanced Trade orders (ES256 JWT) |
| `POLYGON_API_KEY` | Intel | BTC OHLC candles (primary source) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHANNEL_ID` | Both | Alerts (optional) |

---

## Operations

### Monitor

```bash
# Intel live log
tail -f /root/EdgePulse/output/logs/edgepulse.log

# Exec live log
ssh quantvps "tail -f /root/EdgePulse/output/logs/exec.log"

# Open positions count
docker exec $(docker ps -q) redis-cli -a "PASSWORD" hlen ep:positions

# Balances
docker exec $(docker ps -q) redis-cli -a "PASSWORD" hgetall ep:balance
```

### Tombstone a Position (block re-entry + cancel resting order)

```bash
# Manual Redis tombstone (blocks dedup + exit checker)
docker exec $(docker ps -q) redis-cli -a "PASSWORD" \
  hset ep:positions "KXTICKER-EXPIRY-TSTRIKE" \
  '{"contracts":0,"side":"yes","fill_confirmed":false,"order_id":""}'
```

### Emergency Halt

```bash
docker exec $(docker ps -q) redis-cli -a "PASSWORD" hset ep:config HALT_TRADING "1"
```

### Restart Both Services

```bash
systemctl restart edgepulse && ssh quantvps "systemctl restart edgepulse-exec"
```

---

## Infrastructure

### Intel Node — DigitalOcean NYC3

| Item | Value |
|------|-------|
| IP | 167.71.27.43 |
| Services | edgepulse (systemd), Redis, Postgres, Prometheus, Grafana (all Docker) |
| Ports open | 22 (SSH), 8501–8503 (dashboards) |
| Redis port | 6379, locked to exec IP by UFW |

### Exec Node — QuantVPS Chicago

| Item | Value |
|------|-------|
| IP | 172.93.213.88 |
| SSH alias | `ssh quantvps` |
| Services | edgepulse-exec (systemd) |
| Ports open | 22 (SSH), 9092 (Prometheus, Intel IP only) |

### Security Hardening

- Redis: password auth required; `FLUSHALL`/`FLUSHDB` disabled
- UFW active on both nodes, default-deny inbound
- fail2ban: maxretry=3, bantime=1h
- SSH password auth disabled on exec (key-only)
- Log files: chmod 640; private keys: chmod 600
- FRED API keys in URL query params (FRED does not support header auth — accepted risk, URLs never logged)

---

## Monitoring & Alerts

### Telegram

Alerts sent when `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHANNEL_ID` are configured:

| Alert | When |
|-------|------|
| Fill notification | New position opened |
| Exit notification | Position closed (reason + P&L) |
| Circuit breaker | API failure threshold reached |
| Daily summary | 22:00 UTC — P&L, trades, win rate |
| CRITICAL escalation | Sent to `TELEGRAM_ADMIN_ID` |

### Grafana (port 3000)

Dashboards: position P&L, signal flow, Kelly sizing, data source health, Redis stream depth.

### Prometheus

Metrics scraped from `:9091` (Intel) and `:9092` (Exec). Includes signal counts, execution latency, rejection reasons, BTC z-score/RSI, Kalshi balance.

---

## Known Limitations

| Issue | Status |
|-------|--------|
| KXGDP-26APR30-T2.5 YES | ⚠️ Cut-loss sell order placed at 31¢ (order_id: 8eb50b0f). Resting — fills if a buyer appears before Apr 30. |
| KXGDP-26APR30-T1.0 NO | ⚠️ GDPNow 1.31% > 1.0% strike (0.31pp gap) — below 0.75pp cut-loss threshold; holding to expiry Apr 30. |
| Arb multi-leg execution | `arb.py` detects opportunities; executor places only one leg |
| CME basis strategy | `asset_class = "cme_btc_basis"` is a stub — returns 0 contracts |
| BTC LONG needs USD cash | Only $0.49 USD available; SELL signals work with existing BTC |
| Drawdown halt not Redis-persisted | Restarting exec clears an active halt; ERROR log fires on activation |
| BTC Kelly ignores round-trip fee | 1.2% round-trip (2 × 0.6%) not yet subtracted from BTC Kelly inputs |

---

## File Map

| File | Lines | Role |
|------|-------|------|
| `ep_intel.py` | ~2700 | Intel main loop: data fetching, signal generation, publishing |
| `ep_exec.py` | ~2300 | Exec main loop: signal consumption, execution, exit management |
| `ep_btc.py` | ~800 | BTC mean-reversion: indicators, signal generation, sentiment |
| `ep_bus.py` | ~450 | RedisBus: all stream/hash I/O |
| `ep_schema.py` | ~220 | SignalMessage, ExecutionReport, PriceSnapshot dataclasses |
| `ep_risk.py` | ~160 | UnifiedRiskEngine: Kalshi + BTC risk separation |
| `ep_positions.py` | ~100 | PositionsManager: Redis position CRUD, exposure calculation |
| `ep_health.py` | ~500 | Data source health registry + circuit breakers (22 sources) |
| `ep_telegram.py` | ~320 | Telegram alert client |
| `ep_coinbase.py` | ~310 | CoinbaseTradeClient: JWT auth, market orders, balance |
| `ep_polymarket.py` | ~400 | Polymarket Gamma feed: pagination, price matching, arb signals |
| `ep_pnl_snapshots.py` | ~80 | asyncpg P&L snapshot writer to Postgres (called from Intel heartbeat) |
| `ep_config.py` | ~80 | Runtime config from env vars, Redis key constants |
| `deploy.sh` | ~80 | Sync + restart both nodes with checksum verification |
| `kalshi_bot/strategy.py` | ~2700 | Signal generation: FOMC directional, arb, GDP, crypto, economic scanners |
| `kalshi_bot/models/fomc.py` | ~600 | FOMC probability model: 6-source fusion, confidence scoring |
| `kalshi_bot/risk.py` | ~225 | RiskManager: Kelly, exposure limits, drawdown halt |
| `kalshi_bot/executor.py` | ~445 | Kalshi order placement, sell-limit exits, fill confirmation |
| `kalshi_bot/auth.py` | — | RSA-PSS signing for Kalshi API |
| `kalshi_bot/client.py` | — | HTTP client: retries, backoff (max 8s), async batch |
| `kalshi_bot/state.py` | — | BotState, MarketState, PositionState dataclasses |
| `kalshi_bot/websocket.py` | — | Persistent WebSocket, auto-reconnect (max 60s backoff) |
| `ep_resolution_db.py` | — | Resolution history, performance analytics, Sharpe ratio |
| `dashboard/` | — | React + Vite + Tailwind dashboard (port 8502 via FastAPI proxy) |
| `api/` | — | FastAPI SaaS backend: auth, key vault, positions proxy, admin |
| `docker-compose.yml` | — | Redis, Postgres, Prometheus, Grafana |
