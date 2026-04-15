# EdgePulse Trader — $300-Style BTC + Kalshi Stack

A low-latency, BTC-centric trading stack focused on:

- BTC mean-reversion & Kalshi-event edges
- Clean, institutional-grade market data (Polygon.io)
- Redis-based edge-bus
- Prometheus + Grafana monitoring
- Claude LLM as a policy-generator, signal-checker, and developer-assistant

---

## Architecture

```
┌─────────────────────────────────────┐     ┌───────────────────────────────┐
│  DO Droplet NYC3 (Intel node)       │     │  QuantVPS Chicago (Exec node)  │
│                                     │     │                                │
│  edgepulse_launch.py  MODE=intel    │     │  edgepulse_launch.py MODE=exec │
│  ├─ ep_intel.py                     │     │  ├─ ep_exec.py                 │
│  │   ├─ Kalshi WebSocket + signals  │     │  │   ├─ signal consumer        │
│  │   ├─ BTC mean-reversion (Polygon)│     │  │   ├─ risk gate              │
│  │   └─ publish to ep:signals ──────┼─────┼──┤   ├─ order execution       │
│  │                                  │     │  │   └─ exit checker           │
│  ├─ ep_btc.py  (RSI/BB/z-score)    │     │  └─ ep_risk.py                 │
│  ├─ ep_risk.py (Kelly sizing)       │     │      (BTC daily loss cap)      │
│  ├─ ep_metrics.py (Prometheus :9091)│     │  Prometheus :9092              │
│  │                                  │     │                                │
│  llm_agent.py  (Claude policy loop) │     │                                │
│                                     │     │                                │
│  ┌─────────────────────────────┐    │     └───────────────────────────────┘
│  │  Docker Compose             │    │
│  │  Redis   :6379  (edge-bus)  │    │
│  │  Prometheus :9090           │    │
│  │  Grafana    :3000           │    │
│  └─────────────────────────────┘    │
└─────────────────────────────────────┘
```

### Two-node design

| Node | Host | Role |
|---|---|---|
| Intel | DO Droplet NYC3 | Fetch data, compute signals, publish to Redis |
| Exec | QuantVPS Chicago | Consume Redis signals, apply risk gate, execute orders |

Both nodes run the same repo (`git pull` to deploy). `MODE` env var selects the role.

---

## Paid services (~$300/month)

| Service | Cost | Role |
|---|---|---|
| Polygon.io Personal | $200/mo | BTC-USD real-time OHLC |
| QuantVPS Chicago | $99–$120/mo | Low-latency execution node |
| DO Droplet 4CPU/8GB | $40–$80/mo | Intel node + Redis + monitoring |
| DO Managed Redis | $15/mo | Optional — replaces self-hosted Redis |

---

## Free services

| Service | Role |
|---|---|
| Kalshi API | Event-market price data + order execution |
| Coinbase public REST | BTC-USD spot price cross-check / fallback |
| FRED API | Fed Funds rate anchor for FOMC signals |
| Prometheus + Grafana | Self-hosted on DO Droplet |

---

## Signal strategies

### BTC mean-reversion (`ep_btc.py`)
Three-condition entry gate (all required):

| Side | RSI | Price | Z-score |
|---|---|---|---|
| LONG | RSI-14 < 35 | price < lower Bollinger | z < -1.5 |
| SHORT | RSI-14 > 65 | price > upper Bollinger | z > +1.5 |

Data: Polygon.io 5-min candles + Coinbase spot cross-check.
Parameters are runtime-overridable by the LLM agent via `ep:config`.

### Kalshi FOMC directional (`kalshi_bot/strategy.py`)
FRED-anchored fair value vs Kalshi market price. Signals when `fee_adjusted_edge > 10¢`.

### Kalshi FOMC arbitrage
Monotonicity violation across T-level contracts (e.g. P(rate>4.25) > P(rate>4.00)).

### Other Kalshi events
Weather, economic, and sports market signals from the Kalshi universal market scanner.

---

## Redis key layout

```
ep:signals       STREAM   Intel → Exec    edge opportunities (TTL-gated)
ep:executions    STREAM   Exec  → Intel   fill / reject reports
ep:positions     HASH     Exec writes     ticker → position JSON
ep:prices        HASH     Intel writes    ticker → price snapshot JSON
ep:balance       HASH     both nodes      node_id → balance JSON
ep:config        HASH     ops + LLM       runtime overrides (HALT_TRADING, llm_* keys)
ep:system        STREAM   both nodes      lifecycle events
ep:btc_history   LIST     Intel writes    rolling BTC price/RSI/z history (dashboard)
```

See `SCHEMA.md` for full message schemas.

---

## Claude LLM integration (`llm_agent.py`)

