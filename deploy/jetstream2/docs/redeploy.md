# Redeploying af3-cache on a new VM

End-to-end procedure for standing up another instance of this service from this
repo. Written against the live deployment on `af3-caching` (Jetstream2, floating
IP <api-host-ip>) and validated while building `af3-dev`.

`README.md` (phase 1) and `README-phase2.md` describe how the *original* VM was
built and still carry some stale details. **This file supersedes them for new
deployments** — differences are called out in [Corrections](#corrections-to-the-phase-12-runbooks).

---

## Phase 0 — Two decisions before you start

**1. Does the clone need production's data?** A fresh instance runs
`alembic upgrade head` and starts empty. A copy of production restores a
`pg_dump`. **You do one or the other, never both** — see Phase 6.

**2. Which OSDF mode?**

> **Do not point a second instance at the `af3-msa-library` namespace in
> `pelican` mode.** The maintenance reaper runs a real `pelican object rm` on
> entries it judges stale. A clone restored from production's dump references
> the *same* `osdf://` objects, so its reaper will delete production's `.a3m`
> files. Either stay on `dryrun` (the full lifecycle runs, the pelican commands
> are logged instead of executed), or obtain a separate namespace and issuer
> credential first.

Dev and staging instances should be `dryrun` / `dryrun` / `noop`.

---

## Phase 1 — Provision (Exosphere)

1. **Instance** — Ubuntu 22.04 featured image, `m3.medium` (`m3.small` is enough
   for a PoC). Paste `deploy/jetstream2/cloud-init.yaml` into
   *Advanced → Boot Script*; it installs Docker and enables it on boot.
2. **Security group** — inbound **TCP 22, 80, 443 only**. No 5432, and delete
   any "expose all incoming ports" rule. Postgres binds 127.0.0.1 and stays
   private.
3. **Floating IP** — assign one.
4. **Volume** — create and attach one, and **name it in Exosphere for the path
   you want**: Exosphere formats it and mounts it at `/media/volume/<name>`, so
   the volume name *is* the mount path. Production uses 20 GB (5.5 GB in use);
   100 GB+ gives room for on-volume dumps at ~356 MB each.

## Phase 2 — Data volume

The stack reaches Postgres data through `/mnt/pgdata`, which is a **symlink** to
the Exosphere mount — not a mountpoint of its own, and not an `/etc/fstab` entry.

```bash
findmnt -no SOURCE,TARGET /media/volume/<name>     # confirm Exosphere mounted it
sudo ln -s /media/volume/<name> /mnt/pgdata        # symlink FIRST
sudo mkdir -p /mnt/pgdata/data /mnt/pgdata/backups # then create dirs THROUGH it
sudo chown "$USER" /mnt/pgdata/backups
```

**Order matters.** If `/mnt/pgdata` already exists as a real directory, `ln -s`
silently creates the link *inside* it (`/mnt/pgdata/<name>`) instead of failing,
and every later `mkdir` lands on the root disk. Verify with the two commands that
actually prove it:

```bash
ls -ld /mnt/pgdata                                 # must print: lrwxrwxrwx ... -> /media/volume/<name>
df -h /mnt/pgdata                                  # must show the VOLUME, not /dev/sda1
```

If `/mnt/pgdata` is already a directory, remove it with `rmdir` (which refuses to
delete anything non-empty, so it cannot destroy a cluster you forgot about) and
then symlink:

```bash
ls -la /mnt/pgdata                                 # look before removing
sudo rm -f /mnt/pgdata/<name>                      # a stray symlink from a failed attempt
sudo rmdir /mnt/pgdata/data /mnt/pgdata/backups /mnt/pgdata
sudo ln -s /media/volume/<name> /mnt/pgdata
```

### Guard against a silent empty-volume start

Because the mount is not in `/etc/fstab`, if Docker starts before the volume is
mounted, Docker creates the missing bind path as an empty directory **on the root
disk** and Postgres initdbs a fresh empty cluster there. You get a healthy-looking
API with no data. Before bringing up `db`, and after any reboot:

```bash
findmnt -no SOURCE,TARGET /mnt/pgdata || echo "VOLUME NOT MOUNTED"
```

(Use `|| exit 1` only inside a script — in an interactive shell it logs you out.)

Optional hardening, if the volume carries a filesystem label:

```bash
echo 'LABEL=<label> /media/volume/<name> ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a && findmnt -no SOURCE,TARGET /media/volume/<name>
```

`nofail` keeps a missing volume from blocking boot, and it is idempotent with
Exosphere's own mount.

## Phase 3 — Clone the repo

The repo is private, so the VM needs its own **read-only deploy key** — do not
copy another host's personal key.

```bash
ssh-keygen -t ed25519 -C "af3-cache@$(hostname)" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Add that key under **repo → Settings → Deploy keys → Add deploy key**, leaving
*Allow write access* **unchecked**. Then:

```bash
ssh -T git@github.com                              # expect "successfully authenticated"
sudo mkdir -p /opt/af3-cache && sudo chown "$USER" /opt/af3-cache
git clone <repo-ssh-url> /opt/af3-cache
chmod +x /opt/af3-cache/deploy/jetstream2/helpers/reclassify*.sh \
         /opt/af3-cache/deploy/jetstream2/helpers/similarity_*.sh   # no exec bit in git
```

## Phase 4 — Configuration

Both env files are generated from the committed templates and never leave the VM
(they are gitignored, mode 600).

```bash
cd /opt/af3-cache/deploy/jetstream2

cp .env.prod.example .env.prod
PW=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)
sed -i "s#CHANGE_ME_use_openssl_rand#${PW}#g" .env.prod
chmod 600 .env.prod

