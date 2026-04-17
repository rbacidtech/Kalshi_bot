# EdgePulse — Changelog

All notable changes to the EdgePulse distributed trading system are documented here.

---

## [1.0.0] — 2026-04-16  Production hardening: security, correctness, live trading

### Security hardening
- **Redis `requirepass`** added — all connections now authenticate with 64-character token
- **FLUSHALL / FLUSHDB disabled** via `rename-command ""` in docker-compose — eliminates cryptominer attack vector (root cause of nightly position wipe: attacker at 34.70.205.211 called FLUSHALL every ~25 min via unauthenticated Redis)
- **Redis `activedefrag yes`** — continuous background defragmentation; memory fragmentation ratio dropped from 5.85 → 1.20 after BGREWRITEAOF
- **UFW enabled on both nodes** — Intel: 6379 only from QuantVPS IP; Exec: 22 open, 9092 only from Intel IP; default deny incoming on all other ports
- **fail2ban** — installed on both nodes; maxretry=3, bantime=1h; immediately banned 10+ SSH brute-force attackers on Intel
- **SSH hardening on Exec** — `PasswordAuthentication no`; only Intel's ed25519 key accepted
- **Redis AOF rewritten** — `BGREWRITEAOF` removed malicious FLUSHALL + cron injection commands from persistent AOF log
- **Grafana password** rotated from default "changeme"
- **Kalshi client env** updated to `REDIS_URL` with password on both nodes

### Correctness fixes (execution pipeline)
- **fill_poll partial-cancel bug** — orders with `status=canceled AND fill_count>0` previously looped forever as "PARTIAL FILL"; now finalized immediately with actual filled quantity
- **fill_poll executor sync** — after `positions.update_fields()` in fill_poll, now mirrors update into `executor._positions` to prevent state divergence handler from restoring stale `fill_confirmed=False`
- **Race condition in exit path** — `executor._positions.pop()` now runs before `positions.close()` so exit_checker cannot fire between the two operations
- **Right-tail truncation guard** — NO signals for strikes > `current_rate + 0.50` suppressed; HIKE_50 is the model ceiling and edge at T4.50/T4.75 was a probability floor artifact, not real edge
- **NO cost in Kelly sizing** — `price_cents = 100 - market_price_cents` for NO side; was incorrectly using YES price, causing over-sizing
- **NO cost in approve() gate** — `order_cost = (100 - entry_cents) × contracts` for NO side; was using YES price, causing under-counting in exposure checks
- **NO cost in per-series/category limits** — `sig_cost` and `t_cost` now use `(100 - entry)` for NO positions; was double-counting exposure as if buying YES
- **NO cost passed to UnifiedRiskEngine** — `side=sig.side` now forwarded to `_kalshi.approve()` in `ep_risk.py`
- **Retry loop cooldowns** — `BALANCE_UNKNOWN`, `RISK_GATE_SIZE` (10 min), `UNKNOWN_ASSET_CLASS` now set `_entry_failed_cooldown` to prevent hot retry loops on transient failures
- **Startup orphan reconciliation** — `_reconcile_orphan_orders()` on startup: fetches resting Kalshi orders, restores any missing from Redis (prevents positions disappearing after Redis wipe)

### Signal quality
- **Edge at ask-price** — published edge now adjusted by half-spread before Intel publishes; prevents trading signals that only look good at mid
- **Spread-to-edge filter** — signals where `spread > edge` (guaranteed negative EV) suppressed at Intel
- **GDP YES signal suppression** — KXGDP YES signals skipped when `GDPNow < (strike - 0.50)`
- **GDP startup risk check** — Intel warns on startup if any KXGDP YES position has GDPNow materially below strike
- **KXGDP excluded from economic scanner** — GDP markets were being double-processed; now handled only by the dedicated GDP scanner

### Infrastructure
- **Fee-aware P&L logging** — entry/exit reports now subtract `FEE_CENTS × contracts` so reported P&L is net of exchange fees
- **Consumer group recovery** — ep:executions consumer now starts from `id="0"` (replays from stream head) instead of `id="$"` (skip) on group creation; prevents losing execution reports after Redis restart
- **Consumer group NOGROUP handler** — two-pass mkstream strategy with INFO logging on creation vs recovery
- **PYTHONUNBUFFERED=1** in both systemd service files — log output flushes immediately
- **Kalshi API circuit breaker** — halts exec after 5 consecutive API errors; prevents runaway retry storms
- **Daily risk reset** — `set_balance()` resets `_start_balance` and `_halted` at UTC midnight

### Monitoring
- **Exec peer liveness check** — Intel warns if exec HEARTBEAT is > 120s old
- **CME FedWatch OAuth2** — confirmed working with `auth.cmegroup.com/as/token.oauth2` endpoint; confidence 0.92 with dual-source (Kalshi-implied + FRED static fallback)
- **ep:prices backfill** — positions below current edge threshold are backfilled with last-known price snapshot so exit_checker has data for all held positions

---

## [0.9.0] — 2026-04-15  Structural stabilization

### Changes
- **Flat directory structure** — `EdgePulse-Trader/` subdirectory removed; all source files live at repo root
- **systemd service** — `edgepulse-exec.service` enabled as managed unit on QuantVPS; `ExecStartPre` kills stale `:9092` processes on startup
- **Single-leg arb fix** — both legs of a Kalshi arb signal now execute atomically in `_process_signal`
- **FOMC model_src label** — displays `kalshi_implied+fred` accurately when Kalshi prices are the primary source
- **close_time backfill** — existing positions with null `close_time` field are backfilled on exec startup

