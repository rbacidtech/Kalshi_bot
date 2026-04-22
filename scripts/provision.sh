#!/usr/bin/env bash
# Provision a fresh QuantVPS for EdgePulse single-box deployment.
# Run once as root after cloning the repo and installing Docker.
# Idempotent — safe to re-run.
set -euo pipefail

REPO_DIR="/root/EdgePulse"
SYSTEMD_DIR="/etc/systemd/system"
ENV_DIR="/etc/edgepulse"

echo "[provision] creating filesystem hierarchy..."

# Runtime dirs
mkdir -p /var/run/redis           # Redis Unix socket
chmod 755 /var/run/redis

# Persistent data (bind-mounted by docker-compose)
mkdir -p \
    /var/lib/edgepulse/redis \
    /var/lib/edgepulse/postgres \
    /var/lib/edgepulse/prometheus \
    /var/lib/edgepulse/grafana

# Logs
mkdir -p /var/log/edgepulse
touch \
    /var/log/edgepulse/intel.log \
    /var/log/edgepulse/exec.log \
    /var/log/edgepulse/api.log \
    /var/log/edgepulse/llm.log

# Backups
mkdir -p /var/backups/edgepulse

# Env file dir
mkdir -p "$ENV_DIR"

# ── Environment file ──────────────────────────────────────────────────────────
if [ ! -f "$ENV_DIR/edgepulse.env" ]; then
    echo "[provision] creating $ENV_DIR/edgepulse.env from template..."
    if [ -f "$REPO_DIR/.env" ]; then
        cp "$REPO_DIR/.env" "$ENV_DIR/edgepulse.env"
    else
        echo "# EdgePulse runtime config — fill in all values" > "$ENV_DIR/edgepulse.env"
        echo "KALSHI_API_KEY_ID=" >> "$ENV_DIR/edgepulse.env"
        echo "KALSHI_API_KEY_PEM=" >> "$ENV_DIR/edgepulse.env"
        echo "ANTHROPIC_API_KEY=" >> "$ENV_DIR/edgepulse.env"
        echo "FRED_API_KEY=" >> "$ENV_DIR/edgepulse.env"
        echo "POSTGRES_PASSWORD=" >> "$ENV_DIR/edgepulse.env"
        echo "GRAFANA_ADMIN_PASSWORD=" >> "$ENV_DIR/edgepulse.env"
        echo "REDIS_URL=unix:///var/run/redis/redis.sock" >> "$ENV_DIR/edgepulse.env"
        echo "LLM_MODEL=claude-sonnet-4-6" >> "$ENV_DIR/edgepulse.env"
    fi
    chmod 600 "$ENV_DIR/edgepulse.env"
    echo "[provision] ** edit $ENV_DIR/edgepulse.env before starting services **"
fi

# ── systemd service files ─────────────────────────────────────────────────────
echo "[provision] installing systemd units..."
cp "$REPO_DIR/systemd/edgepulse.target"          "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-intel.service"   "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-exec.service"    "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-api.service"     "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-llm.service"     "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-llm.timer"       "$SYSTEMD_DIR/"

systemctl daemon-reload
systemctl enable edgepulse.target

# ── Log rotation ──────────────────────────────────────────────────────────────
echo "[provision] installing logrotate config..."
cp "$REPO_DIR/logrotate.conf" /etc/logrotate.d/edgepulse

# ── Python virtualenv ─────────────────────────────────────────────────────────
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "[provision] creating Python virtualenv..."
    python3 -m venv "$REPO_DIR/.venv"
    "$REPO_DIR/.venv/bin/pip" install -q --upgrade pip
    "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
fi

# ── Infra stack ───────────────────────────────────────────────────────────────
echo "[provision] starting Docker infra stack..."
docker compose -f "$REPO_DIR/infra/docker-compose.yml" up -d

echo ""
echo "[provision] complete."
echo ""
echo "Next steps:"
echo "  1. Edit $ENV_DIR/edgepulse.env with real credentials"
echo "  2. Run: systemctl start edgepulse.target"
echo "  3. Check: journalctl -u edgepulse-intel -f"