cp .env.api.example .env.api
IP=$(curl -s ifconfig.me)
sed -i "s#CHANGE_ME.sslip.io#${IP}.sslip.io#" .env.api
sed -i "s#CHANGE_ME_same_as_POSTGRES_PASSWORD#${PW}#" .env.api
sed -i "0,/CHANGE_ME_openssl_rand_hex_32/s##$(openssl rand -hex 32)#" .env.api   # API_KEYS
sed -i "0,/CHANGE_ME_openssl_rand_hex_32/s##$(openssl rand -hex 32)#" .env.api   # ADMIN_API_KEY
sed -i "s#CHANGE_ME_openssl_rand_hex_16#$(openssl rand -hex 16)#" .env.api
chmod 600 .env.api
```

For a dev/staging instance, switch it off the live OSDF path (see Phase 0):

```bash
sed -i 's#^TOKEN_ISSUER_IMPL=.*#TOKEN_ISSUER_IMPL=dryrun#; \
        s#^OSDF_CLIENT_IMPL=.*#OSDF_CLIENT_IMPL=dryrun#; \
        s#^CLASSIFIER_IMPL=.*#CLASSIFIER_IMPL=noop#' .env.api
```

Record the keys — nothing else stores them:

```bash
grep -E '^(PUBLIC_HOST|API_KEYS|ADMIN_API_KEY|PGWEB_AUTH_PASS)=' .env.api
```

`API_KEYS` is a comma-separated set, so rotation is: append the new key, restart
`api`, hand it out, drop the old one later.

## Phase 5 — Host assets the repo cannot carry

`docker-compose.api.yml` bind-mounts four host paths. **All must exist**, or
Docker creates empty directories over them.

```bash
sudo mkdir -p /opt/diamond          # required even for CLASSIFIER_IMPL=noop
sudo mkdir -p /mnt/pgdata/similarity && sudo chown "$USER" /mnt/pgdata/similarity
ls -l ~/private-key.pem ~/.config/pelican
```

- **`/mnt/pgdata/similarity`** — `SIMILARITY_DB_ROOT` for the MMseqs2 snapshot
  mode (nightly/delta generations + symlinks). Empty is fine; unused while
  `SIMILARITY_DB_MODE=ephemeral`. Must be on the data volume (Phase 2), not
  the root disk.

- **`/opt/diamond`** — empty is fine for `noop`. For `CLASSIFIER_IMPL=diamond`,
  copy the ~3 GB binary + SwissProt DB from an existing instance:
  `rsync -avz /opt/diamond/ <user>@<new-ip>:/tmp/diamond/` then
  `sudo mv /tmp/diamond /opt/diamond`.
- **`~/.config/pelican` and `~/private-key.pem`** — only needed in `pelican`
  mode. Not reproducible; copy from an existing instance
  (`chmod 400` the key, `chmod -R go-rwx` the config dir), or mint a fresh
  issuer keypair and register it with the OSDF origin. If the new VM runs
  `dryrun`, comment out those two `volumes:` lines in `docker-compose.api.yml`
  instead — nothing reads them.

Note the host paths in `docker-compose.api.yml` are hardcoded to
`/home/exouser/...`; adjust if the account differs.

## Phase 6 — Database

Start Postgres:

```bash
cd /opt/af3-cache/deploy/jetstream2
findmnt -no SOURCE,TARGET /mnt/pgdata || echo "VOLUME NOT MOUNTED"   # last chance
docker compose -f docker-compose.prod.yml up -d db
docker compose -f docker-compose.prod.yml ps                          # wait for healthy
```

> **Pick A or B. Never both.** Running `migrate` and *then* restoring a dump
> produces a broken database — see [Pitfalls](#pitfalls).

### A. Fresh instance — apply migrations

```bash
docker compose -f docker-compose.prod.yml --profile tools run --rm migrate
docker compose -f docker-compose.prod.yml --profile tools run --rm \
  --entrypoint 'alembic current' migrate            # -> 0013 (head)