Claude runs **out of the hot path** as a standalone policy loop on the DO Droplet.

Every `LLM_INTERVAL_HOURS` hours (default: 4):
1. Reads BTC price/RSI/z-score, positions, fills, and balance from Redis
2. Returns a JSON policy document (RSI thresholds, Kelly fraction, scale factor, halt flag)
3. Writes each key to `ep:config` with `llm_` prefix
4. The trading bot picks up overrides on its next cycle — no restart needed

Prompt caching (`cache_control: ephemeral`) means only the ~200-token context delta
is billed on repeat runs. The ~1 KB system prompt is cached after the first call.

### LLM-controlled parameters

| Redis key | Effect |
|---|---|
| `llm_rsi_oversold` | BTC long entry RSI threshold |
| `llm_rsi_overbought` | BTC short entry RSI threshold |
| `llm_z_threshold` | BTC z-score entry threshold |
| `llm_kelly_fraction` | Kalshi Kelly fraction override |
| `llm_scale_factor` | Position size multiplier (0.5 = half, 1.5 = +50%) |
| `llm_max_contracts` | Max contracts cap |
| `llm_btc_enabled` | Enable/disable BTC signals |
| `llm_kalshi_enabled` | Enable/disable Kalshi signals |
| `HALT_TRADING` | Emergency stop (also writable by ops) |

---

## Risk management

### Kalshi (`kalshi_bot/risk.py` via `ep_risk.py`)
- Kelly sizing with configurable fraction (default 25%)
- Spread gate: reject signals with spread > `MAX_SPREAD_CENTS`
- Exposure gate: per-market + total exposure caps
- Daily drawdown halt

### BTC (`ep_risk.py`)
- 2% risk-per-trade sizing
- 5% daily session loss cap — no new BTC entries after cap is hit
- 30% total BTC exposure cap relative to balance

---

## Monitoring

Grafana dashboard at `http://<DO-IP>:3000` (auto-provisioned):
- BTC price, RSI, z-score (live chart)
- Signal counts by strategy and side
- Execution fill/reject rates
- Open positions count
- Session P&L (edge-cents)
- Node cycle duration histogram

Prometheus scrapes:
- Intel node: `host.docker.internal:9091/metrics`
- Exec node: `<QUANT_VPS_IP>:9092/metrics`

---

## File map

```
EdgePulse-Trader/
├── edgepulse_launch.py   Main entry point — MODE selects Intel or Exec
├── ep_config.py          Env-based config, Redis key namespace, sys.path bootstrap
├── ep_schema.py          SignalMessage / ExecutionReport / PriceSnapshot dataclasses
├── ep_bus.py             RedisBus — async Redis Streams + Hashes wrapper
├── ep_intel.py           Intel main loop (signal generation, price publishing)
├── ep_exec.py            Exec main loop (signal consumption, order placement, exits)
├── ep_btc.py             BTC mean-reversion strategy (Polygon + Coinbase)
├── ep_risk.py            UnifiedRiskEngine (Kalshi + BTC risk gates)
├── ep_positions.py       PositionStore — Redis-backed position state
├── ep_adapters.py        Signal ↔ SignalMessage translation
├── ep_metrics.py         Prometheus instrumentation
├── llm_agent.py          Claude LLM policy generator (runs out-of-band)
├── dashboard.py          Streamlit trading control panel (Redis data source)
├── docker-compose.yml    Redis + Prometheus + Grafana
├── prometheus.yml        Prometheus scrape config
├── start_edgepulse.sh    One-command stack startup
├── SCHEMA.md             Redis message schema reference
├── SETUP_CHECKLIST.md    Step-by-step deployment guide
└── .env.example          All environment variables with documentation
```

---

## Quick start

```bash
# On the DO Droplet (Intel node):
cd ~/Kalshi_bot/EdgePulse-Trader
cp .env.example .env
# Edit .env: set POLYGON_API_KEY, ANTHROPIC_API_KEY, MODE=intel
bash start_edgepulse.sh

# On QuantVPS (Exec node):
cd ~/Kalshi_bot/EdgePulse-Trader
cp .env.example .env
# Edit .env: set REDIS_URL=redis://<DO-IP>:6379/0, MODE=exec, KALSHI_PAPER_TRADE=true
MODE=exec NODE_ID=exec-qvps-chi python3 edgepulse_launch.py
```

See `SETUP_CHECKLIST.md` for the full step-by-step deployment guide.

---

## Emergency stop

```bash
redis-cli hset ep:config HALT_TRADING 1   # stop all trading immediately
redis-cli hset ep:config HALT_TRADING 0   # resume
```

Both nodes check this flag every cycle. Takes effect within 30 seconds.
