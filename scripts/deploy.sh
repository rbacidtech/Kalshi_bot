#!/usr/bin/env bash
# deploy.sh — pull latest code and restart EdgePulse on this node.
# Run as root on QuantVPS.  Safe to run while live — systemd handles restart sequence.
#
# Usage:
#   ./deploy.sh           # check tree, pull, deps, restart, health-check
#   ./deploy.sh --force   # skip dirty-tree check (emergency use only)

set -euo pipefail

REPO_DIR="/root/EdgePulse"
VENV="$REPO_DIR/.venv"
FORCE="${1:-}"

CRITICAL_SERVICES=(edgepulse-intel edgepulse-exec edgepulse-api)
ALL_SERVICES=(edgepulse-intel edgepulse-exec edgepulse-api
              edgepulse-advisor edgepulse-arb edgepulse-econ-release edgepulse-ob-depth)
API_PORT=8502
INTEL_PORT=9091

# ── Dirty-tree check ──────────────────────────────────────────────────────────
if [[ "$FORCE" != "--force" ]]; then
    DIRTY=$(git -C "$REPO_DIR" status --porcelain 2>/dev/null || true)
    if [[ -n "$DIRTY" ]]; then
        echo "ERROR: working tree has uncommitted changes — deploy blocked." >&2
        echo ""
        echo "  Uncommitted files:"
        git -C "$REPO_DIR" status --short
        echo ""
        echo "  Commit or stash first, then re-run deploy."
        echo "  To override in an emergency: $0 --force"
        exit 1
    fi
fi

# ── Pull ──────────────────────────────────────────────────────────────────────
echo "[deploy] pulling latest code..."
git -C "$REPO_DIR" pull --ff-only

SHA=$(git -C "$REPO_DIR" rev-parse HEAD)
SHORT_SHA="${SHA:0:12}"
echo "[deploy] HEAD = $SHORT_SHA"

# ── Record deployed SHA ───────────────────────────────────────────────────────
echo "$SHA" > "$REPO_DIR/.deployed_sha"
echo "[deploy] wrote $REPO_DIR/.deployed_sha"

# ── Dependencies ──────────────────────────────────────────────────────────────
echo "[deploy] syncing Python dependencies..."
"$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

# ── Restart ───────────────────────────────────────────────────────────────────
echo "[deploy] restarting edgepulse.target..."
systemctl restart edgepulse.target

# ── Health check (up to 90s) ─────────────────────────────────────────────────
echo "[deploy] waiting for services to come up..."

_poll_service() {
    local svc="$1" deadline="$2"
    while [[ $(date +%s) -lt $deadline ]]; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}

_poll_http() {
    local url="$1" deadline="$2"
    while [[ $(date +%s) -lt $deadline ]]; do
        if curl -sf --max-time 3 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

DEADLINE=$(( $(date +%s) + 90 ))
FAILED=0

for svc in "${CRITICAL_SERVICES[@]}"; do
    printf "  %-36s " "$svc"
    if _poll_service "$svc" "$DEADLINE"; then
        echo "active"
    else
        echo "FAILED ($(systemctl is-active "$svc" 2>/dev/null || echo 'unknown'))"
        journalctl -u "$svc" --no-pager -n 15 | sed 's/^/    /'
        FAILED=1
    fi
done

# HTTP health checks
printf "  %-36s " "api:$API_PORT/health"
if _poll_http "http://127.0.0.1:${API_PORT}/health" "$DEADLINE"; then
    echo "ok"
else
    echo "UNREACHABLE"
    FAILED=1
fi

printf "  %-36s " "intel:$INTEL_PORT/health"
if _poll_http "http://127.0.0.1:${INTEL_PORT}/health" "$DEADLINE"; then
    echo "ok"
else
    echo "UNREACHABLE"
    FAILED=1
fi

echo ""
echo "── All services ─────────────────────────────────────────"
for svc in "${ALL_SERVICES[@]}"; do
    state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
    printf "  %-36s %s\n" "$svc" "$state"
done

echo ""
if [[ $FAILED -eq 1 ]]; then
    echo "[deploy] FAILED — one or more services did not come up cleanly." >&2
    echo "[deploy] SHA $SHORT_SHA is on disk but system may be degraded." >&2
    exit 1
fi

echo "[deploy] OK — $SHORT_SHA deployed and healthy."