---

## [0.8.0] — 2026-04-12  FOMC model v2 — CME FedWatch fusion

### Changes
- **CME FedWatch primary source** — FedWatch probabilities via OAuth2 token exchange now primary FOMC model input
- **FRED FF1/FF2/FF3 fallback** — 30-day fed funds futures as secondary fallback when CME unavailable
- **FRED DFEDTARU anchor** — live effective fed funds rate fetched daily; replaces static `CURRENT_FED_RATE` env var
- **Confidence scoring** — signal confidence 0.95 (CME primary) → 0.92 (Kalshi-implied) → 0.75 (FRED static)
- **GDP scanner** — KXGDP markets added to signal pipeline with GDPNow integration
- **Kalshi-implied fallback** — if all external sources unavailable, derives probability distribution from Kalshi YES prices directly

---

## [0.7.0] — 2026-04-08  Performance audit + data source health registry

### Changes
- **`ep_health.py`** — data source health registry; tracks last-success timestamp and error counts per source
- **Async order book fetching** — `client.get_many()` with `httpx.AsyncClient` and semaphore-limited concurrency; full scan time from O(n) → O(1) wall-clock
- **Per-request timeout override** — `per_request_timeout` param in `get_many()` prevents hung connections from blocking asyncio cleanup
- **Exec startup state divergence check** — compares `executor._positions` against Redis on startup; logs and repairs any mismatches
- **Prometheus metrics** — `ep_metrics.py` added; Intel scrape on `:9091`, Exec on `:9092`; Grafana auto-provisioning added

---

## [0.6.0] — 2026-04-05  Distributed architecture — EdgePulse v1

### Changes
- **Two-node split** — Intel (DO NYC3) + Exec (QuantVPS Chicago) communicate via Redis Streams
- **`ep_bus.py`** — `RedisBus` wrapping Redis Streams + Hash I/O; consumer groups with XREADGROUP
- **`ep_schema.py`** — `SignalMessage`, `ExecutionReport`, `PriceSnapshot` dataclasses with JSON round-trip
- **`ep_exec.py`** — Exec main loop: signal consumption, risk gate, Kalshi/Coinbase order placement, exit checker, fill poll
- **`ep_intel.py`** — Intel main loop: 120s scan cycle, price publishing, signal deduplication
- **`ep_risk.py`** — `UnifiedRiskEngine`: Kalshi Kelly + BTC daily loss cap in one gate
- **`ep_positions.py`** — Redis-backed `PositionStore`; `ep:positions` as source of truth
- **`ep_coinbase.py`** — Coinbase Advanced Trade (CDP) client for BTC execution
- **`ep_adapters.py`** — Signal ↔ SignalMessage translation layer
- **`ep_btc.py`** — BTC mean-reversion strategy: RSI-14 + Bollinger Bands + z-score; all three required simultaneously
- **`ep_polymarket.py`** — Polymarket CLOB arb signal source (resting)
- **`ep_behavioral.py`** — Behavioral pattern filters (news-window suppression, post-FOMC cooldown)
- **`ep_telegram.py`** — Telegram alert integration (disabled pending bot token)
- **LLM policy loop** — `llm_agent.py`; Claude reads Redis state every 4-6h and writes JSON policy to `ep:config`
- **`docker-compose.yml`** — Redis 7, Prometheus, Grafana on Intel node

---

## [0.5.0] — 2026-03-20  Strategy v2 — universal Kalshi scanner

### Changes
- **Universal market scanner** — scans all Kalshi markets by category: weather, economic, sports
- **Fee model** — `FEE_CENTS=7` applied to Kelly sizing and edge threshold
- **Stats tracker** (`stats.py`) — per-signal P&L and win-rate tracking
- **FOMC arb** — monotonicity violation scanner across T-level strikes
- **`SCHEMA.md`** — Redis key and message schema documentation

---

## [0.4.0] — 2026-03-05  First live trading session

### Changes
- **Live mode flag** — `KALSHI_PAPER_TRADE=false` enables real order placement
- **Kelly fraction** — 25% Kelly (quarter-Kelly) as default; configurable via `KALSHI_KELLY_FRACTION`
- **Exposure gates** — per-market (5%) and total (30%) caps; daily drawdown halt at 20%
- **`SETUP_CHECKLIST.md`** — step-by-step deployment guide

---

## [0.3.0] — 2026-02-20  Risk management + backtester

### Changes
- **`kalshi_bot/risk.py`** — `RiskManager`: Kelly sizing, spread gate, exposure caps, daily drawdown halt
- **Backtester** (`kalshi_bot/models/backtester.py`) — historical signal replay with P&L attribution
- **FOMC directional v1** — FRED-anchored fair value vs Kalshi market price

---

## [0.2.0] — 2026-02-10  Async client + Kalshi WebSocket

### Changes
- **`kalshi_bot/client.py`** — sync + async (httpx) Kalshi REST client with retry/backoff
- **`kalshi_bot/websocket.py`** — Kalshi WebSocket price feed
- **`dashboard.py`** — Streamlit trading control panel

---

## [0.1.0] — 2026-02-01  Initial single-node Kalshi bot

### Features
- Single-process FOMC prediction market scanner
- RSA-signed Kalshi API authentication
- Paper trading mode
- Basic signal generation from FRED + Kalshi prices
- Logging, retries, configurable thresholds via `.env`
