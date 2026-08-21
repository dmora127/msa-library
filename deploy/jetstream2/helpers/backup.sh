#!/usr/bin/env bash
# Nightly pg_dump to the attached volume, 14-day rotation.
# Cron (crontab -e):
#   15 4 * * * /opt/af3-cache/deploy/jetstream2/backup.sh >> /mnt/pgdata/backups/backup.log 2>&1
#
# NOTE: backups on the same volume only protect against logical errors. Also
# take Jetstream2 volume snapshots and/or copy dumps off-VM for real DR.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
set -a; . ./.env.prod; set +a

DEST=/mnt/pgdata/backups
mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges \
  | gzip > "$DEST/af3cache_${STAMP}.sql.gz"

find "$DEST" -name 'af3cache_*.sql.gz' -mtime +14 -delete
echo "$(date -u +%FT%TZ) backup ok: af3cache_${STAMP}.sql.gz"
