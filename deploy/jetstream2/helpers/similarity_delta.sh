#!/usr/bin/env bash
# Hot-delta refresh for the MMseqs2 similarity search: hits
# POST /v1/admin/similarity/delta-refresh on the local API, which rebuilds the
# (unindexed) delta from scratch with every active sequence newer than the
# ACTIVE nightly generation, and atomically re-points deltaDB at it. An empty
# delta is valid.
#
# Cron (crontab -e) — only meaningful once SIMILARITY_DELTA_BUILD_ENABLED=true
# in .env.api; until then the endpoint answers 409 and this script exits 0
# quietly. Cadence should match SIMILARITY_DELTA_REFRESH_MINUTES:
#   50 * * * * /opt/af3-cache/deploy/jetstream2/helpers/similarity_delta.sh >> /mnt/pgdata/backups/similarity_delta.log 2>&1
#
# Notes:
#   * Reads ADMIN_API_KEY + PUBLIC_HOST out of ./.env.api.
#   * Uses `-k` while CADDY_TLS=tls internal. Drop `-k` once you cut over to a
#     trusted cert (real domain).
#   * Requires a nightly generation to exist (500 similarity_build_failed
#     otherwise) — run similarity_nightly.sh once first.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."   # scripts live in helpers/; compose files + .env live one level up

if [[ ! -r ./.env.api ]]; then
  echo "$(date -u +%FT%TZ) similarity_delta: .env.api missing" >&2
  exit 1
fi
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2-)
HOST=$(grep '^PUBLIC_HOST='    .env.api | cut -d= -f2-)

resp=$(
  curl -sk -X POST -w '\n%{http_code}' --max-time 4000 \
    "https://${HOST}/v1/admin/similarity/delta-refresh" \
    -H "X-API-Key: $ADM"
)
body=$(printf '%s' "$resp" | sed '$d')
code=$(printf '%s' "$resp" | tail -1)

if [[ "$code" == "409" && "$body" == *similarity_build_disabled* ]]; then
  echo "$(date -u +%FT%TZ) similarity_delta: disabled (SIMILARITY_DELTA_BUILD_ENABLED=false), skipping"
  exit 0
fi
if [[ "$code" != "200" ]]; then
  echo "$(date -u +%FT%TZ) similarity_delta FAILED http=$code body=$body" >&2
  exit 1
fi
echo "$(date -u +%FT%TZ) similarity_delta ok $body"
