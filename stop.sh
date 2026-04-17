#!/bin/bash
# stop.sh — Stop all EdgePulse Intel-node screen sessions and Docker stack.
# Run this on the Intel (DO NYC3) node only.
# The Exec node (QuantVPS) must be stopped separately.

echo "──────────────────────────────────────────────"
echo "  EdgePulse stop — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "──────────────────────────────────────────────"

# ── 1. Kill screen sessions ────────────────────────────────────────────────────
for s in edgepulse llm dash bot watchdog; do
    if screen -ls | grep -q "$s"; then
        screen -S "$s" -X quit 2>/dev/null && echo "  Stopped screen: $s"
    fi
done
screen -wipe 2>/dev/null

# ── 2. Stop systemd service if running ────────────────────────────────────────
if systemctl is-active --quiet edgepulse.service; then
    systemctl stop edgepulse.service && echo "  Stopped systemd: edgepulse.service"
fi

# ── 3. Stop Docker Compose stack ──────────────────────────────────────────────
ROOT="$(cd "$(dirname "$0")" && pwd)"
if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    docker compose -f "${ROOT}/docker-compose.yml" down && echo "  Stopped Docker Compose stack"
fi

echo ""
echo "All EdgePulse services stopped (Intel node)."
echo "To stop the Exec node: SSH to QuantVPS and run stop.sh there, or: systemctl stop edgepulse"
