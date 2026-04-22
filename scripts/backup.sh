#!/usr/bin/env bash
# Back up Postgres and Redis state to /var/backups/edgepulse/
# Designed to run daily from cron: 0 3 * * * /root/EdgePulse/scripts/backup.sh
set -euo pipefail

BACKUP_ROOT="/var/backups/edgepulse"
TS=$(date +%Y%m%d-%H%M%S)
DEST="$BACKUP_ROOT/$TS"

mkdir -p "$DEST"
echo "[backup] $TS — writing to $DEST"

# ── Postgres (native) ─────────────────────────────────────────────────────────
PG_PASS=$(grep "^POSTGRES_PASSWORD=" /etc/edgepulse/edgepulse.env | cut -d= -f2)
echo "[backup] dumping postgres..."
PGPASSWORD="$PG_PASS" pg_dump -h 127.0.0.1 -U edgepulse -d edgepulse \
    --format=custom > "$DEST/edgepulse.pgdump"
echo "[backup] postgres: $(du -sh "$DEST/edgepulse.pgdump" | cut -f1)"

# ── Redis (native) ────────────────────────────────────────────────────────────
echo "[backup] snapshotting redis..."
redis-cli -s /run/redis/redis.sock BGSAVE
# Wait for snapshot to complete (max 30s)
LAST=$(redis-cli -s /run/redis/redis.sock LASTSAVE)
for i in $(seq 1 30); do
    sleep 1
    NOW=$(redis-cli -s /run/redis/redis.sock LASTSAVE)
    [ "$NOW" -gt "$LAST" ] && break
done
cp /var/lib/redis/dump.rdb "$DEST/dump.rdb"
echo "[backup] redis: $(du -sh "$DEST/dump.rdb" | cut -f1)"

# ── Prune old backups (keep 7 days) ──────────────────────────────────────────
find "$BACKUP_ROOT" -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true

echo "[backup] complete. current backups:"
ls -lh "$BACKUP_ROOT"
