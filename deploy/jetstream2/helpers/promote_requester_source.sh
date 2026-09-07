#!/usr/bin/env bash
# promote_requester_source.sh — nightly: re-source cache entries contributed by
# one requester (default pipeline-bot@site-a.example.edu) from
# 'Community Contributed - Cached' to 'OSG-Generated'.
#
# Why: this requester's uploads are OSPool/CHTC-generated MSAs that arrive
# through the community upload pipeline (so they publish as community). The
# OSG-Generated source has no idle TTL, so promoting them also makes them
# permanent. Only rows whose CURRENT source is FROM_SOURCE are touched.
#
# Runs promote_requester_source.sql (same directory) in one transaction. Rows
# that would collide with an existing TO_SOURCE entry (a re-upload of an
# already-promoted sequence) are skipped and logged, not fatal. Idempotent;
# cron-friendly (silent on empty runs, lockfile-guarded so overlapping
# invocations are skipped cleanly).
#
# Cron entry (suggested — nightly at 00:00):
#   0 0 * * * /opt/af3-cache/deploy/jetstream2/helpers/promote_requester_source.sh >> /mnt/pgdata/backups/promote_requester_source.log 2>&1
#
# Exits: 0 success or nothing-to-do; 1 SQL failure (transaction rolled back).

set -euo pipefail

# ---- config (override via env if needed) ------------------------------------
REQUESTER="${REQUESTER:-pipeline-bot@site-a.example.edu}"
FROM_SOURCE="${FROM_SOURCE:-Community Contributed - Cached}"
TO_SOURCE="${TO_SOURCE:-OSG-Generated}"
DB_CONTAINER="${DB_CONTAINER:-af3cache-db-1}"
DB_USER="${DB_USER:-af3}"
DB_NAME="${DB_NAME:-af3cache}"
LOCK_FILE="${LOCK_FILE:-/tmp/promote_requester_source.lock}"
SQL_FILE="$(cd "$(dirname "$0")" && pwd)/promote_requester_source.sql"

[ -r "$SQL_FILE" ] || { echo "promote_requester_source: missing $SQL_FILE" >&2; exit 2; }

# ---- single-instance lock (silent skip if another run is active) -------------
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

ts()  { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# ---- run ---------------------------------------------------------------------
# -v binds the psql :'vars' used by the SQL file.
out=$(docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" \
        -v ON_ERROR_STOP=1 -qtA \
        -v requester="$REQUESTER" \
        -v from_source="$FROM_SOURCE" \
        -v to_source="$TO_SOURCE" \
        -f - < "$SQL_FILE" 2>&1) || {
  printf '%s ERROR: %s\n' "$(ts)" "$out"
  exit 1
}

# psql -qtA prints only the summary row: "<n_promoted> <n_skipped> <ids|->".
read -r n_promoted n_skipped skipped_ids <<<"$(printf '%s\n' "$out" | grep -E '^[0-9]+ [0-9]+ ' | tail -1)"
n_promoted="${n_promoted:-0}"; n_skipped="${n_skipped:-0}"

# nothing to do -> quiet exit (cron stays silent, log stays clean)
[ "$n_promoted" -eq 0 ] && [ "$n_skipped" -eq 0 ] && exit 0

msg="promoted ${n_promoted} entries for ${REQUESTER}: ${FROM_SOURCE} -> ${TO_SOURCE}"
[ "$n_skipped" -gt 0 ] && msg="${msg}; skipped ${n_skipped} (already have a ${TO_SOURCE} sibling; cache_entry ids ${skipped_ids})"
printf '%s %s\n' "$(ts)" "$msg"
