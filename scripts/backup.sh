#!/usr/bin/env bash
# Back up Postgres and Redis state to /var/backups/edgepulse/
# Designed to run daily from cron: 0 3 * * * /root/EdgePulse/scripts/backup.sh
set -euo pipefail

BACKUP_ROOT="/var/backups/edgepulse"
TS=$(date +%Y%m%d-%H%M%S)
DEST="$BACKUP_ROOT/$TS"

mkdir -p "$DEST"

echo "[backup] $TS — writing to $DEST"

# ── Postgres ──────────────────────────────────────────────────────────────────
echo "[backup] dumping postgres..."
docker exec \
    "$(docker compose -f /root/EdgePulse/infra/docker-compose.yml ps -q postgres)" \
    pg_dump -U edgepulse -d edgepulse --format=custom \
    > "$DEST/edgepulse.pgdump"
echo "[backup] postgres: $(du -sh "$DEST/edgepulse.pgdump" | cut -f1)"

# ── Redis ─────────────────────────────────────────────────────────────────────
echo "[backup] snapshotting redis..."
redis-cli -s /var/run/redis/redis.sock BGSAVE
# Wait for snapshot to complete (max 30s)
for i in $(seq 1 30); do
    last=$(redis-cli -s /var/run/redis/redis.sock LASTSAVE)
    sleep 1
    now=$(redis-cli -s /var/run/redis/redis.sock LASTSAVE)
    [ "$now" -gt "$last" ] && break
done
cp /var/lib/edgepulse/redis/dump.rdb "$DEST/dump.rdb"
echo "[backup] redis: $(du -sh "$DEST/dump.rdb" | cut -f1)"

# ── Prune old backups (keep 7 days) ──────────────────────────────────────────
find "$BACKUP_ROOT" -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true

echo "[backup] complete. current backups:"
ls -lh "$BACKUP_ROOT"
