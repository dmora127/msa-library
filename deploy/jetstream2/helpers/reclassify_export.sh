#!/usr/bin/env bash
# NR-BLAST reclassification hand-off (nightly):
#   GET /v1/admin/classification/pending.fa  ->  /mnt/pgdata/reclass/pending-<ts>.fa
#   -> scp to the BLAST box inbox  ->  `reclass-submit inbox/<file>` (returns a
#   job id at once; the box has its own queue)  ->  POST /v1/admin/classification/jobs
# Results come back on their own via POST /v1/admin/reclassify from the box;
# helpers/reclassify_poll.sh tracks the job until it is terminal.
#
# Submits on EVERY run, even if earlier jobs are still queued/running: the
# remote queue serialises them and the callback only touches rows that are
# still pending, so overlapping exports are harmless.
#
# Cron (crontab -e):
#   30 0 * * * /opt/af3-cache/deploy/jetstream2/helpers/reclassify_export.sh >> /mnt/pgdata/backups/reclassify_export.log 2>&1
#
# Env overrides (defaults shown):
#   EXPORT_DIR=/mnt/pgdata/reclass         local copies (data volume); KEEP newest
#   KEEP=8
#   REMOTE=exouser@<blast-box-ip>
#   REMOTE_DATA_DIR=/media/volume/mmseq2-data   (FASTA lands in $REMOTE_DATA_DIR/inbox/)
#   REMOTE_SUBMIT=/home/exouser/.local/bin/reclass-submit
#                                          full path: ~/.local/bin is NOT on the PATH of a
#                                          non-interactive ssh session
#   EXTRA_QUERY=                           e.g. "limit=50", "max_length=2000", "min_length=60"
#   CURL_MAX_TIME=900  LOCK_FILE=/tmp/reclassify_export.lock
#
# Exit codes: 0 ok / nothing pending (silent); 1 export, transfer or submit
# failure; 2 misconfiguration. Notes: `-k` while CADDY_TLS=tls internal; the
# first run needs the box's host key (accept-new records it) and the ssh key
# from this VM already authorised there.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."   # scripts live in helpers/; compose files + .env live one level up

EXPORT_DIR="${EXPORT_DIR:-/mnt/pgdata/reclass}"
KEEP="${KEEP:-8}"
REMOTE="${REMOTE:-exouser@<blast-box-ip>}"
REMOTE_DATA_DIR="${REMOTE_DATA_DIR:-/media/volume/mmseq2-data}"
REMOTE_SUBMIT="${REMOTE_SUBMIT:-/home/exouser/.local/bin/reclass-submit}"
EXTRA_QUERY="${EXTRA_QUERY:-}"
CURL_MAX_TIME="${CURL_MAX_TIME:-900}"
LOCK_FILE="${LOCK_FILE:-/tmp/reclassify_export.lock}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new)

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

if [[ ! -r ./.env.api ]]; then
  echo "$(ts) reclassify_export: .env.api missing" >&2; exit 2
fi
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2-)
HOST=$(grep '^PUBLIC_HOST='    .env.api | cut -d= -f2-)
if [[ -z "$ADM" || -z "$HOST" ]]; then
  echo "$(ts) reclassify_export: ADMIN_API_KEY / PUBLIC_HOST unset in .env.api" >&2; exit 2
fi
if ! mkdir -p "$EXPORT_DIR" 2>/dev/null || [[ ! -w "$EXPORT_DIR" ]]; then
  echo "$(ts) reclassify_export: $EXPORT_DIR not writable" >&2; exit 2
fi

# ---- single-instance lock (silent skip if another run is active) -------------
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

API="https://${HOST}/v1/admin/classification"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
NAME="pending-${STAMP}.fa"
OUT="${EXPORT_DIR}/${NAME}"
HDR="${OUT}.headers"
URL="${API}/pending.fa${EXTRA_QUERY:+?$EXTRA_QUERY}"

# ---- 1. export --------------------------------------------------------------
# -f: HTTP error -> exit 22; a truncated chunked body -> exit 18. Write to .part
# and rename so a half-written file is never mistaken for an export.
if ! curl -sSk -f --max-time "$CURL_MAX_TIME" -D "$HDR" -o "${OUT}.part" \
        -H "X-API-Key: $ADM" "$URL"; then
  rc=$?
  echo "$(ts) reclassify_export FAILED export curl rc=$rc url=$URL" >&2
  rm -f "${OUT}.part" "$HDR"; exit 1
fi
mv "${OUT}.part" "$OUT"
EXPECTED=$(grep -i '^x-sequence-count:' "$HDR" | tr -dc '0-9' || true)
MINLEN=$(grep -i '^x-min-length:' "$HDR" | tr -dc '0-9' || true)
rm -f "$HDR"
N=$(grep -c '^>' "$OUT" || true)
if [[ -n "$EXPECTED" && "$EXPECTED" != "$N" ]]; then
  echo "$(ts) reclassify_export WARN count header=$EXPECTED file=$N (writes landed mid-export?)"
fi
if [[ "$N" -eq 0 ]]; then
  rm -f "$OUT"; exit 0            # nothing pending -> quiet
fi
BYTES=$(stat -c%s "$OUT")

# ---- 2. transfer ------------------------------------------------------------
if ! scp -q "${SSH_OPTS[@]}" "$OUT" "${REMOTE}:${REMOTE_DATA_DIR}/inbox/"; then
  echo "$(ts) reclassify_export FAILED scp $NAME -> ${REMOTE}:${REMOTE_DATA_DIR}/inbox/ (local copy kept: $OUT)" >&2
  exit 1
fi

# ---- 3. submit ----------------------------------------------------------------
JOB=$(ssh "${SSH_OPTS[@]}" "$REMOTE" "${REMOTE_SUBMIT} inbox/${NAME}" 2>&1 | tail -1 | tr -d '[:space:]' || true)
if [[ -z "$JOB" || "$JOB" == *" "* || ${#JOB} -gt 128 ]]; then
  echo "$(ts) reclassify_export FAILED remote submit of inbox/${NAME} (output: '${JOB}'); file remains in the inbox" >&2
  exit 1
fi

# ---- 4. record the job --------------------------------------------------------
BODY=$(printf '{"job_id":"%s","input_name":"%s","sequence_count":%d,"input_bytes":%d}' \
        "$JOB" "$NAME" "$N" "$BYTES")
resp=$(curl -sk -X POST -w '\n%{http_code}' --max-time 60 "${API}/jobs" \
         -H "X-API-Key: $ADM" -H 'content-type: application/json' --data-binary "$BODY")
code=$(printf '%s' "$resp" | tail -1)
if [[ "$code" != "201" ]]; then
  echo "$(ts) reclassify_export FAILED recording job=$JOB http=$code body=$(printf '%s' "$resp" | sed '$d') -- THE REMOTE JOB IS RUNNING; record it by hand: POST ${API}/jobs $BODY" >&2
  exit 1
fi

# ---- 5. housekeeping ----------------------------------------------------------
ACTIVE=$(curl -sk --max-time 30 "${API}/jobs?active=true&limit=500" -H "X-API-Key: $ADM" \
         | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')
ls -1t "${EXPORT_DIR}"/pending-*.fa 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "$(ts) reclassify_export ok job=$JOB sequences=$N bytes=$BYTES min_length=${MINLEN:-?} file=$NAME active_jobs=$ACTIVE"
