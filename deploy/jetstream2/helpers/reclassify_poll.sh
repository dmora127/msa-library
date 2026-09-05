#!/usr/bin/env bash
# Poll the NR-BLAST box for every reclassification job that is still
# QUEUED/RUNNING on our side, and store each `reclass-status <job>` JSON via
# PUT /v1/admin/classification/jobs/<job>/status. A terminal status
# (DONE/FAILED/KILLED/UNKNOWN) drops the job out of GET /jobs?active=true, so
# the next tick exits at step 1 without opening any ssh session -- polling
# only happens while a job is actually running.
#
# Cron (crontab -e):
#   */10 * * * * /opt/af3-cache/deploy/jetstream2/helpers/reclassify_poll.sh >> /mnt/pgdata/backups/reclassify_poll.log 2>&1
#
# Env overrides (defaults shown):
#   REMOTE=exouser@<blast-box-ip>
#   REMOTE_STATUS=/home/exouser/.local/bin/reclass-status   (full path: ~/.local/bin is
#                                          NOT on the PATH of a non-interactive ssh session)
#   LOCK_FILE=/tmp/reclassify_poll.lock
#
# Exit codes: 0 (nothing active, or every active job polled); 1 if any ssh or
# API call failed (cron MAILTO fires; the job is retried next tick); 2 misconfig.
# The box forgets jobs after 15 days -> UNKNOWN, which is terminal here too.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."

REMOTE="${REMOTE:-exouser@<blast-box-ip>}"
REMOTE_STATUS="${REMOTE_STATUS:-/home/exouser/.local/bin/reclass-status}"
LOCK_FILE="${LOCK_FILE:-/tmp/reclassify_poll.lock}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new)

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

if [[ ! -r ./.env.api ]]; then
  echo "$(ts) reclassify_poll: .env.api missing" >&2; exit 2
fi
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2-)
HOST=$(grep '^PUBLIC_HOST='    .env.api | cut -d= -f2-)
if [[ -z "$ADM" || -z "$HOST" ]]; then
  echo "$(ts) reclassify_poll: ADMIN_API_KEY / PUBLIC_HOST unset in .env.api" >&2; exit 2
fi

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

API="https://${HOST}/v1/admin/classification"

# ---- 1. anything to poll? ----------------------------------------------------
if ! active=$(curl -sk -f --max-time 30 "${API}/jobs?active=true&limit=500" -H "X-API-Key: $ADM"); then
  echo "$(ts) reclassify_poll FAILED listing active jobs" >&2; exit 1
fi
mapfile -t JOBS < <(printf '%s' "$active" | python3 -c '
import json, sys
for j in json.load(sys.stdin):
    print(j["job_id"], j["status"], j["sequence_count"])')
[[ ${#JOBS[@]} -eq 0 ]] && exit 0

# ---- 2. one ssh + one PUT per active job ------------------------------------
fail=0
for line in "${JOBS[@]}"; do
  read -r ID OLD RECORDS <<< "$line"
  # reclass-status prints JSON even for UNKNOWN (exit 2), so keep stdout on any rc.
  status_json=$(ssh "${SSH_OPTS[@]}" "$REMOTE" "${REMOTE_STATUS} ${ID}" 2>/dev/null) || true
  if [[ -z "$status_json" ]]; then
    echo "$(ts) reclassify_poll FAILED job=$ID: no output from '${REMOTE_STATUS} ${ID}' on $REMOTE" >&2
    fail=1; continue
  fi
  resp=$(curl -sk -X PUT -w '\n%{http_code}' --max-time 60 "${API}/jobs/${ID}/status" \
           -H "X-API-Key: $ADM" -H 'content-type: application/json' --data-binary "$status_json")
  code=$(printf '%s' "$resp" | tail -1)
  body=$(printf '%s' "$resp" | sed '$d')
  if [[ "$code" != "200" ]]; then
    echo "$(ts) reclassify_poll FAILED job=$ID http=$code body=$body remote_json=$status_json" >&2
    fail=1; continue
  fi
  # Plain string ops only: the host python may predate 3.12 (no quotes or
  # backslashes inside f-string braces there).
  printf '%s' "$body" | python3 -c '
import json, sys
j = json.load(sys.stdin)
old, records, ts = sys.argv[1], sys.argv[2], sys.argv[3]
jid = j["job_id"]
new = j["status"]
posted = j.get("posted_so_far")
posted_s = "?" if posted is None else str(posted)
line = ts + " reclassify_poll job=" + jid + " " + old + "->" + new + " posted=" + posted_s + "/" + records
if new in ("DONE", "FAILED", "KILLED", "UNKNOWN"):
    s = j.get("summary") or {}
    line += " summary=" + (",".join(k + "=" + str(v) for k, v in sorted(s.items())) if s else "none")
    if j.get("exit_code") is not None:
        line += " exit_code=" + str(j["exit_code"])
    if new in ("FAILED", "KILLED", "UNKNOWN"):
        tail = (j.get("last_status") or {}).get("log_tail") or []
        err = j.get("last_error")
        if err:
            line += " error=" + repr(err)
        for t in tail[-2:]:
            line += "\n    " + str(t)
print(line)' "$OLD" "$RECORDS" "$(ts)"
done
exit $fail
