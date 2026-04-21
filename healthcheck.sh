#!/usr/bin/env bash
# EdgePulse external health monitor
# Deploy: chmod +x /root/EdgePulse/healthcheck.sh
# Cron:   */5 * * * * /root/EdgePulse/healthcheck.sh
#
# Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID from .env if present.

set -euo pipefail

API_URL="${EP_HEALTH_URL:-http://localhost:8502/health}"
ALERT_COOLDOWN_FILE="/tmp/ep_health_alerted"
COOLDOWN_SECONDS=1800   # 30 min — don't spam

# Load .env if it exists
ENV_FILE="/root/EdgePulse/.env"
if [ -f "$ENV_FILE" ]; then
    set -o allexport
    # shellcheck disable=SC1090
    source <(grep -E '^[A-Z_]+=.+' "$ENV_FILE" | sed 's/#.*//')
    set +o allexport
fi

BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHANNEL_ID="${TELEGRAM_CHANNEL_ID:-}"

send_telegram() {
    local msg="$1"
    if [ -n "$BOT_TOKEN" ] && [ -n "$CHANNEL_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${CHANNEL_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

# Check cooldown
now=$(date +%s)
if [ -f "$ALERT_COOLDOWN_FILE" ]; then
    last_alert=$(cat "$ALERT_COOLDOWN_FILE")
    elapsed=$((now - last_alert))
    if [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
        exit 0
    fi
fi

# Hit health endpoint
HTTP_CODE=$(curl -s -o /tmp/ep_health_body.json -w "%{http_code}" \
    --connect-timeout 5 --max-time 10 "$API_URL" 2>/dev/null || echo "000")

if [ "$HTTP_CODE" != "200" ]; then
    msg="🚨 EdgePulse API DOWN — HTTP ${HTTP_CODE} from ${API_URL} at $(date -u +%H:%MZ)"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ALERT: $msg"
    send_telegram "$msg"
    echo "$now" > "$ALERT_COOLDOWN_FILE"
    exit 1
fi

# Check for degraded status in body
STATUS=$(python3 -c "import json,sys; d=json.load(open('/tmp/ep_health_body.json')); print(d.get('status','unknown'))" 2>/dev/null || echo "parse_error")

if [ "$STATUS" != "ok" ]; then
    DETAIL=$(cat /tmp/ep_health_body.json 2>/dev/null || echo "{}")
    msg="⚠️ EdgePulse DEGRADED — status=${STATUS} detail=${DETAIL}"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARN: $msg"
    send_telegram "$msg"
    echo "$now" > "$ALERT_COOLDOWN_FILE"
fi

exit 0
