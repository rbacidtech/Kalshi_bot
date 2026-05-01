#!/usr/bin/env bash
# migrate_resolutions_db.sh — one-shot migration of the legacy resolutions DB
# to add the `outcome` column expected by the current code path.
#
# Idempotent. Safe to run multiple times. Refuses to run while edgepulse-exec
# is active (the service holds the SQLite connection and concurrent ALTER
# can deadlock or partial-commit).
#
# What it does:
#   1. Refuse if `edgepulse-exec` is `active`.
#   2. Snapshot the DB to /tmp/resolutions.db.bak.<UTC-ts>.
#   3. Capture pre-migration schema + counts.
#   4. ALTER TABLE resolutions ADD COLUMN outcome TEXT (idempotent).
#   5. Backfill outcome from resolved_yes:
#        resolved_yes=1 → 'yes', resolved_yes=0 → 'no', NULL → unchanged.
#   6. Verify post-state matches expectations (199 yes / 3045 no / 0 NULL on
#      a freshly-migrated copy of today's prod DB; counts may grow once the
#      service resumes writing).
#
# Does NOT add `series_ticker` to the legacy DB — that column already exists
# under NOT NULL in legacy. The code's idempotent ALTER in init() handles the
# fresh-install case where the column is added by the CREATE TABLE body.

set -euo pipefail

DB_PATH="${1:-/root/EdgePulse/output/resolutions.db}"
PYTHON="${PYTHON:-/root/EdgePulse/.venv/bin/python3}"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log() { echo "[$(ts)] $*"; }
die() { echo "[$(ts)] FAIL: $*" >&2; exit 1; }

log "migrate_resolutions_db.sh starting"
log "DB_PATH=$DB_PATH"
log "PYTHON=$PYTHON"

# 1. Refuse if edgepulse-exec is active ----------------------------------------
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet edgepulse-exec; then
        die "edgepulse-exec is active. Stop it first: systemctl stop edgepulse-exec"
    fi
    log "edgepulse-exec is not active — proceeding."
else
    log "WARN: systemctl not available; cannot verify service state. Proceeding anyway."
fi

# 2. Existence + backup --------------------------------------------------------
if [[ ! -f "$DB_PATH" ]]; then
    die "DB not found at $DB_PATH"
fi

BACKUP_PATH="/tmp/resolutions.db.bak.$(date -u +%Y%m%dT%H%M%SZ)"
cp -a "$DB_PATH" "$BACKUP_PATH"
log "backup → $BACKUP_PATH"

# 3-6. All migration logic in a single Python heredoc (no sqlite3 CLI dep) -----
"$PYTHON" - "$DB_PATH" "$BACKUP_PATH" <<'PYEOF'
import sqlite3
import sys

db_path     = sys.argv[1]
backup_path = sys.argv[2]

conn = sqlite3.connect(db_path)
conn.isolation_level = None  # explicit transaction control

def cols(table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

def count(sql, params=()):
    return conn.execute(sql, params).fetchone()[0]

# 3. Pre-migration snapshot
pre_cols = cols("resolutions")
print(f"[migrate] pre-schema columns: {pre_cols}")
total = count("SELECT COUNT(*) FROM resolutions")
print(f"[migrate] pre rows: total={total}")
ry_dist = list(conn.execute(
    "SELECT resolved_yes, COUNT(*) FROM resolutions GROUP BY resolved_yes"
))
print(f"[migrate] pre resolved_yes distribution: {ry_dist}")

# 4. Idempotent ALTER for `outcome`
if "outcome" not in pre_cols:
    print("[migrate] adding outcome column")
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE resolutions ADD COLUMN outcome TEXT")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
else:
    print("[migrate] outcome column already exists — skipping ALTER")

# 5. Backfill from resolved_yes
print("[migrate] backfilling outcome from resolved_yes")
conn.execute("BEGIN")
try:
    cur = conn.execute("""
        UPDATE resolutions
           SET outcome = CASE
               WHEN resolved_yes = 1 THEN 'yes'
               WHEN resolved_yes = 0 THEN 'no'
           END
         WHERE outcome IS NULL
    """)
    updated = cur.rowcount
    conn.execute("COMMIT")
    print(f"[migrate] rows updated: {updated}")
except Exception:
    conn.execute("ROLLBACK")
    raise

# 6. Post-state verification
post_cols = cols("resolutions")
print(f"[migrate] post-schema columns: {post_cols}")
if "outcome" not in post_cols:
    raise SystemExit("[migrate] FAIL: outcome column still missing post-migration")

n_yes  = count("SELECT COUNT(*) FROM resolutions WHERE outcome = 'yes'")
n_no   = count("SELECT COUNT(*) FROM resolutions WHERE outcome = 'no'")
n_null = count("SELECT COUNT(*) FROM resolutions WHERE outcome IS NULL")
n_other = count(
    "SELECT COUNT(*) FROM resolutions "
    "WHERE outcome IS NOT NULL AND outcome NOT IN ('yes','no')"
)

print(f"[migrate] post outcome distribution: yes={n_yes} no={n_no} NULL={n_null} other={n_other}")

# Sanity: yes+no+NULL+other should equal total rows; no rows should be lost.
post_total = count("SELECT COUNT(*) FROM resolutions")
if post_total != total:
    raise SystemExit(
        f"[migrate] FAIL: row count changed pre={total} post={post_total}"
    )
if n_other != 0:
    raise SystemExit(
        f"[migrate] FAIL: unexpected outcome values ({n_other} rows neither yes/no/NULL)"
    )

# resolved_yes is nullable; if any rows had NULL resolved_yes, outcome will be
# NULL too. On the current prod DB that's 0; if the count is non-zero report it.
if n_null > 0:
    print(
        f"[migrate] NOTE: {n_null} rows have NULL outcome (resolved_yes was NULL). "
        "These are pre-existing data and not a migration defect."
    )

print(f"[migrate] OK ({n_yes} yes / {n_no} no / {n_null} NULL / {post_total} total)")
PYEOF

PY_RC=$?
if [[ $PY_RC -ne 0 ]]; then
    cat <<EOF >&2

[$(ts)] FAIL: migration aborted (rc=$PY_RC).

Restore instructions:
    cp -a "$BACKUP_PATH" "$DB_PATH"
    # then resume edgepulse-exec only after diagnosing the cause.

EOF
    exit "$PY_RC"
fi

log "migration complete OK. Backup retained at $BACKUP_PATH"
log "next step: restart edgepulse-exec; init() will run the round-trip self-test on startup."
