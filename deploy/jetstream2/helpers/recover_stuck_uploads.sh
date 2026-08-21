#!/usr/bin/env bash
# recover_stuck_uploads.sh — drive msa_ingestions rows out of 'uploaded' by
# re-calling /v1/uploads/{id}/complete. Idempotent; cron-friendly (silent on
# empty runs, lockfile-guarded so overlapping invocations are skipped cleanly).
#
# Background: a buggy classifier path used to 500 on /complete, leaving rows
# stuck in 'uploaded'. Fix is deployed (Diamond is off the upload path);
# re-running /complete now puts them through the same verify+publish pipeline
# a fresh job would hit and flips them to 'available' (or terminal 'rejected'
# if the bytes really don't verify).
#
# Cron entry (suggested — every 15 min):
#   */15 * * * * /opt/af3-cache/deploy/jetstream2/recover_stuck_uploads.sh
#
# Exits: 0 success or nothing-to-do; 1 transient error; 2 misconfiguration.

set -euo pipefail

# ---- config (override via env if needed) ------------------------------------
DEPLOY_DIR="${DEPLOY_DIR:-/opt/af3-cache/deploy/jetstream2}"
ENV_FILE="${ENV_FILE:-${DEPLOY_DIR}/.env.api}"
LOG_FILE="${LOG_FILE:-/mnt/pgdata/backups/recover_stuck_uploads.log}"
LOCK_FILE="${LOCK_FILE:-/tmp/recover_stuck_uploads.lock}"
DB_CONTAINER="${DB_CONTAINER:-af3cache-db-1}"
DB_USER="${DB_USER:-af3}"
DB_NAME="${DB_NAME:-af3cache}"
MAX_PER_RUN="${MAX_PER_RUN:-100}"        # bound a single run; rest picked up next tick
CURL_TIMEOUT="${CURL_TIMEOUT:-180}"      # /complete is bounded by verify_concurrency

# ---- preflight ---------------------------------------------------------------
[ -r "$ENV_FILE" ] || { echo "recover_stuck_uploads: missing $ENV_FILE" >&2; exit 2; }
KEY=$(grep -E '^API_KEYS='   "$ENV_FILE" | cut -d= -f2- | cut -d, -f1)
HOST=$(grep -E '^PUBLIC_HOST=' "$ENV_FILE" | cut -d= -f2-)
[ -n "$KEY"  ] || { echo "recover_stuck_uploads: no API_KEYS in $ENV_FILE"   >&2; exit 2; }
[ -n "$HOST" ] || { echo "recover_stuck_uploads: no PUBLIC_HOST in $ENV_FILE" >&2; exit 2; }
BASE_URL="https://${HOST}"

# ---- single-instance lock (silent skip if another run is active) -------------
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

ts()  { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

# ---- pull stuck ids (oldest first) -------------------------------------------
ids=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -c \
        "SELECT id FROM public.msa_ingestions
          WHERE status='uploaded'
          ORDER BY created_at
          LIMIT ${MAX_PER_RUN}") || {
  log "ERROR: psql query failed"
  echo "recover_stuck_uploads: psql query failed" >&2
  exit 1
}
N=$(printf '%s\n' "$ids" | grep -c . || true)

# nothing to do -> quiet exit (cron stays silent, log stays clean)
[ "$N" -eq 0 ] && exit 0

log "starting: ${N} stuck row(s); base=${BASE_URL}"
i=0 ok=0 rej=0 fail=0
while read -r iid; do
  [ -z "$iid" ] && continue
  i=$((i+1))
  resp=$(curl -sk --max-time "$CURL_TIMEOUT" -w $'\n%{http_code}' -X POST \
              "${BASE_URL}/v1/uploads/${iid}/complete" \
              -H "X-API-Key: ${KEY}" 2>/dev/null) || resp=$'\n000'
  http=$(printf '%s' "$resp" | tail -n1)
  body=$(printf '%s' "$resp" | sed '$d')
  status=$(printf '%s' "$body" | python3 -c \
           'import sys,json
try:    print(json.loads(sys.stdin.read() or "{}").get("status","?"))
except: print("?")' 2>/dev/null || echo "?")
  case "$status" in
    available) ok=$((ok+1));   flag="OK " ;;
    rejected)  rej=$((rej+1)); flag="REJ" ;;
    *)         fail=$((fail+1)); flag="FAIL" ;;
  esac
  log "[$i/$N] ${flag} http=${http} id=${iid} status=${status}"
done <<< "$ids"

log "done: total=${N} available=${ok} rejected=${rej} failed=${fail}"

# Emit a single summary line on stdout so cron mails when something actually
# happened (idle ticks remain silent).
echo "recover_stuck_uploads: total=${N} available=${ok} rejected=${rej} failed=${fail}"
