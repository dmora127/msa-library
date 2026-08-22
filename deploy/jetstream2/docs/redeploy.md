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
chmod +x /opt/af3-cache/deploy/jetstream2/helpers/reclassify*.sh   # no exec bit in git
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

`docker-compose.api.yml` bind-mounts three host paths. **All must exist**, or
Docker creates empty directories over them.

```bash
sudo mkdir -p /opt/diamond          # required even for CLASSIFIER_IMPL=noop
ls -l ~/private-key.pem ~/.config/pelican
```

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

# 30 * * * *   /opt/af3-cache/deploy/jetstream2/helpers/reclassify.sh >> /mnt/pgdata/backups/reclassify.log 2>&1   # diamond only
```

> **Do NOT schedule `POST /v1/admin/maintenance/run` (the mark-stale + reap job).**
> On a live instance `OSDF_CLIENT_IMPL=pelican`, so `reap()` issues real
> `pelican object rm` calls. `Community Contributed - Cached` has
> `idle_ttl_days=90`, and `last_accessed_at` only advances on a query *hit*, so
> at production query volume almost nothing is ever refreshed — enabling this
> would begin permanently deleting community-contributed `.a3m` objects. It needs
> an explicit retention decision and a `dry_run` mode first.

`MAILTO` only works if a mail transport is installed — `postfix` is masked on the
Exosphere base image, so cron mail is silently discarded until you install
`msmtp` + `msmtp-mta` and configure a relay. **Verify with a deliberate failure**;
an unverified alert path is the same as no alert path.

On-volume dumps only protect against logical errors — also take Exosphere volume
snapshots and copy dumps off-VM. The weekly job above writes to `/dev/sdb`
specifically so the two copies do not share a device.

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
