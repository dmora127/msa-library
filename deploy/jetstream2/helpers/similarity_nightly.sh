#!/usr/bin/env bash
# Nightly MMseqs2 similarity database build: hits
# POST /v1/admin/similarity/nightly-build on the local API, which dumps every
# active sequence from Postgres, builds + indexes a new immutable generation
# under SIMILARITY_DB_ROOT, and atomically re-points nightly-buildDB at it.
#
# Cron (crontab -e) — only meaningful once SIMILARITY_NIGHTLY_BUILD_ENABLED=true
# in .env.api; until then the endpoint answers 409 and this script exits 0
# quietly, so the line can be installed ahead of the flag flip:
#   20 2 * * * /opt/af3-cache/deploy/jetstream2/helpers/similarity_nightly.sh >> /mnt/pgdata/backups/similarity_nightly.log 2>&1
#
# Notes:
#   * Reads ADMIN_API_KEY + PUBLIC_HOST out of ./.env.api.
#   * Uses `-k` while CADDY_TLS=tls internal. Drop `-k` once you cut over to a
#     trusted cert (real domain).
#   * The build is synchronous and can take a while on a large table; the
#     --max-time below must exceed SIMILARITY_BUILD_TIMEOUT_SECONDS.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."   # scripts live in helpers/; compose files + .env live one level up

if [[ ! -r ./.env.api ]]; then
  echo "$(date -u +%FT%TZ) similarity_nightly: .env.api missing" >&2
  exit 1
fi
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2-)
HOST=$(grep '^PUBLIC_HOST='    .env.api | cut -d= -f2-)

resp=$(
  curl -sk -X POST -w '\n%{http_code}' --max-time 4000 \
    "https://${HOST}/v1/admin/similarity/nightly-build" \
    -H "X-API-Key: $ADM"
)
body=$(printf '%s' "$resp" | sed '$d')
code=$(printf '%s' "$resp" | tail -1)

if [[ "$code" == "409" && "$body" == *similarity_build_disabled* ]]; then
  echo "$(date -u +%FT%TZ) similarity_nightly: disabled (SIMILARITY_NIGHTLY_BUILD_ENABLED=false), skipping"
  exit 0
fi
if [[ "$code" != "200" ]]; then
  echo "$(date -u +%FT%TZ) similarity_nightly FAILED http=$code body=$body" >&2
  exit 1
fi
echo "$(date -u +%FT%TZ) similarity_nightly ok $body"