```

`migrate` builds the app image, which downloads the pinned pelican release, so
the VM needs outbound internet.

### B. Copy of production — restore a dump into an EMPTY database

Copy the dump over first:

```bash
scp <user>@<source-vm>:/mnt/pgdata/backups/af3cache_<stamp>.sql.gz /mnt/pgdata/backups/
```

Then restore, naming the file explicitly — a `af3cache_*.sql.gz` glob would
concatenate every dump in the directory:

```bash
DUMP=/mnt/pgdata/backups/af3cache_<stamp>.sql.gz

# only if the DB is not already empty (e.g. `migrate` was run by mistake):
docker compose -f docker-compose.prod.yml exec -T db \
  psql -U af3 -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS af3cache WITH (FORCE);" \
  -c "CREATE DATABASE af3cache OWNER af3;"

gunzip -c "$DUMP" | docker compose -f docker-compose.prod.yml exec -T db \
  psql -U af3 -d af3cache -v ON_ERROR_STOP=1 --single-transaction
```

`ON_ERROR_STOP=1 --single-transaction` makes the restore atomic and loud — a
clean run prints `SET`/`CREATE`/`COPY` lines and **no** `ERROR`. Without it,
psql grinds past failures and leaves a half-loaded database.

The dump carries `alembic_version`, so **do not run `migrate` afterwards.**

Dumps are taken `--no-owner --no-privileges`, so the `anon` role and its grants
from migration 0001 are not in the dump. Roles are cluster-wide and survive
`DROP DATABASE`; if the restore stops on `role "anon" does not exist`, create it
and re-run:

```bash
docker compose -f docker-compose.prod.yml exec -T db \
  psql -U af3 -d postgres -c "CREATE ROLE anon NOLOGIN;"
```

Verify the restore before continuing:

```bash
docker compose -f docker-compose.prod.yml exec -T db psql -U af3 -d af3cache -c "
select 'sequences' t, count(*)::text n from msa_sequences
union all select 'cache_entries', count(*)::text from msa_cache_entries
union all select 'ingestions',    count(*)::text from msa_ingestions
union all select 'sources',       count(*)::text from msa_sources
union all select 'alembic',       max(version_num) from alembic_version;"
```

`cache_entries` must be non-zero (a populated `msa_sequences` with an empty
`msa_cache_entries` means every query returns `found:false`) and `alembic` must
read `0013`.

## Phase 7 — API, DB console, edge

```bash
cd /opt/af3-cache/deploy/jetstream2
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml up -d --build api pgweb caddy
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml ps
```

Expect `af3cache-{db,api,pgweb,caddy}-1`. Only Caddy publishes ports (80/443).

## Phase 8 — Verify

```bash
cd /opt/af3-cache/deploy/jetstream2
H=$(grep '^PUBLIC_HOST='   .env.api | cut -d= -f2)
JOB=$(grep '^API_KEYS='    .env.api | cut -d= -f2)
ADM=$(grep '^ADMIN_API_KEY=' .env.api | cut -d= -f2)

curl -sk https://$H/healthz                                              # {"status":"ok"}
curl -sk https://$H/v1/query -H "X-API-Key: $JOB" \
     -H 'content-type: application/json' -d '{"sequence":"MKTAYIAK"}'
curl -sk https://$H/v1/admin/config -H "X-API-Key: $ADM"

docker exec af3cache-api-1 pelican --version                             # pelican mode only
docker exec af3cache-api-1 /opt/diamond/diamond --version                # diamond mode only
docker exec af3cache-api-1 mmseqs version                                # prints the build commit
curl -sk https://$H/v1/admin/similarity/status -H "X-API-Key: $ADM"       # mmseqs_version non-null
curl -sk https://$H/v2/query -H 'content-type: application/json' \
     -d '{"sequence":"MKTAYIAK","similarity_search":true}'               # 200; found:false or match_type:"similar"
