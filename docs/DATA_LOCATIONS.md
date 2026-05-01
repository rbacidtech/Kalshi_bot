# EdgePulse — Where Every P&L / Position / Trade Number Lives

**Purpose:** Stop re-orienting on every audit. This is the single map of every data sink that holds a number related to trades, positions, or P&L — who writes it, who reads it, and what gap each one has.

**Last verified:** 2026-05-01 (during the APR30 weather-settlement audit).

---

## TL;DR — what to trust

| Question | Authoritative source |
|---|---|
| **What is my actual cash + portfolio worth right now?** | Kalshi API: `GET /trade-api/v2/portfolio/balance` |
| **Did a specific market settle, and what was the payout?** | Kalshi API: `GET /trade-api/v2/portfolio/settlements` |
| **What positions does Kalshi think I hold?** | Kalshi API: `GET /trade-api/v2/portfolio/positions` |
| What our internal book *thinks* I hold | Redis `ep:positions` (often in sync, occasionally drifts) |
| What our dashboard / `ep:performance` shows | **Trades.csv-derived → blind to settlement payouts** (see Gap #1) |

> **If the dashboard number disagrees with the Kalshi balance, the Kalshi balance wins.** Always. Until the audit gaps below are closed, the dashboard is a lower-bound estimate that systematically *under*-reports both wins and losses on any market that resolves at expiry rather than via our own exit logic.

---

## Redis keys

### `ep:positions` — HASH (ticker → JSON)
Internal book of open positions.

| | File:line |
|---|---|
| **Writers** | `ep_bus.py:344` `set_position()` (canonical setter); `ep_exec.py:1510,1526` (sync adopts/updates from Kalshi); `ep_exec.py:2742,2762` (fill-poll updates contracts); `ep_positions.py:89,149` (PositionStore) |
| **Deleters** | `ep_bus.py:346` `delete_position()`; `ep_positions.py:96` `PositionStore.close()`; called from many places in `ep_exec.py` (1646, 1853, 1909, 2484, 2551, 2580, etc.) |
| **Readers** | `ep_bus.py:359`; `ep_exec.py:293,1458,1721,2697`; `ep_intel.py:1072`; `api/routers/positions.py:224` |

Schema fields: `side`, `contracts`, `contracts_filled`, `entry_cents`, `fair_value`, `meeting`, `outcome`, `close_time`, `model_source`, `entered_at`, `pending`, `order_id`, `fill_confirmed`, `high_water_pnl`, `tranche_done`, `asset_class`, `pending_exit`, `exit_order_id`, etc. Full schema in `SCHEMA.md`.

**`entry_cents` invariant:** ALWAYS the YES-market price × 100, regardless of side. Exit P&L formula relies on this.

### `ep:balance` — HASH (node_id → JSON)
| | File:line |
|---|---|
| **Writers** | `ep_bus.py:399-404` `set_balance()`; called from `ep_intel.py` after `/portfolio/balance` polls |
| **Readers** | `ep_bus.py:406`; `ep_intel.py:1056`; `api/routers/positions.py:127,197` |

JSON: `{balance_cents, portfolio_value_cents, mode, ts_us}`.

### `ep:performance` + `ep:performance:7` / `:30` / `:90` — STRING (JSON, TTL 25h)
| | File:line |
|---|---|
| **Writer** | `ep_exec.py:3727` `_performance_publisher_loop()` — runs hourly, **reads `output/trades.csv` and pairs entry+exit rows** to compute realized P&L. Writes 7/30/90 day variants + a backward-compat `ep:performance` mirror at line 3731. |
| **Readers** | `ep_intel.py:1091-1094,2849`; `api/routers/performance.py:38-40` |

**⚠ This is the dashboard's "win rate / PnL / by-strategy" source. It is downstream of `trades.csv`, not the Kalshi exchange. Anything that doesn't write a `trades.csv` exit row is invisible here.**

### `ep:divergence` — HASH
Hourly edge-capture ratio (realized vs expected) over a 7-day window of completed trades.

| | File:line |
|---|---|
| **Writer** | `ep_exec.py:3809-3816` `_divergence_monitor_loop()` (hourly) — reads trades.csv |
| **Readers** | None directly — used for ops alerting |

Same blind spot as `ep:performance` (trades.csv-derived).

### `ep:resolutions` — HASH (series → JSON outcomes history)
Used by `ep_behavioral.py` for recency-bias adjustment. **Not a P&L source.**

| | File:line |
|---|---|
| **Writer** | `ep_resolution_db.py:544` (poll loop, every 300s) |
| **Reader** | `ep_behavioral.py:89` |

### `ep:signals` — STREAM (Intel → Exec)
| | File:line |
|---|---|
| **Writer** | `ep_bus.py:105` `publish_signal()` |
| **Reader (consumer group `exec-consumers`)** | `ep_bus.py:134-229` → `ep_exec.py:1321-1365` |

### `ep:executions` — STREAM (Exec → Intel)
Fill confirmations and rejections.

| | File:line |
|---|---|
| **Writer** | `ep_bus.py:246` `publish_execution()` |
| **Reader (consumer group `intel-consumers`)** | `ep_bus.py:264-316` → `ep_intel.py:2801-2823` |

**⚠ The intel-side consumer increments an in-memory Prometheus counter (`metrics.add_pnl(...)`) and logs "Fill confirmed". It does NOT write to Postgres or trades.csv.** So an `ExecutionReport` carrying `realized_pnl_cents` from a settlement is not durable accounting.

---

## Postgres tables (`edgepulse` database)

### `pnl_snapshots`
Hourly point-in-time snapshot of balance + open-position state.

| | File:line |
|---|---|
| **Writer** | `ep_pnl_snapshots.py:81-89` `write_snapshot()`, called from `ep_intel.py:1099-1105` `_write_pnl_snapshot()` inside the heartbeat loop. |
| **Source of `realized_pnl_cents` field** | Reads `ep:performance` (which reads trades.csv). Inherits Gap #1. |
| **Readers** | `api/routers/performance.py:88-107` (history); `api/routers/performance.py:184-198` (equity curve); `api/routers/controls.py:190-222` (daily Δ); `api/routers/ws.py:354-367` (dashboard WS) |

### `position_history`
Closed positions with realized P&L. Schema: `(hist_id, entry_exec_id, ticker, side, contracts, entry_cents, exit_cents, realized_pnl_cents, exit_reason, entered_at, exited_at, strategy)`.

| | File:line |
|---|---|
| **Writers (5 sites in ep_exec.py)** | 1601 (position-sync detects vanished), 2649 (fill-poll exit), 2781 (fill-confirm exit), 3240 (orphan reconcile), 3289 (orphan reconcile, second variant) |
| **NOT written by** | `ep_exec.py:1873` Post-resolution cleanup — **this is Gap #1** |

### `executions`, `signals`, `audit_logs`, `market_snapshots`, `balance_snapshots`, `llm_decisions`
Audit trails for the corresponding Redis streams/messages. Not a P&L source on their own; used for forensics and reconciliation.

---

## Files

### `output/trades.csv`
**The single source of truth for `ep:performance`.** Append-only. Columns: `timestamp, ticker, meeting, outcome, side, action, contracts, price_cents, fair_value, edge, confidence, model_source, order_id, mode`.

| | File:line |
|---|---|
| **Writer** | `kalshi_bot/executor.py:99-120` `_log_trade()` — fires from entry confirmation AND from exit-fill confirmation in `ep_exec.py:2768-2789` `log_exit_fill()`, AND from position-sync settlement at `ep_exec.py:1635-1637`. |
| **NOT written by** | `ep_exec.py:1873` Post-resolution cleanup. The cleanup tries to place an exit order; on `HTTP 409 market_closed` the order fails and `_log_trade` is never reached. **Gap #1.** |
| **Readers** | `ep_resolution_db.py:51-155` `_load_completed_trades()`; `ep_exec.py:3772-3802` divergence monitor; `ep_exec.py:3727+` performance publisher; `ep_backtest.py:45-51`; `api/routers/performance.py:132` (recent-trades endpoint) |

**Known quirks (in memory, also here for the audit):**
- Duplicate exit rows on illiquid markets (multiple poll cycles before the fill confirms).
- Long-dated FOMC fills can take many minutes to land their exit row.

### `output/resolutions.db` (SQLite)
**This is NOT a P&L source.** Drives `ep_behavioral.py` recency-bias adjustment.

| | File:line |
|---|---|
| **Writer** | `ep_resolution_db.py:464-489` `record_resolution()` + `record_trade_outcome()` from `poll_resolutions_loop()` (300s). |
| **Reader** | `ep_resolution_db.py:451-457` `get_outcome()`. Also referenced by Post-resolution cleanup fallback at `ep_exec.py:1889`. |
| **Last write** | 2026-04-17 (15+ days stale as of audit). **Gap #2.** |

### `output/paper_positions.json`, `output/bot.log`
Legacy paper-trading state and ad-hoc log. Not used in live accounting.

### `output/logs/kalshi_bot.jsonl` (and rotated `.YYYY-MM-DD` siblings)
**The actual log output**, JSONL. `journalctl -u edgepulse-*` returns no entries — the service writes here directly via Python logging. Always grep these files.

---

## Kalshi exchange API — the only ground truth

| Endpoint | Called from | Purpose |
|---|---|---|
| `GET /portfolio/balance` | `ep_intel.py:1589`, `kalshi_bot/portfolio.py:32` | Cash + portfolio_value, written to `ep:balance` |
| `GET /portfolio/positions?limit=200` | `ep_exec.py:1445` (every 30 min), `ep_exec.py:3445` (orphan reconcile), `kalshi_bot/executor.py:519,572` | Adopted into `ep:positions` |
| `GET /portfolio/orders/{order_id}` | `ep_exec.py:2707-2708` (fill_poll, every 60s) | Resting-order fill status |
| `GET /markets/{ticker}` | `ep_resolution_db.py:513-514`, `ep_exec.py:1578` | Resolution status + result |
| **`GET /portfolio/settlements`** | **NOWHERE.** Never called. **Gap #3 — no settlement-reconciliation reader exists.** |

---

## The gaps — where numbers go missing

### Gap #1 — Post-resolution cleanup never writes accounting rows (the big one)

**Code:** `ep_exec.py:1873-1930`

**Chain:**
1. Kalshi market settles at expiry (e.g. APR30 weather at 12:02 UTC on 2026-05-01).
2. EdgePulse exit-management loop sees `pos.close_time` was >2h ago.
3. Calls `executor._exit_position(...)` — tries to place a closing order.
4. Order fails with `HTTP 409 {"error":{"code":"market_closed"}}` because the market has already closed/settled.
5. Code calls `await positions.close(ticker)` — **deletes from `ep:positions` anyway.**
6. Code publishes an `ExecutionReport` to `ep:executions` with `realized_pnl_cents`.
7. Intel-side consumer at `ep_intel.py:2801` reads the report → `metrics.add_pnl(...)` (in-memory Prometheus only) + log line "Fill confirmed". **No durable write.**
8. **`trades.csv` exit row: NEVER WRITTEN** (the executor's `_log_trade` only fires after a successful order placement).
9. **`position_history` row: NEVER WRITTEN** (line 1873 is the only ep:positions-deletion site that skips `_audit_writer()`. The other 5 sites write it; this one doesn't).
10. Hourly `_performance_publisher_loop` recomputes `ep:performance` from `trades.csv` → settlement P&L is not in the totals.
11. Hourly `_write_pnl_snapshot` writes `pnl_snapshots.realized_pnl_cents` from `ep:performance` → Postgres history is also blind.
12. Real cash hits the Kalshi account → `ep:balance` reflects it on next poll.

**Net effect:** `ep:balance` rises (real money) while `ep:performance.total_pnl_cents` and `pnl_snapshots.realized_pnl_cents` stay flat or drift down. The dashboard understates wins more than losses (because winners more often run to settlement; losers more often hit our own exit logic), so it skews systematically pessimistic.

**Concrete instance from 2026-05-01:** APR30 weather/GDP/NBA settlements totalled ~$194 in revenue against ~$212 in cost basis = ~-$18 net. The internal pnl_snapshots showed -$26 over the same window (which includes non-settlement exits like `cut_loss_intel`), so the recent settlement-driven drift is small. **However**, the audit asymmetry is structural: every settlement that fires through `Post-resolution cleanup` bypasses durable accounting, and over weeks/months this drift compounds. (NOTE: an earlier draft of this doc claimed ~$252 of unrecorded P&L; that was wrong — it conflated a $300 user deposit with settlement payouts. The defect is real; the magnitude in any given window depends on actual settlement P&L, not on cash-balance changes.)

**Fix vector (NOT to be deployed without explicit approval):** add `_audit_writer().write("position_history", {...})` and `executor._log_trade(...)` calls to the success path of `ep_exec.py:1873-1930`, mirroring the structure used at line 1601-1637.

### Gap #2 — `resolutions.db` writer dead since 2026-04-17

**Code:** `ep_resolution_db.py:495-561` `poll_resolutions_loop`

**Effect:** `ep_behavioral.py` recency-bias adjustment runs on stale outcomes. Does NOT directly affect P&L (separate from Gap #1).

**Investigation pending:** the loop is scheduled at `ep_exec.py:4057` and the log shows multiple "Resolution poller started" messages, suggesting either repeated restarts or the call site being hit more than once. `db.record_resolution()` calls would be visible in the kalshi_bot.jsonl logs as `"Resolution: <ticker> → <yes|no>"` — none have appeared since 2026-04-17. Root cause unknown; could be:
- Loop crashed silently and the outer `while True` swallows the exception (line 555 catches `Exception` at `log.warning` only).
- All open positions were closed-via-cleanup before the 300s poll could see them as resolved.
- SQLite write is failing with a non-raising error.

### Gap #3 — Nobody calls `/portfolio/settlements`

The Kalshi API exposes a settlement-history endpoint that gives definitive `revenue` and `fee_cost` for every settled market. **Zero references in our codebase.** Adding a settlement reconciliation loop that reads this endpoint and writes synthetic `position_history` rows would fix Gap #1 retroactively (backfill) AND going forward (live reconciliation).

---

## How to reconcile manually (until gaps are fixed)

```bash
# 1. Real cash + portfolio value (the truth)
/root/EdgePulse/.venv/bin/python3 -c "
import os; os.chdir('/root/EdgePulse'); import ep_config
from kalshi_bot import config as cfg
from kalshi_bot.client import KalshiClient
from kalshi_bot.auth import KalshiAuth
auth = KalshiAuth(api_key_id=cfg.API_KEY_ID, private_key_path=cfg.PRIVATE_KEY_PATH)
c = KalshiClient(base_url=cfg.BASE_URL, auth=auth, timeout=cfg.HTTP_TIMEOUT,
                 max_retries=cfg.MAX_RETRIES, backoff=cfg.RETRY_BACKOFF, concurrency=cfg.CONCURRENCY)
print(c.get('/portfolio/balance'))
"

# 2. Settlements over the last N markets
/root/EdgePulse/.venv/bin/python3 -c "
import os; os.chdir('/root/EdgePulse'); import ep_config
from kalshi_bot import config as cfg
from kalshi_bot.client import KalshiClient
from kalshi_bot.auth import KalshiAuth
auth = KalshiAuth(api_key_id=cfg.API_KEY_ID, private_key_path=cfg.PRIVATE_KEY_PATH)
c = KalshiClient(base_url=cfg.BASE_URL, auth=auth, timeout=cfg.HTTP_TIMEOUT,
                 max_retries=cfg.MAX_RETRIES, backoff=cfg.RETRY_BACKOFF, concurrency=cfg.CONCURRENCY)
import json
print(json.dumps(c.get('/portfolio/settlements', params={'limit':200}), indent=2))
" | less

# 3. What our internal book says
SOCK=/run/redis/redis.sock
redis-cli -s $SOCK get ep:performance:7  | python3 -m json.tool
redis-cli -s $SOCK get ep:performance:30 | python3 -m json.tool
redis-cli -s $SOCK hgetall ep:balance

# 4. Postgres equity curve (also blind to settlements but useful for trend)
sudo -u postgres psql -d edgepulse -c "
  SELECT date_trunc('day', ts) AS day,
         MAX(realized_pnl_cents) AS realized_eod_cents,
         MAX(balance_cents)      AS bal_max_cents,
         MAX(position_count)     AS positions_max
  FROM pnl_snapshots
  WHERE ts >= NOW() - INTERVAL '14 days'
  GROUP BY 1 ORDER BY 1;"
```

---

## Service / log topology (where to look when nothing else helps)

- **Primary logs (always grep these, NOT journalctl):**
  - `output/logs/kalshi_bot.jsonl` (live + last 24h structured JSON)
  - `output/logs/kalshi_bot.jsonl.YYYY-MM-DD` (rotated daily files)
  - `output/logs/exec.log` (rotated, less detailed)
- `journalctl -u edgepulse-*` returns "No entries" — the services do not log to journald.
- Services: `edgepulse-intel`, `edgepulse-exec`, `edgepulse-api` (all `active`, run as root, EnvironmentFile=`/etc/edgepulse/edgepulse.env`).

---

*Audit history: created 2026-05-01 after the APR30 weather settlement reconciliation surfaced a ~$252 unrecorded P&L gain. Update this doc whenever a writer/reader site is added, moved, or removed — the file:line references must stay current to be useful.*
