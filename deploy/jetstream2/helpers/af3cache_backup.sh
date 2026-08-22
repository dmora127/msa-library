#!/usr/bin/env bash
#
# Nightly pg_dump of the af3cache database to a host directory, with
# retention sweep. Designed for cron under the `exouser` account on the
# Jetstream2 VM.
#
# Install (one-time):
#   chmod +x /opt/af3-cache/deploy/jetstream2/helpers/af3cache_backup.sh
#   crontab -e   # add:
#     0 6 * * 0 ENV_FILE=/opt/af3-cache/deploy/jetstream2/.env.prod \
#               BACKUP_DIR=/media/volume/application-backup/af3cache \
#               /opt/af3-cache/deploy/jetstream2/helpers/af3cache_backup.sh \
#               >> /home/exouser/logs/backup.log 2>&1
#
# Exits non-zero (so cron MAILTO fires) on:
#   - .env missing or POSTGRES_PASSWORD unset
#   - docker exec / pg_dump non-zero
#   - dump file smaller than MIN_DUMP_BYTES (sanity floor)
#   - pg_restore --list can't parse the resulting dump
#
# Everything is env-overridable for testing — see the block below.

set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/af3-cache/deploy/jetstream2/.env.prod}"
BACKUP_DIR="${BACKUP_DIR:-/home/exouser/backups}"
LOG_DIR="${LOG_DIR:-/home/exouser/logs}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DB_CONTAINER="${DB_CONTAINER:-af3cache-db-1}"
DB_USER="${DB_USER:-af3}"
DB_NAME="${DB_NAME:-af3cache}"
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-1024}"
LOCK_FILE="${LOCK_FILE:-/tmp/af3cache-backup.lock}"

log() { printf '%s [backup] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Dead-man's switch -- see backup.sh for the rationale.
[ -r "${HOME:-/home/exouser}/.af3cache-monitoring.env" ] && . "${HOME:-/home/exouser}/.af3cache-monitoring.env"
HC="${HC_BACKUP_WEEKLY_URL:-}"
hc() { [ -n "$HC" ] && curl -fsS -m 10 --retry 3 "${HC}${1:-}" >/dev/null 2>&1; return 0; }

die() { log "ERROR: $*"; hc /fail; exit 1; }

# Single-instance lock — prevents overlap if a previous run is still going
# (e.g. dump took longer than the cron interval).
exec 9>"$LOCK_FILE"
flock -n 9 || die "another backup is already running (lock: $LOCK_FILE)"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"

# Source the .env without leaking secrets into the shell's xtrace output.
[ -r "$ENV_FILE" ] || die "env file not readable: $ENV_FILE"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
[ -n "${POSTGRES_PASSWORD:-}" ] || die "POSTGRES_PASSWORD missing from $ENV_FILE"

TS=$(date -u +%Y%m%d-%H%M%SZ)
DUMP_FILE="$BACKUP_DIR/af3cache-${TS}.dump"
TMP_FILE="${DUMP_FILE}.partial"

log "starting dump → $DUMP_FILE"

# Write to .partial first, rename on success — readers never see a torn file.
if ! docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$DB_CONTAINER" \
       pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc \
               --no-owner --no-privileges \
     > "$TMP_FILE"; then
  rm -f "$TMP_FILE"
  die "pg_dump failed"
fi

DUMP_BYTES=$(stat -c %s "$TMP_FILE")
if [ "$DUMP_BYTES" -lt "$MIN_DUMP_BYTES" ]; then
  rm -f "$TMP_FILE"
  die "dump too small ($DUMP_BYTES bytes < $MIN_DUMP_BYTES) — refusing to keep"
fi

# Cheap integrity check: pg_restore reads the TOC and bails on corruption.
if ! docker exec -i "$DB_CONTAINER" pg_restore --list < "$TMP_FILE" \
     > /dev/null; then
  rm -f "$TMP_FILE"
  die "pg_restore --list failed — dump appears corrupt"
fi

mv "$TMP_FILE" "$DUMP_FILE"
log "dump OK ($DUMP_BYTES bytes)"

# Retention sweep — best-effort; sweep failure shouldn't fail the backup.
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -name 'af3cache-*.dump' \
              -type f -mtime "+$RETENTION_DAYS" -print -delete 2>/dev/null \
          | wc -l || true)
log "retention sweep: removed $DELETED dumps older than ${RETENTION_DAYS}d"

log "done"
hc
