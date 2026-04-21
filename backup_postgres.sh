#!/usr/bin/env bash
# Daily Postgres backup for EdgePulse
# Deploy: chmod +x /root/EdgePulse/backup_postgres.sh
# Cron:   0 3 * * * /root/EdgePulse/backup_postgres.sh >> /root/EdgePulse/output/logs/backup.log 2>&1

set -euo pipefail

BACKUP_DIR="/root/EdgePulse/output/backups"
KEEP_DAYS=14
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/edgepulse_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting Postgres backup..."

# Find the running postgres container
PG_CONTAINER=$(docker ps --filter "name=postgres" --filter "status=running" -q | head -1)

if [ -z "$PG_CONTAINER" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: No running postgres container found"
    exit 1
fi

# Dump and compress
docker exec "$PG_CONTAINER" \
    pg_dump -U edgepulse -d edgepulse --no-password \
    | gzip > "$BACKUP_FILE"

SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup written: $BACKUP_FILE ($SIZE)"

# Prune old backups
find "$BACKUP_DIR" -name "edgepulse_*.sql.gz" -mtime +${KEEP_DAYS} -delete
REMAINING=$(find "$BACKUP_DIR" -name "edgepulse_*.sql.gz" | wc -l)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup complete. ${REMAINING} backups retained."
