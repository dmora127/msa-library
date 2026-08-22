#!/usr/bin/env bash
#
# monitoring_watchdog.sh — the dead-man's switch for the monitoring stack.
#
# Everything else in this system alerts by SENDING something. That design has a
# hole you cannot see from inside it: if Prometheus stops scraping, or Grafana
# stops evaluating rules, or the VM dies, then "no alerts" and "alerting is
# broken" look exactly the same. This script closes that hole by pinging an
# EXTERNAL service (healthchecks.io) on success. If the pings stop for any
# reason, healthchecks.io emails you. Silence becomes the signal.
#
# It deliberately checks more than liveness:
#   1. Prometheus answers queries AND its data is FRESH. A Prometheus that is up
#      but no longer scraping still answers queries -- with stale data. Checking
#      only "is the process alive" would miss that entirely.
#   2. Grafana's API is up and its database is reachable.
#   3. Grafana has EVALUATED its alert rules recently. Grafana can be perfectly
#      healthy while its ruler is wedged, in which case no alert would ever fire.
#
# Cron (every 5 min; healthchecks.io check: period 10m, grace 20m):
#   */5 * * * * /opt/af3-cache/deploy/jetstream2/helpers/monitoring_watchdog.sh
#
# Exits 0 healthy, 1 if any check fails (cron MAILTO fires too).

set -uo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/af3-cache/deploy/jetstream2}"
PROM_CONTAINER="${PROM_CONTAINER:-af3cache-prometheus-1}"
GRAFANA_CONTAINER="${GRAFANA_CONTAINER:-af3cache-grafana-1}"
# Scrape interval is 30s; 180s means several consecutive misses, not one blip.
MAX_STALENESS="${MAX_STALENESS:-180}"
# Rule group evaluates every 60s.
MAX_EVAL_AGE="${MAX_EVAL_AGE:-300}"

# Source AFTER the defaults so the env file can override, and BEFORE reading
# HC_URL -- getting this order wrong silently leaves the ping URL empty, which
# is exactly the bug this script exists to prevent (it happened in volume_guard).
# Ping URLs live beside the rest of the deploy config. Path is derived from
# this script's own location, so a clone at a different prefix still works.
_SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITORING_ENV="${MONITORING_ENV:-$_SD/../.af3cache-monitoring.env}"
[ -r "$MONITORING_ENV" ] && . "$MONITORING_ENV"
HC_URL="${HC_WATCHDOG_URL:-}"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
fail() {
  echo "$(ts) watchdog CRITICAL: $*" >&2
  [ -n "$HC_URL" ] && curl -fsS -m 10 --retry 2 "${HC_URL}/fail" >/dev/null 2>&1
  exit 1
}

# --- 1. Prometheus responds and its data is fresh ----------------------------
# time() - max(timestamp(up)) is the age of the newest scrape sample. Using
# timestamp() (the SAMPLE's time) rather than the query's own evaluation time is
# what makes this a real freshness check.
PROM_Q='time()%20-%20max(timestamp(up))'
prom_out=$(docker exec "$PROM_CONTAINER" wget -qO- \
    "http://localhost:9090/api/v1/query?query=${PROM_Q}" 2>/dev/null) \
  || fail "Prometheus container unreachable (docker exec failed)"

staleness=$(printf '%s' "$prom_out" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(d['data']['result'][0]['value'][1] if d.get('status')=='success' and d['data']['result'] else '')
except Exception:
    print('')
" 2>/dev/null)

[ -n "$staleness" ] || fail "Prometheus query returned no usable result"
awk -v s="$staleness" -v m="$MAX_STALENESS" 'BEGIN{exit !(s+0 <= m+0)}' \
  || fail "Prometheus data is stale: ${staleness}s since last scrape (max ${MAX_STALENESS}s) -- it is running but not scraping"

# --- 2. Grafana is alive -----------------------------------------------------
gr_out=$(docker exec "$GRAFANA_CONTAINER" wget -qO- \
    "http://localhost:3000/api/health" 2>/dev/null) \
  || fail "Grafana container unreachable (docker exec failed)"
printf '%s' "$gr_out" | grep -q '"database": *"ok"' \
  || fail "Grafana health check did not report database ok: $(printf '%s' "$gr_out" | tr -d '\n' | cut -c1-120)"

# --- 3. Grafana is actually evaluating alert rules ---------------------------
# Needs the admin credential; skipped (with a warning) if unreadable, because a
# missing password should not turn the whole watchdog into a false alarm.
GF_PW=$(awk -F= '/^GF_SECURITY_ADMIN_PASSWORD=/{print substr($0,index($0,"=")+1)}' \
        "$DEPLOY_DIR/.env.monitoring" 2>/dev/null)
if [ -n "$GF_PW" ]; then
  rules_out=$(docker exec "$GRAFANA_CONTAINER" wget -qO- \
      "http://admin:${GF_PW}@localhost:3000/api/prometheus/grafana/api/v1/rules" 2>/dev/null) \
    || fail "Grafana ruler API unreachable"
  eval_age=$(printf '%s' "$rules_out" | python3 -c "
import json,sys,datetime
try:
    gs=json.load(sys.stdin)['data']['groups']
    if not gs: print(''); sys.exit()
    now=datetime.datetime.now(datetime.timezone.utc)
    ages=[]
    for g in gs:
        le=g.get('lastEvaluation')
        if not le: continue
        t=datetime.datetime.fromisoformat(le.replace('Z','+00:00'))
        ages.append((now-t).total_seconds())
    print(min(ages) if ages else '')
except Exception:
    print('')
" 2>/dev/null)
  [ -n "$eval_age" ] || fail "Grafana reports no alert rule groups -- rules are not loaded"
  awk -v a="$eval_age" -v m="$MAX_EVAL_AGE" 'BEGIN{exit !(a+0 <= m+0)}' \
    || fail "Grafana has not evaluated alert rules in ${eval_age}s (max ${MAX_EVAL_AGE}s) -- ruler is wedged; alerts would never fire"
else
  echo "$(ts) watchdog WARN: could not read GF_SECURITY_ADMIN_PASSWORD; skipped the rule-evaluation check" >&2
fi

# --- healthy -----------------------------------------------------------------
[ -n "$HC_URL" ] && curl -fsS -m 10 --retry 3 "$HC_URL" >/dev/null 2>&1
exit 0
