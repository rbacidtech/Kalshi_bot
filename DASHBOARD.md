# EdgePulse Dashboard — Operations Reference

Web dashboard for monitoring and controlling the EdgePulse trading system. React SPA + FastAPI backend served on port 8502.

---

## Access

| Method | URL |
|--------|-----|
| Direct (on server) | `http://localhost:8502` |
| Remote (SSH tunnel) | `ssh -L 8502:localhost:8502 root@172.93.213.88` → `http://localhost:8502` |
| Grafana metrics | `ssh -L 3000:localhost:3000 root@172.93.213.88` → `http://localhost:3000` |

Default admin account: set at first-run registration. JWT tokens expire in 24h; refresh tokens in 7 days.

---

## Stack

| Layer | Tech | Port | Location |
|-------|------|------|----------|
| React SPA | Vite + TypeScript + Tailwind | served via API | `/root/EdgePulse/dashboard/` |
| FastAPI backend | uvicorn, 2 workers | 8502 (localhost) | `/root/EdgePulse/api/` |
| Grafana | Docker | 3000 (localhost) | `infra/docker-compose.yml` |
| Prometheus | Docker | 9090 (localhost) | `infra/` |

---

## Pages

### Dashboard (/)

Main live view.

| Section | Data Source | What it Shows |
|---------|-------------|---------------|
| Balance cards | `GET /positions` | Kalshi cash balance, total deployed, unrealized P&L, position count |
| Coinbase card | `GET /positions/coinbase` | BTC balance + USD cash (requires COINBASE env vars) |
| Drawdown meter | `GET /controls/status` `.session_pnl` | Today's P&L vs daily drawdown limit |
| Positions table | `GET /positions` | All open positions: ticker, side, contracts, entry, current, P&L, age |
| 24h P&L sparkline | `GET /performance/history` | Hourly pnl_snapshots, last 24 hours |
| Activity feed | `GET /controls/activity` | Last 50 events from `ep:system` stream (fills, exits, cycle completions) |
| Halt/Resume button | `POST /controls/halt` or `/controls/resume` | Sets `HALT_TRADING` in `ep:config` — takes effect next Exec cycle |

**Polling:** 30-second refetch interval on all data.

---

### Controls (/controls)

Bot configuration and live status.

#### Config tab

Changes are written to `ep:config` (Redis hash) and `ep:bot:config` (UI state JSON).

| Field | Redis Key Written | Effective When |
|-------|------------------|----------------|
| Edge threshold | `override_edge_threshold` | Next Exec scan cycle (0–120s) |
| Max contracts | `override_max_contracts` | Next Exec scan cycle |
| Min confidence | `override_min_confidence` | Next Exec scan cycle |
| Kelly fraction | `llm_kelly_fraction` | Next Exec scan cycle |
| Max market exposure | `KALSHI_MAX_MARKET_EXPOSURE` | Next Exec scan cycle |
| Daily drawdown limit | `KALSHI_DAILY_DRAWDOWN_LIMIT` | Next Exec scan cycle |
| Strategy toggles (enable_fomc, etc.) | `ep:bot:config` UI state only | **Requires service restart** |

Direct Redis override (bypasses dashboard, immediate):
```bash
redis-cli -s /run/redis/redis.sock hset ep:config override_edge_threshold "0.12"
```

#### Status tab

| Indicator | Source | Stale Threshold |
|-----------|--------|-----------------|
| Node heartbeats (intel / exec) | `ep:system` stream HEARTBEAT events | >180s = stale (cadence = 60s) |
| WebSocket connected | `ep:health` hash `.sources.kalshi_ws.status` | per-cycle |
| Business issues | `ep:health` hash `.sources.business.error` | per-cycle |
| Session P&L | `pnl_snapshots` table | per-cycle |
| Halt state | `ep:config` `HALT_TRADING` | immediate |

**Known bug:** `controls.py:259` — node status dict is overwritten inside the iteration loop. Intel node data can be silently dropped if Redis iterates exec before intel. See KNOWN_GAPS.md.

#### AI Suggest tab

Sends current config + question to `POST /controls/ai-suggest`. Uses Claude Haiku via Anthropic API. Requires `ANTHROPIC_API_KEY` in environment.

---

### Performance (/performance)

Historical trade analytics.

| Section | Data Source | Notes |
|---------|-------------|-------|
| Summary cards | `GET /performance` | win_rate, total_pnl_cents, sharpe_daily, total_trades |
| Equity curve | `GET /performance/equity-curve` | Daily realized P&L from pnl_snapshots table |
| P&L distribution | `GET /performance` `.by_distribution` | Histogram of trade outcomes |
| Strategy breakdown | `GET /performance` `.by_strategy` | Per-signal-category win rate + P&L |
| Time range selector | query param `days=7|30|90` | Defaults to 30 days |

**Data dependency:** `GET /performance` reads `ep:performance:{days}` Redis key published by the Exec node. If exec is down, returns zeros — page shows "No completed trades."

---

### Advisor (/advisor)

LLM strategy monitor output.

| Section | Source Redis Key | Written By |
|---------|-----------------|------------|
| Strategy summary | `ep:advisor:status` `.summary` | `ep_advisor.py` (30-min cadence) |
| Strategy health grid | `ep:advisor:status` `.strategy_health` | `ep_advisor.py` |
| Kelly by strategy | `ep:advisor:status` `.kelly_by_strategy` | `ep_advisor.py` |
| Escalation reasons | `ep:advisor:status` `.escalation_reasons` | `ep_advisor.py` |
| Alert feed | `ep:alerts` Redis stream | `ep_advisor.py` + exec node |

