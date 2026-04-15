#!/bin/bash
# start_edgepulse.sh — Start the full EdgePulse stack on the DO Droplet (Intel node)
#
# What this starts:
#   1. Docker Compose  — Redis + Prometheus + Grafana
#   2. edgepulse       — EdgePulse Intel node (signal generation + bus)
#   3. llm             — Claude LLM policy agent (runs every LLM_INTERVAL_HOURS)
#
# Prereqs:
#   pip install -r requirements.txt   (run from Kalshi_bot/ with venv active)
#   docker compose (v2) installed
#   .env populated (POLYGON_API_KEY, ANTHROPIC_API_KEY, KALSHI_API_KEY_ID, ...)
#
# Usage:
#   cd ~/Kalshi_bot/EdgePulse-Trader
#   bash start_edgepulse.sh
#
# Attach to logs:
#   screen -r edgepulse      # Intel node
#   screen -r llm            # LLM agent
#   docker compose logs -f   # Redis / Prometheus / Grafana

set -euo pipefail
cd "$(dirname "$0")"          # run from EdgePulse-Trader/ regardless of cwd

VENV="${HOME}/Kalshi_bot/.venv/bin/activate"
LOG_DIR="${HOME}/Kalshi_bot/output/logs"
mkdir -p "$LOG_DIR"

echo "──────────────────────────────────────────────"
echo "  EdgePulse stack start — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "──────────────────────────────────────────────"

# ── 1. Monitoring stack (Redis + Prometheus + Grafana) ────────────────────────
echo "[1/3] Starting Docker Compose monitoring stack..."
if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    docker compose up -d
    echo "      Redis:      redis://localhost:6379"
    echo "      Prometheus: http://localhost:9090  (SSH tunnel to view)"
    echo "      Grafana:    http://$(curl -s ifconfig.me 2>/dev/null || echo '<DO-IP>'):3000"
else
    echo "      WARNING: docker/docker-compose not found."
    echo "      If Redis is already running elsewhere, this is fine."
    echo "      Install Docker: https://docs.docker.com/engine/install/ubuntu/"
fi

# ── 2. Kill existing EdgePulse screens ───────────────────────────────────────
echo "[2/3] Stopping any existing EdgePulse screen sessions..."
screen -S edgepulse -X quit 2>/dev/null || true
screen -S llm       -X quit 2>/dev/null || true
sleep 2

# ── 3. Intel node ─────────────────────────────────────────────────────────────
echo "[3/3] Starting EdgePulse Intel node..."
screen -dmS edgepulse bash -c "
    source '${VENV}'
    cd '$(pwd)'
    export MODE=intel
    export NODE_ID=intel-do-nyc3
    python3 edgepulse_launch.py 2>&1 | tee '${LOG_DIR}/edgepulse.log'
"

# ── 4. LLM policy agent ───────────────────────────────────────────────────────
echo "[4/4] Starting Claude LLM policy agent (loop mode)..."
screen -dmS llm bash -c "
    source '${VENV}'
    cd '$(pwd)'
    python3 llm_agent.py --loop 2>&1 | tee '${LOG_DIR}/llm_agent.log'
"

sleep 2
echo ""
echo "✅ All services started:"
screen -ls | grep -E "edgepulse|llm" || true
echo ""
echo "  Intel logs:   screen -r edgepulse"
echo "  LLM logs:     screen -r llm"
echo "  Docker logs:  docker compose logs -f"
echo "  Grafana:      http://$(curl -s ifconfig.me 2>/dev/null || echo '<DO-IP>'):3000  (admin / changeme)"
echo "  Metrics:      http://localhost:9091/metrics"
echo "  Redis CLI:    redis-cli"
