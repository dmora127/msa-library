#!/usr/bin/env bash
# Poll the NR-BLAST box for every reclassification job still QUEUED/RUNNING:
# POST /v1/admin/classification/jobs/poll. The API runs `reclass-status` over
# ssh for each active job and stores the JSON; when nothing is active it
# returns immediately without opening any ssh session, so this is free to run
# often. A terminal status (DONE/FAILED/KILLED/UNKNOWN) stops the polling.
#
# Cron (crontab -e):
#   */10 * * * * /opt/af3-cache/deploy/jetstream2/helpers/reclassify_poll.sh >> /mnt/pgdata/backups/reclassify_poll.log 2>&1
#
# Env overrides: CURL_MAX_TIME=600, LOCK_FILE=/tmp/reclassify_poll.lock.
# Exit codes: 0 (nothing active, or every active job polled); 1 if the API
# call failed or any job's remote status failed (cron MAILTO fires; retried
# next tick); 2 misconfiguration.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."

CURL_MAX_TIME="${CURL_MAX_TIME:-600}"
LOCK_FILE="${LOCK_FILE:-/tmp/reclassify_poll.lock}"

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

resp=$(curl -sk -X POST -w '\n%{http_code}' --max-time "$CURL_MAX_TIME" \
         "https://${HOST}/v1/admin/classification/jobs/poll" -H "X-API-Key: $ADM")
code=$(printf '%s' "$resp" | tail -1)
body=$(printf '%s' "$resp" | sed '$d')
if [[ "$code" != "200" ]]; then
  echo "$(ts) reclassify_poll FAILED http=$code body=$body" >&2; exit 1
fi

# One line per polled job; summary + log tail on terminal states; FAILED lines
# for jobs whose remote status could not be fetched. Plain string ops only.
printf '%s' "$body" | python3 -c '
import json, sys
r = json.load(sys.stdin); ts = sys.argv[1]
for p in r["polled"]:
    j = p["job"]; new = j["status"]
    posted = j.get("posted_so_far"); posted_s = "?" if posted is None else str(posted)
    line = (ts + " reclassify_poll job=" + j["job_id"] + " " + p["previous_status"] + "->" + new
            + " posted=" + posted_s + "/" + str(j["sequence_count"]))
    if new in ("DONE", "FAILED", "KILLED", "UNKNOWN"):
        s = j.get("summary") or {}
        line += " summary=" + (",".join(k + "=" + str(v) for k, v in sorted(s.items())) if s else "none")
        if j.get("exit_code") is not None:
            line += " exit_code=" + str(j["exit_code"])
        if new != "DONE":
            if j.get("last_error"):
                line += " error=" + repr(j["last_error"])
            for t in ((j.get("last_status") or {}).get("log_tail") or [])[-2:]:
                line += "\n    " + str(t)
    print(line)
for f in r["failed"]:
    print(ts + " reclassify_poll FAILED job=" + f["job_id"] + ": " + f["error"], file=sys.stderr)
sys.exit(1 if r["failed"] else 0)' "$(ts)"
