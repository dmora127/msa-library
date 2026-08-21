# Jetstream2 — restart procedures (after VM reboot)

Most of the af3-cache stack auto-recovers — only a couple things need a manual
nudge.

## What auto-recovers on reboot

- **Docker daemon** — enabled on boot (cloud-init did `systemctl enable docker`).
- **`db`, `api`, `pgweb`, `caddy` containers** — `restart: unless-stopped`
  brings them back automatically once Docker is up.
- **pgweb console** — now a first-class compose service at
  `https://$PUBLIC_HOST/webdbinterface` (no more SSH tunnel); recovers with the
  rest of the stack.
- **Postgres data** — `/mnt/pgdata` is `/etc/fstab`-mounted (the line added at
  first setup); the symlink to `/media/volume/af3-cache-volume` survives reboots.
- **Caddy's local cert** — persisted in the `caddy_data` named volume.
- **`/opt/af3-cache/`, `.env.api`, `.env.prod`** — they're files on the root disk.
- **Cron jobs** (`backup.sh`, `reclassify.sh`) — crontab persists.

## What does NOT auto-recover

- **`nohup reclassify_drain.sh`** — foreground bash process; the reboot kills
  it. Restart it manually if it was mid-drain (safe to re-run — cursor restarts
  at 0 and the `WHERE` skips already-classified rows, so it just resumes
  draining the remaining backlog).
- **Anything you started via `docker compose down`** instead of just `stop` —
  those are *removed*, not stopped; restart policy doesn't help.

## Recipe after a reboot

```bash
ssh exouser@af3-caching

# 0. Wait ~30s for Docker to come up after boot, then check everything
systemctl is-active docker          # -> active
docker ps                            # expect af3cache-{db,api,caddy}-1 healthy
df -h /mnt/pgdata                    # confirm volume is mounted
curl -sk https://<api-host-ip>.sslip.io/healthz   # {"status":"ok"}
crontab -l                            # backup + reclassify lines still there

# 1. If anything is missing (rare — only if a container actually failed):
cd /opt/af3-cache/deploy/jetstream2
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml up -d
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml ps
# expect af3cache-{db,api,pgweb,caddy}-1 ; the DB console lives at
#   https://<api-host-ip>.sslip.io/webdbinterface  (browser prompts for the
#   PGWEB_AUTH_USER/PGWEB_AUTH_PASS basic-auth credentials from .env.api)

# 2. (Optional) Resume the backlog drain if it didn't finish:
cd /opt/af3-cache/deploy/jetstream2
nohup ./reclassify_drain.sh 500 \
  >> /mnt/pgdata/backups/reclassify_drain.log 2>&1 &
echo "drain pid=$!"
```

## Three quick sanity checks

```bash
# A: the actual API works end to end (auth + DB roundtrip)
ADM=$(grep '^ADMIN_API_KEY=' /opt/af3-cache/deploy/jetstream2/.env.api | cut -d= -f2)
curl -sk https://<api-host-ip>.sslip.io/v1/admin/config -H "X-API-Key: $ADM"

# B: diamond still reachable inside the api container (the /opt/diamond mount)
docker exec af3cache-api-1 /opt/diamond/diamond --version 2>&1 | head -1

# C: the cron jobs aren't behind on logs (last backup, last reclassify)
ls -lt /mnt/pgdata/backups/ | head
tail -3 /mnt/pgdata/backups/reclassify.log 2>/dev/null
```

## Things that survive a reboot AND even a `docker compose pull && up -d`

So you don't lose them:

- Postgres data (`/mnt/pgdata` host bind)
- Caddy certs + ACME state (`caddy_data` named volume)
- `.env.api` / `.env.prod` (mode 600 files on disk)
- Any classification work in progress (it's all in Postgres)

## Things you'd lose only if you delete a volume

- Reset Postgres: `docker volume rm` or `rm -rf /mnt/pgdata/data/*` — **don't**.
- Reset Caddy certs: `docker volume rm af3cache_caddy_data` — would force a
  fresh internal-CA cert (harmless during PoC; just makes clients re-trust).

## Triage when something doesn't come back

```bash
docker ps -a                                            # stopped containers + exit reasons
journalctl -u docker --since "5 min ago" | tail        # docker daemon errors
docker compose -f /opt/af3-cache/deploy/jetstream2/docker-compose.prod.yml \
  -f /opt/af3-cache/deploy/jetstream2/docker-compose.api.yml logs --tail=80
```
