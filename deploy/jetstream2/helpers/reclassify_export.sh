#!/usr/bin/env bash
# NR-BLAST reclassification hand-off (nightly): POST /v1/admin/classification/jobs/submit.
# The API does the work -- exports the pending set, scp's it to the BLAST box's
# inbox, runs `reclass-submit` there and records the job -- so this is only a
# curl. The same call is the on-demand path from any host with the admin key.
#
# Submits on EVERY run, even if earlier jobs are still queued/running: the
# remote queue serialises them and the callback only touches rows that are
# still pending, so overlapping exports are harmless.
#
# Cron (crontab -e):
#   30 0 * * * /opt/af3-cache/deploy/jetstream2/helpers/reclassify_export.sh >> /mnt/pgdata/backups/reclassify_export.log 2>&1
#
# Env overrides: MIN_LENGTH, MAX_LENGTH, LIMIT (request body; empty = server
# defaults, i.e. RECLASS_EXPORT_MIN_LENGTH and no cap), CURL_MAX_TIME=1200,
# LOCK_FILE=/tmp/reclassify_export.lock. E.g. `LIMIT=50 ./reclassify_export.sh`.
#
# Exit codes: 0 ok / nothing pending / submit already in progress (quiet);
# 1 API or remote failure; 2 misconfiguration. `-k` while CADDY_TLS=tls internal.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."   # scripts live in helpers/; compose files + .env live one level up

CURL_MAX_TIME="${CURL_MAX_TIME:-1200}"
LOCK_FILE="${LOCK_FILE:-/tmp/reclassify_export.lock}"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

if [[ ! -r ./.env.api ]]; then
  echo "$(ts) reclassify_export: .env.api missing" >&2; exit 2
fi
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2-)
HOST=$(grep '^PUBLIC_HOST='    .env.api | cut -d= -f2-)
if [[ -z "$ADM" || -z "$HOST" ]]; then
  echo "$(ts) reclassify_export: ADMIN_API_KEY / PUBLIC_HOST unset in .env.api" >&2; exit 2
fi

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

BODY=$(MIN_LENGTH="${MIN_LENGTH:-}" MAX_LENGTH="${MAX_LENGTH:-}" LIMIT="${LIMIT:-}" python3 -c '
import json, os
b = {k: int(v) for k, v in (("min_length", os.environ["MIN_LENGTH"]),
                            ("max_length", os.environ["MAX_LENGTH"]),
                            ("limit", os.environ["LIMIT"])) if v}
print(json.dumps(b))')

resp=$(curl -sk -X POST -w '\n%{http_code}' --max-time "$CURL_MAX_TIME" \
         "https://${HOST}/v1/admin/classification/jobs/submit" \
         -H "X-API-Key: $ADM" -H 'content-type: application/json' --data-binary "$BODY")
code=$(printf '%s' "$resp" | tail -1)
body=$(printf '%s' "$resp" | sed '$d')

case "$code" in
  201)
    printf '%s' "$body" | python3 -c '
import json, sys
j = json.load(sys.stdin)
print(sys.argv[1] + " reclassify_export ok job=" + j["job_id"] + " sequences=" + str(j["sequence_count"])
      + " bytes=" + str(j.get("input_bytes")) + " input=" + j["input_name"])' "$(ts)"
    ;;
  409)
    if [[ "$body" == *reclass_nothing_pending* ]]; then exit 0; fi         # quiet
    if [[ "$body" == *reclass_submit_in_progress* ]]; then
      echo "$(ts) reclassify_export: a submit is already in progress, skipping"; exit 0
    fi
    echo "$(ts) reclassify_export FAILED http=$code body=$body" >&2; exit 1
    ;;
  *)
    echo "$(ts) reclassify_export FAILED http=$code body=$body" >&2; exit 1
    ;;
esac