df -h /mnt/pgdata                                                        # still the volume
```

`-k` is required while `CADDY_TLS=tls internal`. Swagger UI is at
`https://$H/docs`; the pgweb console at `https://$H/webdbinterface` (browser
prompts for `PGWEB_AUTH_USER`/`PGWEB_AUTH_PASS`).

## Phase 9 — Cron

All helper scripts now `cd "$HERE/.."` and work correctly from `helpers/`.
(They previously did not — that, plus cron still pointing at the pre-`helpers/`
paths, silently broke both backups for weeks. **Whenever you move or rename a
helper, run it by hand once before trusting cron.**)

```bash
crontab -e
MAILTO=you@example.edu

# Daily dump -> pgdata volume (/dev/sdd), 14-day retention.
15 4 * * *   /opt/af3-cache/deploy/jetstream2/helpers/backup.sh >> /mnt/pgdata/backups/backup.log 2>&1

# Weekly validated dump -> a DIFFERENT physical device (/dev/sdb). -Fc format,
# size floor, and a pg_restore --list integrity check.
0 6 * * 0    ENV_FILE=/opt/af3-cache/deploy/jetstream2/.env.prod BACKUP_DIR=/media/volume/application-backup/af3cache /opt/af3-cache/deploy/jetstream2/helpers/af3cache_backup.sh >> /home/exouser/logs/backup.log 2>&1

# Re-drive ingestions stuck in 'uploaded' (idempotent, lockfile-guarded).
*/15 * * * * /opt/af3-cache/deploy/jetstream2/helpers/recover_stuck_uploads.sh >> /mnt/pgdata/backups/recover_stuck_uploads.log 2>&1

# Data-volume mount guard (see Phase 2 warning). Publishes af3cache_pgdata_mounted.
*/5 * * * *  /opt/af3-cache/deploy/jetstream2/helpers/volume_guard.sh

# Monitoring dead-man's switch (Phase 10). Checks Prometheus is scraping FRESH
# data and Grafana is actually EVALUATING rules -- not just that both are alive.
*/5 * * * *  /opt/af3-cache/deploy/jetstream2/helpers/monitoring_watchdog.sh

# 30 * * * *   /opt/af3-cache/deploy/jetstream2/helpers/reclassify.sh >> /mnt/pgdata/backups/reclassify.log 2>&1   # diamond only

# MMseqs2 similarity snapshot builders (see "Similarity search" below). Safe to
# install BEFORE flipping the flags: the API answers 409 and the helpers exit 0
# quietly until SIMILARITY_NIGHTLY_BUILD_ENABLED / SIMILARITY_DELTA_BUILD_ENABLED
# are true in .env.api.
# 20 2 * * *   /opt/af3-cache/deploy/jetstream2/helpers/similarity_nightly.sh >> /mnt/pgdata/backups/similarity_nightly.log 2>&1
# 50 * * * *   /opt/af3-cache/deploy/jetstream2/helpers/similarity_delta.sh   >> /mnt/pgdata/backups/similarity_delta.log 2>&1
```

> **Do NOT schedule `POST /v1/admin/maintenance/run` (the mark-stale + reap job).**
> On a live instance `OSDF_CLIENT_IMPL=pelican`, so `reap()` issues real
> `pelican object rm` calls. `Community Contributed - Cached` has
> `idle_ttl_days=90`, and `last_accessed_at` only advances on a query *hit*, so
> at production query volume almost nothing is ever refreshed — enabling this
> would begin permanently deleting community-contributed `.a3m` objects. It needs
> an explicit retention decision and a `dry_run` mode first.

### Dead-man's switches (healthchecks.io)

Every other alert works by *sending* something, which cannot cover the case where
the sender itself is broken: a deleted cron line, a moved script, a powered-off
VM, or a wedged Prometheus all produce **silence**, which is indistinguishable
from "all is well". Four free healthchecks.io checks invert that — they alert when
pings *stop*:

| Check | Period / grace | Pinged by |
|---|---|---|
| `af3cache-backup-daily` | 1d / 4h | `helpers/backup.sh` |
| `af3cache-backup-weekly` | 7d / 12h | `helpers/af3cache_backup.sh` |
| `af3cache-volume-guard` | 10m / 20m | `helpers/volume_guard.sh` |
| `af3cache-monitoring-watchdog` | 10m / 20m | `helpers/monitoring_watchdog.sh` |

