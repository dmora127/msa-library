#!/usr/bin/env bash
# Periodic re-classification sweep: hits POST /v1/admin/maintenance/reclassify
# on the local API, which re-runs the configured Classifier on every
# msa_cache_entries row still marked 'Unclassified' (up to LIMIT) and updates
# species + query_name on confident hits.
#
# Cron (crontab -e):
#   30 * * * * /opt/af3-cache/deploy/jetstream2/helpers/reclassify.sh >> /mnt/pgdata/backups/reclassify.log 2>&1
#
# Usage:  ./reclassify.sh [LIMIT]   (default LIMIT=200)
#
# Notes:
#   * Reads ADMIN_API_KEY + PUBLIC_HOST out of ./.env.api.
#   * Uses `-k` while CADDY_TLS=tls internal. Drop `-k` once you cut over to a
#     trusted cert (real domain).
#   * Diamond can be slow; --limit bounds per-run work. Cron more often to
#     drain a backlog.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."   # scripts live in helpers/; compose files + .env live one level up

if [[ ! -r ./.env.api ]]; then
  echo "$(date -u +%FT%TZ) reclassify: .env.api missing" >&2
  exit 1
fi
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2-)
HOST=$(grep '^PUBLIC_HOST='    .env.api | cut -d= -f2-)
LIMIT=${1:-200}

resp=$(
  curl -sk -X POST -w '\n%{http_code}' \
    "https://${HOST}/v1/admin/maintenance/reclassify?limit=${LIMIT}" \
    -H "X-API-Key: $ADM"
)
body=$(printf '%s' "$resp" | sed '$d')
code=$(printf '%s' "$resp" | tail -1)

if [[ "$code" != "200" ]]; then
  echo "$(date -u +%FT%TZ) reclassify FAILED http=$code body=$body" >&2
  exit 1
fi
echo "$(date -u +%FT%TZ) reclassify ok limit=$LIMIT $body"
