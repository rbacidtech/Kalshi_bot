# EdgePulse-Trader — Setup Checklist

Step-by-step guide to go from a fresh server to a running EdgePulse stack.
Work through each section in order; each section's prereqs are the previous section.

---

## Phase 0 — Prerequisites

- [ ] QuantVPS account created — Chicago node provisioned (Ubuntu 22.04)
- [ ] DigitalOcean Droplet created — 4 CPU / 8 GB RAM, NYC3, Ubuntu 22.04
- [ ] Polygon.io Personal plan ($200/mo) active — API key obtained
- [ ] Kalshi account created — API key + RSA private key downloaded
- [ ] Anthropic API key obtained (for LLM agent)
- [ ] FRED API key obtained (free at fred.stlouisfed.org) — optional but recommended
- [ ] Git repo cloned to both nodes: `git clone <your-repo> /root/EdgePulse`

---

## Phase 1 — DO Droplet (Intel node) — one-time setup

### 1.1 System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git curl ufw fail2ban
```

### 1.2 Docker (for Redis + Prometheus + Grafana)

```bash
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker $USER
newgrp docker
docker compose version   # must be v2+
```

### 1.3 Python venv

```bash
cd /root/EdgePulse
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1.4 Environment file

```bash
cd /root/EdgePulse
cp .env.example .env
nano .env   # fill in all required values (see comments in the file)
```

Required keys for the Intel node:
- `POLYGON_API_KEY` — BTC-USD candles (optional; Coinbase Exchange is free fallback)
- `ANTHROPIC_API_KEY` — LLM policy agent
- `FRED_API_KEY` — FOMC anchor + macro series
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` — required for live trading
- `REDIS_URL=redis://:<PASSWORD>@localhost:6379/0`
- `MODE=intel`
- `NODE_ID=intel-do-nyc3`
- `DATABASE_URL=postgresql://...` — Postgres for P&L snapshots

### 1.5 Private key (live Kalshi)

```bash
cp /path/to/kalshi_private_key.pem /root/EdgePulse/private_key.pem
chmod 600 /root/EdgePulse/private_key.pem
```

### 1.6 Firewall (UFW)

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow from <YOUR_HOME_IP> to any port 3000   # Grafana
sudo ufw allow from 172.93.213.88 to any port 6379    # Redis — Exec IP only
sudo ufw enable
```

### 1.7 Start the stack (systemd)

```bash
docker compose -f /root/EdgePulse/docker-compose.yml up -d  # Redis, Postgres, Prometheus, Grafana
cp /root/EdgePulse/edgepulse.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now edgepulse
```

Verify:
```bash
systemctl status edgepulse                  # should be active (running)
journalctl -u edgepulse -f                  # live log
docker compose ps                           # all containers Up
docker exec $(docker ps -q -f name=redis) redis-cli -a "<PASSWORD>" ping   # PONG
curl -s localhost:9091/metrics | head -5    # Prometheus scrape
```

### 1.8 Grafana setup

1. Open `http://<DO-IP>:3000` (admin / `Ep2026!xK9mP`)
2. Dashboards → EdgePulse → EdgePulse Overview should auto-provision
3. If not: Import → Upload `grafana-provisioning/dashboards/edgepulse_dashboard.json`

---

## Phase 2 — QuantVPS (Exec node) — one-time setup

### 2.1 System packages + venv

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git
cd /root/EdgePulse
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2 Environment file

```bash
cp /root/EdgePulse/.env.example /root/EdgePulse/.env
nano /root/EdgePulse/.env
```

Required keys for the Exec node (different from Intel — do NOT copy Intel's .env):
- `REDIS_URL=redis://:<PASSWORD>@167.71.27.43:6379/0`  ← Intel node Redis
- `MODE=exec`
- `NODE_ID=exec-qvps-chi`
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`
- `KALSHI_PAPER_TRADE=true` to start; set `false` for live
- `COINBASE_API_KEY_NAME` + `COINBASE_PRIVATE_KEY_PATH`

### 2.3 Install systemd service on Exec

```bash
cp /root/EdgePulse/edgepulse-exec.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now edgepulse-exec
systemctl status edgepulse-exec
```

### 2.4 Update Prometheus on Intel

Edit `/root/EdgePulse/prometheus.yml`:
```yaml
  - job_name: "edgepulse-exec"
    static_configs:
      - targets: ["172.93.213.88:9092"]
```

Then reload:
```bash
curl -X POST http://localhost:9090/-/reload
```

---

## Phase 3 — React Dashboard

The SaaS dashboard is a React app served via FastAPI on port 8502.

```bash
# Build the React frontend (Intel node, one-time)
cd /root/EdgePulse/dashboard
npm install
npm run build

# Start FastAPI backend
cd /root/EdgePulse
source .venv/bin/activate
uvicorn api.main:app --host 0.0.0.0 --port 8502
```

Access at `http://<DO-IP>:8502`. Protect with `ufw allow from <YOUR_IP> to any port 8502`.

---

## Phase 4 — Verify end-to-end (paper mode)

- [ ] `systemctl status edgepulse` active (running) on Intel
- [ ] `ssh quantvps "systemctl status edgepulse-exec"` active on Exec
- [ ] `journalctl -u edgepulse -f` shows Intel cycles completing without errors
- [ ] Grafana dashboard shows signal counters incrementing
- [ ] `docker exec ... redis-cli hgetall ep:config` shows LLM keys like `llm_scale_factor`
- [ ] `docker exec ... redis-cli xlen ep:signals` is growing (Intel publishing)
- [ ] `docker exec ... redis-cli xlen ep:executions` is growing (Exec processing)
- [ ] Paper fills visible in `docker exec ... redis-cli hgetall ep:positions`

---

## Phase 5 — Go live checklist

Only proceed after 48+ hours of clean paper-mode operation.

- [ ] `KALSHI_PAPER_TRADE=false` set in Exec `.env`
- [ ] `COINBASE_PAPER=false` set in Exec `.env` (fund Coinbase first)
- [ ] Real Kalshi API key + private key installed on Exec node
- [ ] Starting balance is an amount you are prepared to lose
- [ ] Grafana alerts configured for drawdown > 10%
- [ ] Telegram alerts configured (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHANNEL_ID`)
- [ ] `KALSHI_KELLY_FRACTION=0.10` (start at ¼ Kelly until confident)
- [ ] Emergency stop documented: `docker exec ... redis-cli -a "<PASS>" hset ep:config HALT_TRADING 1`

---

## Day-to-day operations

| Task | Command |
|---|---|
| View Intel logs (live) | `journalctl -u edgepulse -f` |
| View Exec logs (live) | `ssh quantvps "journalctl -u edgepulse-exec -f"` |
| Deploy code update | `git pull && ./deploy.sh` |
| Restart Intel only | `./deploy.sh --intel` |
| Restart Exec only | `./deploy.sh --exec` |
| Emergency stop | `docker exec $(docker ps -q) redis-cli -a "<PASS>" hset ep:config HALT_TRADING 1` |
| Resume trading | `docker exec $(docker ps -q) redis-cli -a "<PASS>" hset ep:config HALT_TRADING 0` |
| View open positions | `docker exec $(docker ps -q) redis-cli -a "<PASS>" hgetall ep:positions` |
| View balances | `docker exec $(docker ps -q) redis-cli -a "<PASS>" hgetall ep:balance` |
| Check Redis health | `docker exec $(docker ps -q) redis-cli -a "<PASS>" ping && info memory` |
| Check Docker stack | `docker compose ps && docker compose logs -f` |
| Manual LLM run | `source .venv/bin/activate && python3 llm_agent.py` |