Ping URLs go in `deploy/jetstream2/.af3cache-monitoring.env` (from
`.af3cache-monitoring.env.example`, mode 600, gitignored) as
`HC_BACKUP_DAILY_URL`, `HC_BACKUP_WEEKLY_URL`, `HC_VOLUME_URL`,
`HC_WATCHDOG_URL`. The helper scripts locate it relative to their own directory,
so a clone at a different prefix works unchanged. Scripts skip the ping silently
when a URL is unset — which is exactly why the trap below matters.

> **Ordering trap, learned the hard way.** Source the env file **before** reading
> the variable out of it. Getting this backwards leaves the ping URL empty, and
> the script still exits 0 with a correct-looking log — the dead-man's switch just
> quietly does nothing, which you would only discover during a real outage.
> Verify with `bash -x <script>` and confirm a `curl` to hc-ping.com actually
> appears; do not trust the exit code.

### Mail transport

`MAILTO` only works if a mail transport is installed — `postfix` is **masked** on
the Exosphere base image, so cron mail is silently discarded until you set this
up. On one instance a failed-backup notification sat in the masked MTA's queue
for two months.

```bash
sudo apt-get install -y msmtp msmtp-mta      # msmtp-mta diverts /usr/sbin/sendmail
cp .msmtprc.example .msmtprc && chmod 600 .msmtprc   # fill in the relay creds
ln -sfn /opt/af3-cache/deploy/jetstream2/.msmtprc ~/.msmtprc
```

> **The symlink is required, not cosmetic.** msmtp reads only `~/.msmtprc` or
> `/etc/msmtprc` (`msmtp --version` lists both). When cron's `MAILTO` delivers it
> execs `/usr/sbin/sendmail` with no opportunity to pass `-C`, so the home path
> must resolve. `/etc/msmtprc` is the wrong fix: it would need to be
> world-readable for cron-as-`exouser`, leaking the relay password to every local
> account. Keep the real file mode `600` — msmtp refuses a group/world-readable
> config.

Use a dedicated throwaway account with an app password, never a personal or
NetID credential. **Verify with a deliberate failure** — an unverified alert path
is the same as no alert path:

```bash
MIN_DUMP_BYTES=999999999999 helpers/backup.sh   # must produce an email
```

On-volume dumps only protect against logical errors — also take Exosphere volume
snapshots and copy dumps off-VM. The weekly job above writes to `/dev/sdb`
specifically so the two copies do not share a device.

---

## Phase 10 — Monitoring stack

Six containers: `prometheus`, `grafana`, `node-exporter`, `cadvisor`,
`postgres-exporter`, `blackbox-exporter`. All are `expose:`-only; Grafana is
reached through Caddy at `/grafana` behind `basic_auth`.

> **The compose invocation now takes THREE `-f` files.** A two-file
> `up -d --remove-orphans` will **silently delete the entire monitoring stack**,
> because those services are not in the merged config it computes.

```bash
cd /opt/af3-cache/deploy/jetstream2
docker compose -f docker-compose.prod.yml \
               -f docker-compose.api.yml \
               -f docker-compose.monitoring.yml up -d
```

Setup, once per instance:

1. `cp .env.monitoring.example .env.monitoring && chmod 600 .env.monitoring`,
   then fill in every `CHANGE_ME`.
2. Create the read-only exporter role. **Two traps here:**
   - Role names starting with `pg_` are **reserved** by Postgres — use `af3_exporter`.
   - RLS is enabled on `msa_cache_entries`, `msa_ingestions`, `msa_sources` and
     `query_metrics`, and the existing policies only target the `anon` role. The
     app's `af3` role is superuser with `BYPASSRLS` so it never noticed. A plain
     `GRANT SELECT` is **not enough** — the exporter reads **zero rows**, which
     makes `af3cache_entries_total` report 0, i.e. exactly the signature of the
     empty-cluster boot race it exists to detect. Add an explicit SELECT policy
     per table (preferred over granting `BYPASSRLS`).
   ```sql
   CREATE ROLE af3_exporter LOGIN PASSWORD '<openssl rand -hex 24>';
   GRANT pg_monitor TO af3_exporter;
   GRANT CONNECT ON DATABASE af3cache TO af3_exporter;
   GRANT USAGE ON SCHEMA public TO af3_exporter;
   GRANT SELECT ON public.msa_cache_entries, public.msa_ingestions,
                   public.msa_sources, public.query_metrics TO af3_exporter;
   CREATE POLICY exporter_read_cache_entries ON public.msa_cache_entries
     FOR SELECT TO af3_exporter USING (true);
   -- ...repeat for msa_ingestions, msa_sources, query_metrics
   ```