**Dependency:** This page is read-only display of output from `edgepulse-advisor.service`. If that service is stopped, the page shows "No advisor run recorded yet." No advisor data is generated by the dashboard itself.

Check advisor service: `systemctl status edgepulse-advisor.service`

---

### Admin (/admin)

Requires admin role. User management and platform stats.

| Operation | Endpoint | Notes |
|-----------|----------|-------|
| List users | `GET /admin/users?page=&per_page=` | Paginated |
| Edit user tier / admin flag | `PATCH /admin/users/{id}` | Audit logged |
| Deactivate user | `DELETE /admin/users/{id}` | Soft delete (sets is_active=false) |
| Platform stats | `GET /admin/stats` | total_users, users_by_tier, total_deployed_cents |

---

### Keys (/keys)

API credential vault. Keys are AES-256-GCM encrypted at rest.

| Operation | Endpoint | Notes |
|-----------|----------|-------|
| Store key | `POST /keys` | Encrypts immediately; plaintext never persisted |
| List keys | `GET /keys` | Returns metadata only (exchange, created_at, last_used_at) |
| Delete key | `DELETE /keys/{exchange}` | Irreversible |
| Verify Kalshi | `GET /keys/kalshi/verify` | Calls `GET /portfolio/balance` on Kalshi |
| Verify Coinbase | `GET /keys/coinbase/verify` | **Stub — not implemented.** Returns "Verification failed." |

---

### Notifications (/notifications)

Alert stream from Redis `ep:notifications`.

| Filter | Values |
|--------|--------|
| Type | `fill`, `exit`, `alert`, `trade_alert`, `circuit_breaker`, `daily_summary` |
| Severity | `info`, `warning`, `critical` |

Notifications are written by the Exec node and the advisor service. Dashboard is display-only.

---

### Subscriptions (/subscriptions)

Multi-tenant tier management.

| Section | Endpoint |
|---------|----------|
| Current plan + usage | `GET /subscriptions/me` |
| Tier comparison | `GET /subscriptions/tiers` |

Volume meter: green < 70%, amber 70–90%, red > 90%. Upgrade requires manual admin action.

---

## Health Endpoint

```bash
curl http://localhost:8502/health
# {"api":"ok","redis":"ok","db":"ok","env":"development","status":"ok"}
```

Checks: Redis ping + PostgreSQL `SELECT 1`. Used by load balancers and monitoring.

---

## Service Management

```bash
# Check dashboard API
systemctl status edgepulse-api.service

# Restart API (needed after Python/config changes)
systemctl restart edgepulse-api.service

# View API logs
tail -f /var/log/edgepulse/api.log

# Rebuild frontend after React/TypeScript changes
cd /root/EdgePulse/dashboard && npm run build
# No API restart needed — FastAPI serves dist/ as static files
```

---

## Redis Keys the Dashboard Reads

| Key | Type | Written By | Used By |
|-----|------|------------|---------|
| `ep:positions` | hash | Exec node | Positions page |
| `ep:prices` | hash | Intel node (WebSocket) | Positions P&L calculation |
| `ep:balance` | hash | Intel node | Balance cards |
| `ep:config` | hash | Dashboard + Exec + LLM | Controls page (read + write) |
| `ep:bot:config` | string (JSON) | Dashboard | Controls page UI state |
| `ep:health` | hash | Intel + Exec nodes | Controls status tab |
| `ep:system` | stream | All nodes | Activity feed, heartbeats |
| `ep:performance` | string (JSON) | Exec node | Performance page |
| `ep:advisor:status` | string (JSON) | ep_advisor.py | Advisor page |
| `ep:alerts` | stream | ep_advisor.py | Advisor alert feed |
| `ep:notifications` | stream | Exec node | Notifications page |

---

## Database Tables the Dashboard Reads

| Table | Used By | Notes |
|-------|---------|-------|
| `users` | Auth, admin | JWT subject, role check |
| `api_keys` | Keys page | Encrypted key vault |
| `pnl_snapshots` | Performance page, sparkline | Written by `ep_pnl_snapshots.py` on each Intel heartbeat |
| `subscriptions` | Subscriptions page | Tier + volume tracking |
| `audit_log` | Admin page | Mutations to users/keys |

---

## Known Issues

| Issue | Severity | File | Workaround |
|-------|----------|------|------------|
| Node status dict overwrite — only last node in `ep:health` survives iteration | High | `api/routers/controls.py:259` | Check both nodes directly via Redis: `redis-cli -s /run/redis/redis.sock hgetall ep:health` |
| Coinbase key verify stub | Medium | `api/routers/keys.py:413` | Test Coinbase creds manually via Coinbase API |
| Strategy toggles (enable_fomc, etc.) don't apply until restart | Medium | `api/routers/controls.py` | `systemctl restart edgepulse-exec.service` after toggling |
| No manual position exit from dashboard | Medium | — | Use Redis directly or wait for exit checker |
| 30s polling — not real-time | Low | All pages | Acceptable for position-level data |

---

## Grafana Dashboards

Access via SSH tunnel: `ssh -L 3000:localhost:3000 root@172.93.213.88`

URL: `http://localhost:3000` — credentials: `admin` / (set in `GRAFANA_ADMIN_PASSWORD` env var, default `changeme`)

Provisioned dashboards in `/root/EdgePulse/grafana-provisioning/dashboards/edgepulse_dashboard.json`.

Datasource: Prometheus at `http://host.docker.internal:9090`, scraping metrics from port 9091 (Intel) and 9092 (Exec).

Key panels: signal flow counts, Kelly sizing by category, execution latency, rejection reasons, Redis stream depth, BTC RSI/z-score, Kalshi balance over time.
