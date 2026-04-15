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
- [ ] Git repo cloned to both nodes: `git clone <your-repo> ~/Kalshi_bot`

---

## Phase 1 — DO Droplet (Intel node) — one-time setup

### 1.1 System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git screen curl ufw
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
cd ~/Kalshi_bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1.4 Environment file

```bash
cd ~/Kalshi_bot/EdgePulse-Trader
cp .env.example .env
nano .env   # fill in all required values (see comments in the file)
```

Required keys for the Intel node:
- `POLYGON_API_KEY` — BTC-USD candles
- `ANTHROPIC_API_KEY` — LLM policy agent
- `FRED_API_KEY` — FOMC anchor (optional)
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` — leave blank for paper mode
- `REDIS_URL=redis://localhost:6379/0`
- `MODE=intel`
- `NODE_ID=intel-do-nyc3`

### 1.5 Private key (if using live Kalshi)

```bash
cp /path/to/your/kalshi_private_key.pem ~/Kalshi_bot/private_key.pem
chmod 600 ~/Kalshi_bot/private_key.pem
```

### 1.6 Firewall

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow from <YOUR_HOME_IP> to any port 3000   # Grafana
sudo ufw allow from <QUANT_VPS_IP> to any port 6379   # Redis (if Exec reads it)
sudo ufw enable
```

### 1.7 Start the stack

```bash
cd ~/Kalshi_bot/EdgePulse-Trader
bash start_edgepulse.sh
```

Verify:
```bash
screen -ls                    # should show edgepulse + llm sessions
screen -r edgepulse           # attach to Intel node (Ctrl-A D to detach)
docker compose ps             # redis, prometheus, grafana all Up
redis-cli ping                # PONG
curl -s localhost:9091/metrics | head -5   # Prometheus metrics
```

### 1.8 Grafana setup

1. Open `http://<DO-IP>:3000` in your browser (admin / changeme)
2. Change the admin password immediately: Profile → Change Password
3. Dashboards → EdgePulse → EdgePulse Overview should auto-provision
4. If not auto-provisioned: Dashboards → Import → Upload `grafana-provisioning/dashboards/edgepulse_dashboard.json`

---

## Phase 2 — QuantVPS (Exec node) — one-time setup

### 2.1 System packages + venv (same as Phase 1.1–1.3)

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git screen
cd ~/Kalshi_bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2 Environment file

```bash
cd ~/Kalshi_bot/EdgePulse-Trader
cp .env.example .env
nano .env
```

Required keys for the Exec node:
- `REDIS_URL=redis://<DO-DROPLET-IP>:6379/0`  ← point at DO Droplet Redis
- `MODE=exec`
- `NODE_ID=exec-qvps-chi`
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` — required for live trading
- `KALSHI_PAPER_TRADE=true` to start; set to `false` when ready for live

### 2.3 Start Exec node

```bash
cd ~/Kalshi_bot/EdgePulse-Trader
screen -dmS exec bash -c "
    source ~/Kalshi_bot/.venv/bin/activate
    MODE=exec NODE_ID=exec-qvps-chi python3 edgepulse_launch.py 2>&1 | tee ~/Kalshi_bot/output/logs/exec.log
"
screen -r exec
```

### 2.4 Update Prometheus on DO Droplet

Edit `~/Kalshi_bot/EdgePulse-Trader/prometheus.yml`:
```yaml
  - job_name: "edgepulse-exec"
    static_configs:
      - targets: ["<QUANT_VPS_IP>:9092"]   # replace placeholder
```

Then reload:
```bash
curl -X POST http://localhost:9090/-/reload
```

---

## Phase 3 — Streamlit dashboard

The dashboard (`dashboard.py`) connects to Redis and provides a live trading control panel.

```bash
# On DO Droplet (or any machine that can reach Redis)
source ~/Kalshi_bot/.venv/bin/activate
cd ~/Kalshi_bot/EdgePulse-Trader
streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0
```

Access at `http://<DO-IP>:8501`. Protect with `ufw allow from <YOUR_IP> to any port 8501`.

---

## Phase 4 — Verify end-to-end (paper mode)

- [ ] `screen -r edgepulse` shows Intel cycles completing without errors
- [ ] `screen -r exec` shows Exec consuming signals from Redis
- [ ] `screen -r llm` shows LLM agent writing policy to `ep:config`
- [ ] Grafana dashboard shows BTC price updating + signal counters incrementing
- [ ] `redis-cli hgetall ep:config` shows LLM keys like `llm_scale_factor`
- [ ] `redis-cli xlen ep:signals` is growing (Intel publishing)
- [ ] `redis-cli xlen ep:executions` is growing (Exec processing)
- [ ] Paper fills appearing in `output/trades.csv`

---

## Phase 5 — Go live checklist

Only proceed after 48+ hours of clean paper-mode operation.

- [ ] `KALSHI_PAPER_TRADE=false` set in Exec node `.env`
- [ ] Real Kalshi API key + private key installed on Exec node
- [ ] Starting balance is an amount you are prepared to lose
- [ ] Grafana alerts configured for drawdown > 10%
- [ ] Phone alerts (Twilio) configured in `.env`
- [ ] `KALSHI_KELLY_FRACTION=0.10` (start at ¼ of normal Kelly until confident)
- [ ] Daily check: `redis-cli hget ep:config HALT_TRADING` returns 0
- [ ] Emergency stop documented: `redis-cli hset ep:config HALT_TRADING 1`

---

## Day-to-day operations

| Task | Command |
|---|---|
| View Intel logs | `screen -r edgepulse` |
| View Exec logs | `screen -r exec` (on QuantVPS) |
| View LLM logs | `screen -r llm` |
| Emergency stop | `redis-cli hset ep:config HALT_TRADING 1` |
| Resume trading | `redis-cli hset ep:config HALT_TRADING 0` |
| View open positions | `redis-cli hgetall ep:positions` |
| View LLM policy | `redis-cli hgetall ep:config` |
| Restart Intel stack | `bash start_edgepulse.sh` |
| Restart Exec node | `screen -S exec -X quit && screen -dmS exec ...` |
| Check Redis health | `redis-cli ping && redis-cli info memory` |
| Check Docker stack | `docker compose ps && docker compose logs -f` |
| Deploy code update | `git pull && bash start_edgepulse.sh` |
| Manual LLM run | `python3 llm_agent.py` (one-shot) |