3. Add `GRAFANA_PROXY_USER` / `GRAFANA_PROXY_HASH` to `.env.api`
   (`docker run --rm caddy:2 caddy hash-password --plaintext '<pw>'`), then
   **recreate Caddy** — see the warning below.

> **Caddy `basic_auth` does NOT strip the `Authorization` header.** Without
> `header_up -Authorization` in the `/grafana` reverse_proxy block, Grafana
> receives the *Caddy* credentials, fails to log in on every single request, and
> trips its brute-force lockout — locking the real admin out with
> *"too many consecutive incorrect login attempts"*. The login page still
> renders, so this looks like it works until you try to sign in.

> **Caddyfile changes need a RECREATE, not a reload.** It is a single-file bind
> mount and the running container holds a stale inode, so `caddy reload` re-reads
> the old file:
> `docker compose -f ... -f ... -f ... up -d --force-recreate caddy` (~4s downtime).

Alert thresholds worth knowing: while `CADDY_TLS=tls internal`, Caddy's internal
CA issues **12-hour** leaf certificates, so the cert-expiry alert is set to
**1 hour**, not the usual 30 days — a 30-day rule would fire on every evaluation.
After the real-cert cutover, raise it to 21 days and switch the blackbox module
from `http_2xx_insecure` to `http_2xx`.

### Application metrics (`/metrics`)

The app exposes Prometheus metrics at `/metrics`, scraped directly at
`api:8000` over the compose network. **The Caddyfile 404s `/metrics` and
`/readyz` at the edge** — without that block the catch-all would proxy them
straight through, i.e. world-readable the moment the endpoint exists.
`METRICS_ENABLED=false` in `.env.api` removes the middleware from the stack and
unmounts the endpoint entirely (restart, no rebuild) — the kill switch.

What it adds beyond the DB-shaped metrics from postgres-exporter:

| Metric | Answers |
|---|---|
| `af3cache_query_{sequences,hits}_total` | **cache hit ratio** — the table's `count` is per-REQUEST while `hit_count` is per-SEQUENCE, so those two columns cannot form a ratio |
| `af3cache_http_request_duration_seconds` | p95 latency per route |
| `af3cache_app_errors_total{code}` | `AppError`s, which were previously counted and logged NOWHERE |
| `af3cache_osdf_verify_inflight` | the only signal for a hung `pelican object get` (the timeout covers the queue wait, not the fetch) |
| `af3cache_token_issue_total{result}` | `pelican token create` failing = no job can upload, while `/healthz` stays green |
| `af3cache_classify_total{result}` | separates a genuine no-hit from diamond being broken — it returns UNCLASSIFIED on every failure path and never raises |
| `af3cache_db_pool_checked_out` | vs 60 (pool_size 20 + max_overflow 40) |
| `af3cache_similarity_total{result}` | `/v2/query` similarity outcomes: `hit`/`no_hit` are answers; `binary_missing`/`timeout`/`nonzero_exit`/`queue_timeout`/`db_missing` mean the deployment is broken. Exact-hit ratio above deliberately excludes these |
| `af3cache_similarity_stage_seconds{stage}` | where an ephemeral request spends its time (`dump`, `createdb`, `createindex`, `search`) — the number that decides `SIMILARITY_EPHEMERAL_INDEX` and the move to snapshot mode |
| `af3cache_similarity_inflight` | pinned at `SIMILARITY_MAX_CONCURRENT` = a hung mmseqs (the queue timeout covers the wait, not the search) |
| `af3cache_similarity_build_total{build_type,result}` | nightly/delta builds by outcome once snapshot mode is enabled |

> **Label cardinality is load-bearing.** Requests are labelled by route TEMPLATE
> (`/v1/uploads/{ingestion_id}/complete`), never the raw path — there are 423k+
> ingestion ids. Anything unrouted collapses to the literal `<unmatched>`, so
> internet background noise against a public IP cannot mint one permanent series
> per junk URL. If you add middleware, keep it OUTSIDE the router or
> `scope["route"]` will not be populated.

### Running the test suite

`tests/conftest.py` defaults to `postgresql://af3:af3@localhost:5432`, which **on a
deployed VM is the production database** (the password differs, so it fails
rather than connects — but do not rely on that). Two safe options.

