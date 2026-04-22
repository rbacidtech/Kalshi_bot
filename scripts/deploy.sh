#!/usr/bin/env bash
# Deploy latest EdgePulse code and restart services.
# Run as root on QuantVPS. Safe to run while system is live — systemd
# handles the restart sequence; exec comes up after intel.
set -euo pipefail

REPO_DIR="/root/EdgePulse"
VENV="$REPO_DIR/.venv"

echo "[deploy] pulling latest code..."
git -C "$REPO_DIR" pull --ff-only

echo "[deploy] syncing Python dependencies..."
"$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

echo "[deploy] restarting edgepulse.target..."
systemctl restart edgepulse.target

echo "[deploy] waiting for services to settle (10s)..."
sleep 10

echo "[deploy] service status:"
systemctl status edgepulse-intel.service edgepulse-exec.service \
    edgepulse-api.service --no-pager -l || true

echo "[deploy] done."
