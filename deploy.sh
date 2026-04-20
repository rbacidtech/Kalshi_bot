#!/usr/bin/env bash
# deploy.sh — sync code from Intel (NYC) → Exec (quantvps/Chicago) and restart both services.
#
# Usage:
#   ./deploy.sh           # sync + restart both nodes
#   ./deploy.sh --intel   # restart Intel only (no sync, no exec restart)
#   ./deploy.sh --exec    # sync + restart Exec only
#   ./deploy.sh --sync    # sync only, no restarts
#
# Run from the Intel node (/root/EdgePulse).  Never syncs .env or output/.

set -euo pipefail

EXEC_HOST="quantvps"
REMOTE_DIR="/root/EdgePulse"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

INTEL_SERVICE="edgepulse.service"
EXEC_SERVICE="edgepulse-exec.service"

MODE="${1:-}"

# ── Sync ──────────────────────────────────────────────────────────────────────
sync_to_exec() {
    echo "→ Syncing ep_*.py to $EXEC_HOST..."
    rsync -avz --checksum \
        "$LOCAL_DIR"/ep_*.py \
        "$EXEC_HOST:$REMOTE_DIR/"

    echo "→ Syncing kalshi_bot/ to $EXEC_HOST..."
    rsync -avz --checksum \
        "$LOCAL_DIR/kalshi_bot/" \
        "$EXEC_HOST:$REMOTE_DIR/kalshi_bot/"

    echo "→ Syncing edgepulse_launch.py to $EXEC_HOST..."
    rsync -avz --checksum \
        "$LOCAL_DIR/edgepulse_launch.py" \
        "$EXEC_HOST:$REMOTE_DIR/"

    # Verify checksum on a key file
    LOCAL_MD5=$(md5sum "$LOCAL_DIR/ep_exec.py" | awk '{print $1}')
    REMOTE_MD5=$(ssh "$EXEC_HOST" "md5sum $REMOTE_DIR/ep_exec.py" | awk '{print $1}')
    if [[ "$LOCAL_MD5" != "$REMOTE_MD5" ]]; then
        echo "ERROR: ep_exec.py checksum mismatch after sync — aborting" >&2
        exit 1
    fi
    echo "✓ Sync verified (ep_exec.py checksum matches)"
}

# ── Restart helpers ───────────────────────────────────────────────────────────
restart_intel() {
    echo "→ Restarting $INTEL_SERVICE on Intel (local)..."
    systemctl restart "$INTEL_SERVICE"
    sleep 2
    STATUS=$(systemctl is-active "$INTEL_SERVICE")
    if [[ "$STATUS" != "active" ]]; then
        echo "ERROR: $INTEL_SERVICE failed to start (status=$STATUS)" >&2
        journalctl -u "$INTEL_SERVICE" --no-pager -n 20
        exit 1
    fi
    echo "✓ $INTEL_SERVICE active"
}

restart_exec() {
    echo "→ Restarting $EXEC_SERVICE on $EXEC_HOST..."
    ssh "$EXEC_HOST" "systemctl restart $EXEC_SERVICE && sleep 2 && systemctl is-active $EXEC_SERVICE"
    echo "✓ $EXEC_SERVICE active"
}

# ── Main ──────────────────────────────────────────────────────────────────────
case "$MODE" in
    --intel)
        restart_intel
        ;;
    --exec)
        sync_to_exec
        restart_exec
        ;;
    --sync)
        sync_to_exec
        ;;
    "")
        sync_to_exec
        restart_intel
        restart_exec
        echo ""
        echo "Deploy complete."
        ;;
    *)
        echo "Usage: $0 [--intel | --exec | --sync]" >&2
        exit 1
        ;;
esac
