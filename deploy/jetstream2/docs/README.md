# af3-cache on Jetstream2 — Database (phase 1)

Self-hosted **plain Postgres 16** in Docker on one Jetstream2 VM. The DB is
**private** (bound to 127.0.0.1 + closed security group); the FastAPI app
(phase 2, separate doc) is the only public surface and reaches Postgres over
the local Docker network. Data persists on an attached Jetstream2 volume.

> Jetstream2 has no managed database — this is a self-managed instance. There
> is no SLA; take volume snapshots + offsite dumps (see Backups).

## 0. Sizing

The DB stores only **metadata** (sequences, cache-entry pointers, ingestions,
settings) — the `.a3m` blobs live in OSDF, not here. It's small.

- **Instance**: `m3.medium` (8 vCPU / 30 GB) is comfortable; `m3.small` works
  for a PoC. Image: **Ubuntu 22.04** (Featured image).
- **Volume**: 100 GB (mostly headroom + on-volume backups).

## 1. Provision (Exosphere UI — https://exosphere.jetstream-cloud.org)

1. **Create instance**: pick the Ubuntu 22.04 featured image + flavor above.
   In *Advanced → Boot Script*, paste `cloud-init.yaml` from this directory
   (installs Docker).
2. **Security group**: allow **inbound TCP 22 (SSH)** only. Do **NOT** open
   5432. (Phase 2 adds 443 for the API.)
3. **Floating IP**: assign one (needed for SSH now, API later).
4. **Create + attach a 100 GB volume** to the instance.

SSH in: `ssh ubuntu@<floating-ip>` (or `exouser@`, per Exosphere).

## 2. Mount the volume (persistent PGDATA)

```bash
lsblk                                  # find the attached disk, e.g. /dev/sdb
sudo mkfs.ext4 -L pgdata /dev/sdb      # ONLY if blank — destroys data
sudo mkdir -p /mnt/pgdata
echo 'LABEL=pgdata /mnt/pgdata ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a
sudo mkdir -p /mnt/pgdata/data /mnt/pgdata/backups
df -h /mnt/pgdata                      # confirm it's the volume, not root disk
```

## 3. Get the code + secrets

```bash
sudo mkdir -p /opt/af3-cache && sudo chown "$USER" /opt/af3-cache
git clone <this-repo-url> /opt/af3-cache
cd /opt/af3-cache/deploy/jetstream2

cp .env.prod.example .env.prod
PW=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)
sed -i "s#CHANGE_ME_use_openssl_rand#${PW}#g" .env.prod
chmod 600 .env.prod
# .env.prod is gitignored (.env*); the password lives ONLY here + in backups.
```

## 4. Bring up Postgres + apply the schema

```bash
cd /opt/af3-cache/deploy/jetstream2

docker compose -f docker-compose.prod.yml up -d db
docker compose -f docker-compose.prod.yml ps          # db healthy?

# one-shot: builds the app image and runs `alembic upgrade head` (0001..0008)
docker compose -f docker-compose.prod.yml --profile tools run --rm migrate
```

Verify:

```bash
set -a; . ./.env.prod; set +a
docker compose -f docker-compose.prod.yml exec -T db \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select tablename from pg_tables where schemaname='public' order by 1;"
docker compose -f docker-compose.prod.yml --profile tools run --rm \
  --entrypoint 'alembic current' migrate            # -> 0008 (head)
```

Expect tables: `alembic_version, app_settings, msa_cache_entries,
msa_ingestions, msa_sequences, msa_sources` + view `msa_cache_lookup`, 8
sources (community `idle_ttl_days=90`), settings incl `reap_grace_hours`,
`osdf_namespace_template = pelican://{namespace}/...`, `token_ttl_seconds=1200`.

## 5. Backups + boot persistence

```bash
crontab -e
# 15 4 * * * /opt/af3-cache/deploy/jetstream2/backup.sh >> /mnt/pgdata/backups/backup.log 2>&1
```

- `restart: unless-stopped` + Docker enabled on boot ⇒ Postgres returns after
  a VM reboot; data is on the `/mnt/pgdata` volume.
- **DR**: also snapshot the Jetstream2 volume periodically and copy a dump
  off-VM. On-volume backups alone don't survive volume loss.

## Connection string (for phase 2 — the API)

- App **container** on this compose network:
  `postgresql+asyncpg://<user>:<pw>@db:5432/af3cache` (already in `.env.prod`).
- From the **VM host** (psql/uv/debug): swap host `db` → `127.0.0.1`.
- Never expose 5432 publicly; the API is the only thing that should be.

## Security checklist

- [ ] Security group: 22 in only (no 5432). Postgres bound 127.0.0.1.
- [ ] `.env.prod` is 600, gitignored, strong random password.
- [ ] Volume mounted at `/mnt/pgdata` (data NOT on the ephemeral root disk).
- [ ] Backups cron set; volume snapshots scheduled.
- [ ] OS patched (`unattended-upgrades` or periodic `apt upgrade`).

Phase 2 (API container, HTTPS via Caddy/Traefik on 443, OSPool reachability)
is a separate runbook — ping me when the DB is verified.
