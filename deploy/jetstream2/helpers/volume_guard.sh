#!/usr/bin/env bash
# volume_guard.sh — verify the Postgres data volume is actually mounted.
#
# Why this exists: /mnt/pgdata is a symlink to /media/volume/af3-cache-volume,
# mounted by Exosphere through a udev-triggered automount unit that has NO
# ordering relationship to docker.service, and is deliberately NOT in
# /etc/fstab. If Docker wins that boot race, Postgres can come up on an empty
# root-disk directory and /healthz will cheerfully return {"status":"ok"} --
# "a healthy-looking API with no data" (docs/redeploy.md).
#
# docker-compose.prod.yml's create_host_path:false is the primary defence (db
# refuses to start). This script is the detector: it runs from cron and, once
# Phase 2 lands, publishes a metric for node_exporter's textfile collector.
#
# Cron (every 5 min):
#   */5 * * * * /opt/af3-cache/deploy/jetstream2/helpers/volume_guard.sh
#
# Exits 0 = healthy, 1 = NOT mounted correctly (cron MAILTO fires).

set -uo pipefail

TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile}"

# Source FIRST, then read. (Assigning HC_URL before this line silently left it
# empty, so the dead-man's-switch ping never fired -- caught by `bash -x`.)
[ -r "${HOME:-/home/exouser}/.af3cache-monitoring.env" ] && . "${HOME:-/home/exouser}/.af3cache-monitoring.env"
HC_URL="${HC_VOLUME_URL:-}"          # healthchecks.io ping URL, optional

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

ok=1
reason=""

# 1. Something is mounted at the target.
findmnt -no SOURCE,TARGET /mnt/pgdata >/dev/null 2>&1 || { ok=0; reason="not a mountpoint"; }
# 2. It is the RIGHT filesystem, not a fresh empty dir that happens to exist.
[ "$ok" = 1 ] && { [ -f /mnt/pgdata/.volume-ok ] || { ok=0; reason="sentinel .volume-ok missing"; }; }
# 3. The cluster directory is present.
[ "$ok" = 1 ] && { [ -d /mnt/pgdata/data ] || { ok=0; reason="data/ missing"; }; }

# Publish for Prometheus (Phase 2). Atomic rename so a scrape never sees a
# half-written file. Skipped silently until the textfile dir exists.
if [ -d "$TEXTFILE_DIR" ] && [ -w "$TEXTFILE_DIR" ]; then
  tmp="$TEXTFILE_DIR/.pgdata.prom.$$"
  {
    echo "# HELP af3cache_pgdata_mounted Postgres data volume is correctly mounted (1) or not (0)."
    echo "# TYPE af3cache_pgdata_mounted gauge"
    echo "af3cache_pgdata_mounted $ok"
  } > "$tmp" && mv "$tmp" "$TEXTFILE_DIR/pgdata.prom"
fi

if [ "$ok" != 1 ]; then
  echo "$(ts) volume_guard CRITICAL: /mnt/pgdata is not the attached volume ($reason)" >&2
  exit 1
fi

# Dead-man's switch: silence here means this check itself stopped running.
[ -n "$HC_URL" ] && curl -fsS -m 10 --retry 3 "$HC_URL" >/dev/null 2>&1
exit 0
