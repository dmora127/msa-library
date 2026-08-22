#!/usr/bin/env bash
# Nightly pg_dump to the attached volume, 14-day rotation.
# Cron (crontab -e):
#   15 4 * * * /opt/af3-cache/deploy/jetstream2/helpers/backup.sh >> /mnt/pgdata/backups/backup.log 2>&1
#
# NOTE: backups on the same volume only protect against logical errors. Also
# take Jetstream2 volume snapshots and/or copy dumps off-VM for real DR.
set -euo pipefail

# Dead-man's switch. Silence from this job is itself the alert: healthchecks.io
# notifies when the pings STOP, which catches a deleted cron line or a dead VM --
# failure modes that a "mail me on error" design structurally cannot see.
# Ping URLs live beside the rest of the deploy config. Path is derived from
# this script's own location, so a clone at a different prefix still works.
_SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITORING_ENV="${MONITORING_ENV:-$_SD/../.af3cache-monitoring.env}"
[ -r "$MONITORING_ENV" ] && . "$MONITORING_ENV"
HC="${HC_BACKUP_DAILY_URL:-}"
hc() { [ -n "$HC" ] && curl -fsS -m 10 --retry 3 "${HC}${1:-}" >/dev/null 2>&1; return 0; }
trap 'hc /fail' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."   # scripts live in helpers/; compose files + .env live one level up
set -a; . ./.env.prod; set +a

# Fail closed if the data volume is not mounted. /mnt/pgdata is a symlink to
# /media/volume/af3-cache-volume, mounted by Exosphere and NOT in /etc/fstab —
# so a boot race can leave an empty dir on the root disk here. Dumping an empty
# cluster over a good backup is worse than not dumping at all.
findmnt -no SOURCE,TARGET /mnt/pgdata >/dev/null 2>&1 || {
  echo "$(date -u +%FT%TZ) backup FATAL: /mnt/pgdata is not a mountpoint" >&2; hc /fail; exit 1; }
[ -f /mnt/pgdata/.volume-ok ] || {
  echo "$(date -u +%FT%TZ) backup FATAL: volume sentinel /mnt/pgdata/.volume-ok missing" >&2; hc /fail; exit 1; }

DEST=/mnt/pgdata/backups
mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

# Write to .partial and rename only on success, so an interrupted or failed
# dump can never be mistaken for a good backup. `set -o pipefail` (above) makes
# a pg_dump failure fail the whole pipeline even though gzip exits 0.
OUT="$DEST/af3cache_${STAMP}.sql.gz"
trap 'rm -f "$OUT.partial"' EXIT

docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges \
  | gzip > "$OUT.partial"

# Sanity floor: a real dump is hundreds of MB; anything tiny means an empty
# cluster or a truncated stream.
SIZE=$(stat -c %s "$OUT.partial")
if [ "$SIZE" -lt "${MIN_DUMP_BYTES:-10485760}" ]; then
  echo "$(date -u +%FT%TZ) backup FATAL: dump only ${SIZE} bytes" >&2; hc /fail; exit 1
fi
gzip -t "$OUT.partial" || { echo "$(date -u +%FT%TZ) backup FATAL: gzip corrupt" >&2; hc /fail; exit 1; }

mv "$OUT.partial" "$OUT"
trap - EXIT

find "$DEST" -name 'af3cache_*.sql.gz' -mtime +14 -delete
echo "$(date -u +%FT%TZ) backup ok: af3cache_${STAMP}.sql.gz"
hc
