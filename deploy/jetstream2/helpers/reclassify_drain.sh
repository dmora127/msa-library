#!/usr/bin/env bash
# One-time backlog drain. Loops POST /v1/admin/maintenance/reclassify advancing
# a cursor (after_id := last_id) until exhausted=true, so it walks the ENTIRE
# Unclassified backlog even when many rows can't be classified (diamond
# returns 'Unclassified' -> we just skip past them via id, never bouncing on
# the same head batch).
#
# Run under nohup/tmux for big backlogs (diamond is the rate limit):
#   nohup ./reclassify_drain.sh 500 \
#         > /mnt/pgdata/backups/reclassify_drain.log 2>&1 &
#   tail -f /mnt/pgdata/backups/reclassify_drain.log
#
# Usage:  ./reclassify_drain.sh [LIMIT_PER_BATCH]     (default 200)
# Safe to interrupt + re-run: each batch is its own HTTP request committed on
# its own; restart picks up where it left off (cursor reset to 0 finds the
# next Unclassified row by id and skips already-classified ones).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ ! -r ./.env.api ]]; then
  echo "$(date -u +%FT%TZ) drain: .env.api missing" >&2
  exit 1
fi
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2-)
HOST=$(grep '^PUBLIC_HOST='    .env.api | cut -d= -f2-)
LIMIT=${1:-200}

cursor=0
total_scanned=0
total_updated=0
batch=0

echo "$(date -u +%FT%TZ) drain start limit=$LIMIT host=$HOST"
while :; do
  batch=$((batch + 1))
  resp=$(curl -sk -X POST -w '\n%{http_code}' \
    "https://${HOST}/v1/admin/maintenance/reclassify?limit=${LIMIT}&after_id=${cursor}" \
    -H "X-API-Key: $ADM")
  body=$(printf '%s' "$resp" | sed '$d')
  code=$(printf '%s' "$resp" | tail -1)
  if [[ "$code" != "200" ]]; then
    echo "$(date -u +%FT%TZ) drain HTTP $code body=$body" >&2
    exit 1
  fi

  read -r scanned updated last_id exhausted < <(printf '%s' "$body" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d["scanned"], d["updated"], d["last_id"] if d["last_id"] is not None else 0, str(d["exhausted"]).lower())
')

  total_scanned=$((total_scanned + scanned))
  total_updated=$((total_updated + updated))
  echo "$(date -u +%FT%TZ) drain batch=$batch scanned=$scanned updated=$updated cursor=$last_id exhausted=$exhausted (totals scanned=$total_scanned updated=$total_updated)"

  if [[ "$exhausted" == "true" ]]; then break; fi
  cursor=$last_id
done

echo "$(date -u +%FT%TZ) drain DONE batches=$batch total_scanned=$total_scanned total_updated=$total_updated"