**Option A — throwaway Postgres on another port** (fastest; runs the suite with
the host's `uv`). `AF3_TEST_PG_HOST` / `AF3_TEST_PG_PORT` redirect the harness;
production on 5432 is never addressed:

```bash
docker run -d --name af3cache-test-pg -p 127.0.0.1:5433:5432 \
  -e POSTGRES_USER=af3 -e POSTGRES_PASSWORD=af3 -e POSTGRES_DB=af3cache postgres:16
AF3_TEST_PG_PORT=5433 uv run pytest -q
# the real-mmseqs integration tests are skipped unless a binary is reachable:
SIMILARITY_MMSEQS_BINARY=$(docker run --rm --entrypoint sh af3cache-api -c 'command -v mmseqs') # inside the image
SIMILARITY_MMSEQS_BINARY=/path/to/mmseqs AF3_TEST_PG_PORT=5433 uv run pytest -q tests/test_similarity_integration.py
docker rm -f af3cache-test-pg
```

**Option B — everything inside a container network namespace:**

```bash
docker run -d --name af3-pgtest -e POSTGRES_USER=af3 -e POSTGRES_PASSWORD=af3 \
           -e POSTGRES_DB=postgres postgres:16
docker run --rm --network container:af3-pgtest -v /opt/af3-cache:/w -w /w \
  -e PYTHONPATH=/w -e HOME=/tmp --entrypoint sh af3cache-api -c \
  '/bin/uv pip install -q pytest pytest-asyncio && python -m pytest -q'
docker rm -f af3-pgtest
```

`--network container:` makes `localhost` inside the test container resolve to the
throwaway, so production is unreachable by construction. Afterwards remove the
root-owned `__pycache__` the container leaves behind:
`sudo find /opt/af3-cache -name __pycache__ -type d -exec rm -rf {} +`

> **Known failures (pre-existing, 3 of 168):** `test_no_credential_rejected`,
> `test_complete_requires_api_key`, `test_request_requires_api_key` all fail with
> `TypeError: not all arguments converted during string formatting`.
> `RedactSecretsFilter._KV_RE` in `app/core/logging.py` matches the format string's
> OWN placeholder in `app/core/security.py` (`x-api-key=%s` becomes
> `x-api-key=<redacted>`), leaving zero placeholders with two args. In production
> this silently kills that auth-rejection log line. Scheduled for the phase 4
> logging rework.

The `af3cache_reap_exposure` metric is **read-only**. It counts what the
maintenance/reap job *would* delete. That job is deliberately not scheduled — see
the Phase 9 warning.

---

## Updating an instance afterwards

```bash
cd /opt/af3-cache
git pull
docker compose -f deploy/jetstream2/docker-compose.prod.yml \
               -f deploy/jetstream2/docker-compose.api.yml up -d --build api
```

`.env.api` / `.env.prod` sit inside the tree but are gitignored, so a pull never
touches them. Do not rsync over a git working tree.

## Pitfalls

Each of these was hit for real while building `af3-dev`.

| Symptom | Cause | Fix |
|---|---|---|
| `df -h /mnt/pgdata` shows `/dev/sda1` | `/mnt/pgdata` is a real directory on the root disk; Postgres would initdb there and ignore the volume | Phase 2 — `rmdir` it, then symlink, then `mkdir` through the link |
| `ln: failed to create symbolic link '/mnt/pgdata/<name>': File exists` | `ln -s` was pointed at an existing *directory*, so it created the link inside it | Remove the stray inner symlink, `rmdir` the directory, re-link |
| Restore prints dozens of `already exists` errors, then FK violations on `msa_cache_entries` / `msa_ingestions` | `migrate` ran before the restore, so the FKs were enforced *during* `COPY` instead of being added after it; the affected tables end up **empty** | Phase 6 B — `DROP DATABASE ... WITH (FORCE)`, recreate, restore with `ON_ERROR_STOP=1 --single-transaction` |
| Restore appears to work but the API returns `found:false` for everything | `msa_sequences` loaded, `msa_cache_entries` did not | Same as above; verify with the row-count query |
| `api` container starts but classification/token issuance misbehaves | a bind-mounted host path did not exist, so Docker created an empty directory over it | Phase 5 |
| Cron backup silently stops producing dumps | helper scripts resolve `./.env.prod` and the compose file relative to `helpers/` | Phase 9 |

## Corrections to the phase-1/2 runbooks

`README.md` and `README-phase2.md` predate the current deployment; where they
disagree with this file, this file is right.

- **Volume**: they describe `mkfs.ext4` + a `LABEL=pgdata /mnt/pgdata` fstab
  entry. That is not how `af3-caching` is set up — there is no such fstab line;
  Exosphere mounts the volume at `/media/volume/<name>` and `/mnt/pgdata` is a
  symlink to it. `restart_procedures.md`'s claim that `/mnt/pgdata` is
  "`/etc/fstab`-mounted" is likewise wrong.
- **Alembic head** is `0013`, not `0008`.
- **Code delivery**: `README-phase2.md` §1 documents an rsync from a Mac. That
  predates the git repo; use `git clone` / `git pull`.
- **`osdf_namespace_template`**: the code default is
  `osdf:///af3-msa-library/community-contributed/{ingestion_id}.a3m`, not the
  `pelican://{namespace}/...` form the phase-1 doc quotes.
- **Helper scripts** moved into `helpers/` and the docs into `docs/`; paths in
  the older files still point at the old locations.

## Similarity search (MMseqs2) — modes and how to switch

`POST /v2/query` with `"similarity_search": true` runs an MMseqs2 search over
every sequence that has an **active** cache entry when the exact lookup misses.
Everything lives under `app/services/similarity/`; the binary is baked into the
image (Dockerfile `mmseqs` stage). Two target strategies, `SIMILARITY_DB_MODE`:

| mode | what a request does | operations |
|---|---|---|
| `ephemeral` (default) | dumps the active table into a private `query_similarity_*` temp dir, `createdb` + `createindex`, searches, deletes the dir | none — but the whole table is rebuilt per request; keep `SIMILARITY_MAX_CONCURRENT=1` and `SIMILARITY_MMSEQS_SPLIT_MEMORY_LIMIT` set, watch `af3cache_similarity_stage_seconds` |
| `snapshot` | searches the indexed nightly generation and the unindexed hot delta under `SIMILARITY_DB_ROOT`, merges, dedups | needs the two builders below on cron |

**Measured on the production table, 2026-09-04** (597k active sequences,
257 MB FASTA; 8-CPU VM; mmseqs 18-8cc5c, 4 threads, `--split-memory-limit 2G`):

| stage | wall | peak RSS |
|---|---|---|
| dump (psql) | 1.8 s | — |
| `createdb` | 2.1 s | 0.27 GB |
| `createindex` | 31.3 s | **1.8 GB** (3.1 GB on disk) |
| `search`, indexed | 0.33 s | 0.25 GB |
| `search`, NO index | 0.31 s | 0.25 GB |

Same 7 hits either way. For a single-query ephemeral search the precomputed
index buys nothing and costs ~31 s plus 1.8 GB RAM per request — more than the
api container's `mem_limit: 2g`. So in ephemeral mode either set
`SIMILARITY_EPHEMERAL_INDEX=false` (request ≈ 5 s, ~0.3 GB) or raise
`mem_limit` before enabling. The index pays off only in snapshot mode, where the
nightly builds it once and every request reuses it.

**Enabling snapshot mode** (all config; no code or schema change):

1. Phase 5: `/mnt/pgdata/similarity` exists and is bind-mounted (`docker-compose.api.yml`).
2. In `.env.api` set `SIMILARITY_NIGHTLY_BUILD_ENABLED=true` and
   `SIMILARITY_DELTA_BUILD_ENABLED=true`; `docker compose -f docker-compose.api.yml up -d api`.
3. Run the nightly ONCE by hand and check it: `helpers/similarity_nightly.sh`,
   then `curl -sk https://$H/v1/admin/similarity/status -H "X-API-Key: $ADM"`
   shows `nightly.generation` and `nightly.indexed: true`.
4. Run `helpers/similarity_delta.sh` once (an empty delta is fine).
5. Uncomment the two cron lines in Phase 9.
6. Set `SIMILARITY_DB_MODE=snapshot` in `.env.api` and restart `api`.
   Flipping back is `SIMILARITY_DB_MODE=ephemeral` + restart.

How the snapshot generations behave: every build writes a NEW immutable dir
(`nightly-build-<ts>-DB/`, `delta-<ts>-DB/`) with `targetDB*` and a
`manifest.json` (Postgres high-water mark + snapshot time captured in the same
repeatable-read transaction as the dump), then atomically swaps the
`nightly-buildDB` / `deltaDB` symlink. A failed build leaves the active symlink
untouched. The delta is always a FULL rebuild of everything newer than the
**active nightly's** manifest (never "since the last delta"), so a new nightly
automatically shrinks the next delta to zero. Old generations are deleted only
after `SIMILARITY_*_RETENTION_MINUTES` (default 60) and never while they are the
active target, so in-flight requests that resolved the old symlink finish
safely. Requests resolve both symlinks once, up front.

Kill switches: `SIMILARITY_SEARCH_ENABLED=false` makes opted-in requests return
409 `similarity_disabled` (exact lookups unaffected); the builders answer 409
`similarity_build_disabled` while their flags are false.
