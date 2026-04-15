#!/bin/bash
# start.sh — EdgePulse stack entrypoint.
#
# Starts:
#   1. Docker Compose  — Redis + Prometheus + Grafana
#   2. edgepulse       — Intel node (signal generation, bus publish)
#   3. llm             — Claude LLM policy agent
#   4. dash            — Streamlit dashboard (port 8501)
#
# Prereqs:
#   pip install -r requirements.txt  (run from ~/EdgePulse with .venv active)
#   docker compose v2 installed
#   .env populated: POLYGON_API_KEY, ANTHROPIC_API_KEY, KALSHI_API_KEY_ID, ...
#
# Usage:
#   bash ~/EdgePulse/start.sh
#
# Attach to logs:
#   screen -r edgepulse   # Intel node
#   screen -r llm         # LLM agent
#   screen -r dash        # Streamlit dashboard
#   docker compose -f ~/EdgePulse/docker-compose.yml logs -f

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"   # /root/EdgePulse regardless of cwd

VENV="${ROOT}/.venv/bin/activate"
LOG_DIR="${ROOT}/output/logs"
mkdir -p "$LOG_DIR"

echo "──────────────────────────────────────────────"
echo "  EdgePulse stack start — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "──────────────────────────────────────────────"

# ── 1. Stop any existing sessions ─────────────────────────────────────────────
echo "[1/4] Stopping existing screen sessions..."
for s in edgepulse llm dash bot watchdog; do
    screen -S "$s" -X quit 2>/dev/null || true
done
sleep 2

# ── 2. Docker Compose — Redis + Prometheus + Grafana ──────────────────────────
echo "[2/4] Starting Docker Compose monitoring stack..."
if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    docker compose -f "${ROOT}/docker-compose.yml" up -d
    echo "      Redis:      redis://localhost:6379"
    echo "      Prometheus: http://localhost:9090"
    echo "      Grafana:    http://$(curl -s ifconfig.me 2>/dev/null || echo '<SERVER-IP>'):3000"
else
    echo "      WARNING: docker/docker-compose not found — skipping."
    echo "      If Redis is already running, this is fine."
fi

# ── 3. Intel node ─────────────────────────────────────────────────────────────
echo "[3/4] Starting EdgePulse Intel node..."
screen -dmS edgepulse bash -c "
    source '${VENV}'
    cd '${ROOT}'
    export MODE=intel
    export NODE_ID=intel-do-nyc3
    python3 edgepulse_launch.py 2>&1 | tee '${LOG_DIR}/edgepulse.log'
"

# ── 4. LLM policy agent ───────────────────────────────────────────────────────
echo "[4/4] Starting Claude LLM policy agent..."
screen -dmS llm bash -c "
    source '${VENV}'
    cd '${ROOT}'
    python3 llm_agent.py --loop 2>&1 | tee '${LOG_DIR}/llm_agent.log'
"

# ── 5. Streamlit dashboard ────────────────────────────────────────────────────
screen -dmS dash bash -c "
    source '${VENV}'
    cd '${ROOT}'
    streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0 \
        2>&1 | tee '${LOG_DIR}/dashboard.log'
"

sleep 2
echo ""
echo "All services started:"
screen -ls | grep -E "edgepulse|llm|dash" || true
echo ""
echo "  Intel logs:   screen -r edgepulse"
echo "  LLM logs:     screen -r llm"
echo "  Dashboard:    screen -r dash"
echo "  Grafana:      http://$(curl -s ifconfig.me 2>/dev/null || echo '<SERVER-IP>'):3000  (admin / changeme)"
echo "  Dashboard:    http://$(curl -s ifconfig.me 2>/dev/null || echo '<SERVER-IP>'):8501"
echo "  Metrics:      http://localhost:9091/metrics"
echo "  Redis CLI:    redis-cli"
