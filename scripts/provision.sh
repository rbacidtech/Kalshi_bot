#!/usr/bin/env bash
# Provision a fresh QuantVPS for EdgePulse single-box deployment.
# Idempotent — safe to re-run.
set -euo pipefail

REPO_DIR="/root/EdgePulse"
SYSTEMD_DIR="/etc/systemd/system"
ENV_DIR="/etc/edgepulse"
PG_VERSION=16

# ── Filesystem hierarchy ──────────────────────────────────────────────────────
echo "[provision] creating filesystem hierarchy..."
mkdir -p /var/log/edgepulse /var/backups/edgepulse "$ENV_DIR"
touch /var/log/edgepulse/{intel,exec,api,llm,advisor,arb,econ_release,ob_depth}.log

# ── Environment file ──────────────────────────────────────────────────────────
if [ ! -f "$ENV_DIR/edgepulse.env" ]; then
    echo "[provision] creating $ENV_DIR/edgepulse.env..."
    if [ -f "$REPO_DIR/.env" ]; then
        cp "$REPO_DIR/.env" "$ENV_DIR/edgepulse.env"
    else
        cat > "$ENV_DIR/edgepulse.env" << 'EOF'
# EdgePulse runtime config — fill in all values before starting
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=/root/EdgePulse/private_key.pem
ANTHROPIC_API_KEY=
FRED_API_KEY=
POSTGRES_PASSWORD=
GRAFANA_ADMIN_PASSWORD=
REDIS_URL=unix:///run/redis/redis.sock
DATABASE_URL=postgresql+asyncpg://edgepulse:CHANGE_ME@localhost/edgepulse
LLM_MODEL=claude-sonnet-4-6
EOF
        echo "[provision] ** fill in $ENV_DIR/edgepulse.env before starting **"
    fi
    chmod 600 "$ENV_DIR/edgepulse.env"
fi

# ── Redis (native) ────────────────────────────────────────────────────────────
if ! command -v redis-server &>/dev/null || ! redis-server --version | grep -q "7\|8"; then
    echo "[provision] installing Redis from official repo..."
    curl -fsSL https://packages.redis.io/gpg \
        | gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] \
https://packages.redis.io/deb $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/redis.list
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --option \
        Dpkg::Options::="--force-confold" redis 2>&1 | tail -3
fi

# Configure Redis Unix socket
grep -q "^unixsocket " /etc/redis/redis.conf || \
    echo "unixsocket /run/redis/redis.sock" >> /etc/redis/redis.conf
grep -q "^unixsocketperm " /etc/redis/redis.conf || \
    echo "unixsocketperm 777" >> /etc/redis/redis.conf
grep -q "^appendonly yes" /etc/redis/redis.conf || \
    sed -i 's/^appendonly no/appendonly yes/' /etc/redis/redis.conf
grep -q "^daemonize yes" /etc/redis/redis.conf || \
    sed -i 's/^daemonize no/daemonize yes/' /etc/redis/redis.conf
grep -q "^pidfile /run/redis" /etc/redis/redis.conf || \
    echo "pidfile /run/redis/redis-server.pid" >> /etc/redis/redis.conf
grep -q "^maxmemory 2gb" /etc/redis/redis.conf || \
    echo "maxmemory 2gb" >> /etc/redis/redis.conf

# Mask the hardened official service; ours handles startup correctly
chmod 644 /etc/redis/redis.conf
chmod 755 /etc/redis/
systemctl mask redis-server redis 2>/dev/null || true

# ── PostgreSQL (native) ───────────────────────────────────────────────────────
if ! command -v psql &>/dev/null; then
    echo "[provision] installing PostgreSQL $PG_VERSION..."
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg
    echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    apt-get update -qq
    apt-get install -y postgresql-${PG_VERSION} 2>&1 | tail -3
fi

# Create DB user and database (idempotent)
PG_PASS=$(grep "^POSTGRES_PASSWORD=" "$ENV_DIR/edgepulse.env" 2>/dev/null | cut -d= -f2)
if [ -n "$PG_PASS" ]; then
    sudo -u postgres psql -tc \
        "SELECT 1 FROM pg_roles WHERE rolname='edgepulse'" | grep -q 1 || \
        sudo -u postgres psql -c "CREATE USER edgepulse WITH PASSWORD '$PG_PASS';"
    sudo -u postgres psql -tc \
        "SELECT 1 FROM pg_database WHERE datname='edgepulse'" | grep -q 1 || \
        sudo -u postgres psql -c "CREATE DATABASE edgepulse OWNER edgepulse;"
    sudo -u postgres psql -c \
        "GRANT ALL PRIVILEGES ON DATABASE edgepulse TO edgepulse;" 2>/dev/null
fi

# ── systemd service files ─────────────────────────────────────────────────────
echo "[provision] installing systemd units..."
cp "$REPO_DIR/systemd/edgepulse.target"               "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-redis.service"        "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-intel.service"        "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-exec.service"         "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-api.service"          "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-llm.service"          "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-llm.timer"            "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-advisor.service"      "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-arb.service"          "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-econ-release.service" "$SYSTEMD_DIR/"
cp "$REPO_DIR/systemd/edgepulse-ob-depth.service"     "$SYSTEMD_DIR/"

systemctl daemon-reload
systemctl enable edgepulse.target edgepulse-redis.service

# ── Log rotation ──────────────────────────────────────────────────────────────
echo "[provision] installing logrotate config..."
cp "$REPO_DIR/logrotate.conf" /etc/logrotate.d/edgepulse

# ── Python virtualenv ─────────────────────────────────────────────────────────
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "[provision] creating Python virtualenv..."
    python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/pip" install -q --upgrade pip
"$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
"$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements-api.txt"
# bcrypt 4.0.1 is pinned in requirements-api.txt — enforce it
"$REPO_DIR/.venv/bin/pip" install -q "bcrypt==4.0.1"

# ── Monitoring stack (optional) ───────────────────────────────────────────────
# Prometheus + Grafana via Docker (snap Docker works fine for these — no bind mounts to /var/lib)
# Uncomment if needed:
# docker-compose -f "$REPO_DIR/infra/docker-compose.yml" up -d prometheus grafana

echo ""
echo "[provision] complete."
echo ""
echo "Next steps:"
echo "  1. Confirm $ENV_DIR/edgepulse.env has real credentials"
echo "  2. systemctl start edgepulse.target"
echo "  3. journalctl -u edgepulse-intel -f"
