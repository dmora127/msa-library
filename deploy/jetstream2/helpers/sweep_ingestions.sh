#!/usr/bin/env bash
# sweep_ingestions.sh — drive the abandoned-ingestion sweeper
# (POST /v1/admin/maintenance/sweep-ingestions). Cron-friendly: silent on
# empty runs, lockfile-guarded so overlapping invocations are skipped cleanly.
#
# What the API does per run (policy lives server-side, SWEEP_* in .env.api):
#   - every pending/uploaded row older than 30 min is re-run through the normal
#     verify+publish pipeline (fetch from OSDF, verify sha/size/format, publish);
#   - a row that still cannot be verified once it is older than 24 h is
#     rejected as "Expired - Upload unverifiable";
#   - if every attempt in a run failed with OSDF unavailable, expiries are held
#     (an OSDF outage must not turn real uploads into rejections).
#
# Cron entry (every 15 min):
#   */15 * * * * /opt/af3-cache/deploy/jetstream2/helpers/sweep_ingestions.sh >> /mnt/pgdata/backups/sweep_ingestions.log 2>&1
#
# SWEEP_DRY_RUN=1 asks the API to report what it WOULD do without touching rows.
# Exits: 0 success or nothing-to-do; 1 transient error; 2 misconfiguration.

set -euo pipefail

# ---- config (override via env if needed) ------------------------------------
DEPLOY_DIR="${DEPLOY_DIR:-/opt/af3-cache/deploy/jetstream2}"
ENV_FILE="${ENV_FILE:-${DEPLOY_DIR}/.env.api}"
LOG_FILE="${LOG_FILE:-/mnt/pgdata/backups/sweep_ingestions.log}"
LOCK_FILE="${LOCK_FILE:-/tmp/sweep_ingestions.lock}"
CURL_TIMEOUT="${CURL_TIMEOUT:-700}"      # > the API's SWEEP_BUDGET_SECONDS (600)
SWEEP_DRY_RUN="${SWEEP_DRY_RUN:-0}"

# ---- preflight ---------------------------------------------------------------
[ -r "$ENV_FILE" ] || { echo "sweep_ingestions: missing $ENV_FILE" >&2; exit 2; }
KEY=$(grep -E '^ADMIN_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
HOST=$(grep -E '^PUBLIC_HOST='   "$ENV_FILE" | cut -d= -f2-)
[ -n "$KEY"  ] || { echo "sweep_ingestions: no ADMIN_API_KEY in $ENV_FILE" >&2; exit 2; }
[ -n "$HOST" ] || { echo "sweep_ingestions: no PUBLIC_HOST in $ENV_FILE"   >&2; exit 2; }
BASE_URL="${BASE_URL:-https://${HOST}}"   # override for local testing (http://127.0.0.1:PORT)

# ---- single-instance lock (silent skip if another run is active) -------------
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

ts()  { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

body='{}'
[ "$SWEEP_DRY_RUN" = "1" ] && body='{"dry_run": true}'

resp=$(curl -sk --max-time "$CURL_TIMEOUT" -w $'\n%{http_code}' -X POST \
            "${BASE_URL}/v1/admin/maintenance/sweep-ingestions" \
            -H "X-API-Key: ${KEY}" -H 'content-type: application/json' \
            -d "$body" 2>/dev/null) || resp=$'\n000'
http=$(printf '%s' "$resp" | tail -n1)
json=$(printf '%s' "$resp" | sed '$d')

if [ "$http" != "200" ]; then
  log "ERROR: http=${http} body=$(printf '%s' "$json" | head -c 300)"
  echo "sweep_ingestions: http=${http}" >&2
  exit 1
fi

# One summary line + one line per expired/error row. Quiet when nothing was scanned.
summary=$(SWEEP_JSON="$json" python3 - <<'PY'
import json, os, sys
d = json.loads(os.environ["SWEEP_JSON"])
if d["scanned"] == 0:
    sys.exit(0)
print("scanned=%d attempted=%d published=%d rejected=%d unverified=%d expired=%d "
      "held=%d errors=%d skipped=%d exhausted=%s dry_run=%s %ss" % (
          d["scanned"], d["attempted"], d["published"], d["rejected_verify"],
          d["unverified"], d["expired"], d["expiries_held"], d["errors"],
          d["skipped_budget"], d["exhausted"], d["dry_run"], d["elapsed_seconds"]))
for it in d["items"]:
    if it["outcome"] in ("expired", "error", "would_expire", "rejected_verify"):
        print("  %-16s id=%s before=%s age=%dh %s" % (
            it["outcome"], it["id"], it["status_before"],
            it["age_seconds"] // 3600, it.get("detail") or ""))
PY
) || { log "ERROR: could not parse response"; exit 1; }

[ -z "$summary" ] && exit 0            # nothing scanned -> cron stays silent

printf '%s\n' "$summary" | while IFS= read -r line; do log "$line"; done
# stdout -> cron mails only when something happened
printf 'sweep_ingestions: %s\n' "$(printf '%s\n' "$summary" | head -n1)"
