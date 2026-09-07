# af3-cache on Jetstream2 — API + HTTPS (phase 2)

Adds the public FastAPI service in front of the phase-1 private DB:

```
OSPool job ──HTTPS──> Caddy (:443, auto LE cert) ──> api:8000 ──> db:5432
```

**Simulation mode**: `TOKEN_ISSUER_IMPL=dryrun` + `OSDF_CLIENT_IMPL=dryrun` —
the API runs the full lifecycle but **logs the exact `pelican token create` /
`pelican object get` commands instead of executing them** (the OSG issuer/store
credential isn't ready). Uploads publish trusting the job-declared sha/size.
Swap both to `pelican` later — no other change.

Prereq: phase-1 DB is up (`docker compose -f docker-compose.prod.yml ps` shows
`db` healthy).

## 1. Re-sync the code (it changed since phase 1)

On your **Mac**:

```bash
cd /path/to/af3-cache
rsync -avz --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' \
  --exclude '.env' --exclude '.env.*' --exclude '*.env*' \
  --exclude 'supabase/.temp' --exclude 'supabase/.branches' \
  --exclude '.claude' \
  ./ exouser@af3-caching:/opt/af3-cache/
```

(`.env.prod`/`.env.api` on the VM are untouched — they're excluded both ways.)

## 2. Close the security-group gate + open HTTPS

In Exosphere/Horizon → Security Groups, end state **exactly**:

| Dir | Proto | Port | Remote | |
|---|---|---|---|---|
| in | TCP | 22 | 0.0.0.0/0 ::/0 | keep (SSH) |
| in | TCP | **80** | 0.0.0.0/0 | **add** (ACME challenge) |
| in | TCP | **443** | 0.0.0.0/0 | **add** (public API) |
| in | TCP | **Any** | 0.0.0.0/0 | **DELETE** ("expose all incoming ports") |
| in | ICMP / UDP 60000-61000 | | | keep (ping/Mosh, optional) |
| out | any | any | any | keep |

Postgres stays private (127.0.0.1-bound + no 5432 rule). Only 22/80/443 in.

## 3. Write `.env.api` (generated keys)

On the **VM**:

```bash
cd /opt/af3-cache/deploy/jetstream2
IP=$(curl -s ifconfig.me)            # the VM's public/floating IP
JOBKEY=$(openssl rand -hex 32)
ADMKEY=$(openssl rand -hex 32)
cat > .env.api <<EOF
API_KEYS=$JOBKEY
ADMIN_API_KEY=$ADMKEY
TOKEN_ISSUER_IMPL=dryrun
OSDF_CLIENT_IMPL=dryrun
PELICAN_NAMESPACES_FILE=/etc/af3cache/namespaces.yaml
PUBLIC_HOST=${IP}.sslip.io
CADDY_TLS=tls internal
EOF
chmod 600 .env.api
echo "PUBLIC_HOST=${IP}.sslip.io"
echo "JOB  X-API-Key: $JOBKEY"
echo "ADMIN X-API-Key: $ADMKEY"
```

**Copy those two keys somewhere safe** — they're only in `.env.api` (mode 600,
gitignored). The job key goes to OSPool submitters; the admin key stays with you.

## 4. Bring up the API + Caddy

```bash
cd /opt/af3-cache/deploy/jetstream2
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml \
  up -d --build api caddy
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml ps
```

With `CADDY_TLS=tls internal` (sslip.io), Caddy issues a local cert instantly
— Let's Encrypt **rate-limits the shared `sslip.io` domain**, so a real LE cert
there is unreliable. For a trusted cert (required before real OSPool jobs),
point a domain you control at the floating IP and set `CADDY_TLS=` (empty) in
`.env.api`, then restart caddy. Watch startup:

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml \
  logs --tail=30 caddy
```

## 5. Verify (run from your Mac or anywhere)

```bash
H=<ip>.sslip.io
curl -s https://$H/healthz                       # {"status":"ok"} over real TLS
curl -s https://$H/v1/query -H "X-API-Key: $JOBKEY" \
  -H 'content-type: application/json' \
  -d '{"sequence":"MKTAYIAK"}'                    # {"results":[{"found":false}]}
curl -s https://$H/v1/admin/config -H "X-API-Key: $ADMKEY"
```

Exercise the **simulated** upload + see the logged pelican commands:

```bash
SHA=$(printf x | sha256sum | cut -d' ' -f1)
curl -s https://$H/v1/uploads/request -H "X-API-Key: $JOBKEY" \
  -H 'content-type: application/json' \
  -d "{\"sequence\":\"MKSIMSEQ\",\"db_version\":\"uniref-2026-01\",\
\"declared_size_bytes\":321,\"declared_sha256\":\"$SHA\"}"
# -> note ingestion_id + osdf:///af3-msa-library/community-contributed/<uuid>.a3m
#    osdf_uri + a SIMULATED token

# on the VM, see the exact command it WOULD have run:
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml \
  logs api | grep SIMULATE
# "SIMULATE token issuance — would run: pelican token create \
#  osdf:///af3-msa-library/community-contributed/<uuid>.a3m --write \
#  --issuer <ISSUER for unconfigured namespace> -k <PRIVATE_KEY> \
#  --scope-path <SCOPE_PATH> --lifetime 1200"
# (placeholders until namespaces.yaml exists; with it, the real issuer, key
#  path and --scope-path /community-contributed/<uuid>.a3m are logged)
```

Then `POST https://$H/v1/uploads/<id>/complete` (with the job key) → status
`available`, logs the would-be `pelican object get …`, and the entry is now
queryable. Browser: **https://<ip>.sslip.io/docs** (Swagger UI — explore/admin
with the keys).

## 6. OSPool usage

Jobs hit `https://<ip>.sslip.io/v1/query` and `/v1/uploads/*` with header
`X-API-Key: <job key>`. The cert is normally trusted (Let's Encrypt) so no
`--insecure` / custom CA on execute nodes.

## 7. Going live: real pelican token issuance + OSDF verify

Simulation → production is a config flip PLUS getting OSDF **credentials** into
the api container. The **pelican CLI itself is baked into the image**: the
Dockerfile's `pelican` stage downloads the pinned static release binary
(`PELICAN_VERSION`, set as a build arg in `docker-compose.api.yml`) to
`/usr/local/bin/pelican`. Bump the version there and rebuild to upgrade — and
never bind-mount a host pelican over that path, it would shadow the pinned
binary. Order:

**a. Mount only the creds into the api container** (adjust host paths in
`docker-compose.api.yml`):

```yaml
    volumes:
      - /home/exouser/.config/pelican:/root/.config/pelican:ro   # OSDF creds
      - /home/exouser/private-key.pem:/keys/community.pem:ro     # issuer key
```

**b. Write the namespace registry and flip `.env.api`:**

```bash
cp namespaces.yaml.example namespaces.yaml && chmod 600 namespaces.yaml
# edit: issuer URL, private_key: /keys/community.pem, capacity_tb
```
(mounted by `docker-compose.api.yml` at `/etc/af3cache/namespaces.yaml`; the
api will not start in pelican mode without a valid one)

```
TOKEN_ISSUER_IMPL=pelican
OSDF_CLIENT_IMPL=pelican
PELICAN_TOKEN_BINARY=/usr/local/bin/pelican
OSDF_PELICAN_BINARY=/usr/local/bin/pelican
PELICAN_NAMESPACES_FILE=/etc/af3cache/namespaces.yaml
```

**c. Recreate, then PROVE pelican works in-container before trusting it:**

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml up -d --build --force-recreate api
docker exec af3cache-api-1 pelican --version                     # the baked-in version
docker exec af3cache-api-1 pelican object ls osdf:///af3-msa-library/    # read creds OK?
```

**d. Bulk ingest MUST use `--skip-verify`.** With `OSDF_CLIENT_IMPL=pelican` the
two-phase EBI/ospool pipeline POSTs before phase 2 uploads, so a fetch-to-verify
would fail. Stop the running ingest and relaunch with the flag (publishes
trusting declared sha/size; *job* uploads still get real verify):

```bash
pkill -f ebi_msa_ingest.py; pkill -f ospool_msa_ingest.py
nohup python3 /opt/af3-cache/scripts/ebi_msa_ingest.py … --skip-verify >> … &
```

**e. The reaper now actually deletes.** `OSDF_CLIENT_IMPL=pelican` makes the
maintenance reaper run real `pelican object rm` on stale objects — confirm the
mounted creds can delete before enabling the maintenance cron.

Rollback: set both impls back to `dryrun`, `up -d --force-recreate api`.

## 8. Going to a real domain (trusted TLS)

Moving off `<ip>.sslip.io` + `tls internal` (self-signed) onto a domain you
control gets a publicly-trusted Let's Encrypt cert — which is what lets OSPool
execute nodes talk to the API **without** `-k`/custom-CA distribution. The stack
is built for it: **no Caddyfile or code change**, just a DNS record + two
`.env.api` values + updating clients.

**a. Get a domain / DNS control.** Either a subdomain under a zone you already
own (e.g. ask CHTC/campus IT for `af3-cache.chtc.wisc.edu`), or register one
(Cloudflare/Namecheap, self-service in minutes).

**b. Point it at the VM.** Create an **A record** `<fqdn>` → `<api-host-ip>`
(the floating IP). **Keep that floating IP allocated** — releasing it breaks the
record. Verify before flipping anything:
```bash
dig +short <fqdn>        # must return <api-host-ip>
```

**c. Confirm the firewall** (already open from §2): Let's Encrypt HTTP-01 needs
:80 reachable from the internet, HTTPS needs :443. Both open to `0.0.0.0/0`.

**d. Flip Caddy — two env values, nothing else.** In `.env.api` (VM-local):
```
PUBLIC_HOST=af3-cache.chtc.wisc.edu     # your FQDN
CADDY_TLS=tls you@wisc.edu               # ACME contact (expiry/issue notices); empty also works
```
The Caddyfile already uses `{$PUBLIC_HOST}`/`{$CADDY_TLS}`, so just recreate
Caddy — it auto-obtains the cert on startup (persisted in the `caddy_data`
volume; auto-renews as long as :80 stays reachable):
```bash
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml up -d --force-recreate caddy
```

**e. Verify issuance:**
```bash
docker compose -f docker-compose.prod.yml -f docker-compose.api.yml logs caddy \
  | grep -i 'certificate obtained'
curl -s https://<fqdn>/healthz           # NO -k needed anymore
```

**f. Update everything pointing at the old URL, dropping `-k`/`--insecure`:**
- ingest scripts (`ebi_msa_ingest.py` / `ospool_msa_ingest.py`): `--base-url
  https://<fqdn>`, remove `--insecure`
- the AF3 job client's API base URL → `https://<fqdn>`
- docs (top-level README "Deployment", this file's examples)
- unaffected: token scopes / `osdf://` object URIs (not the API hostname)

**Sequencing & gotchas:**
- **DNS first, then flip.** Caddy needs `<fqdn>` resolving to the VM (and :80
  reachable) at first issuance, or ACME fails. Confirm with `dig` before step d.
- **Rate limits go away.** `tls internal` existed only because sslip.io is a
  shared domain Let's Encrypt rate-limits; a domain you control isn't.
- Optional once stable: HSTS / security headers in the Caddyfile; a low A-record
  TTL during cutover so you can re-point quickly.
- Rollback: set `PUBLIC_HOST` back to `<ip>.sslip.io` and `CADDY_TLS=tls
  internal`, recreate caddy.

## Notes

- **Simulation** (default): tokens are `SIMULATED-TOKEN.<jti>` and uploads are
  published unverified (declared sha/size trusted). Section 7 is the real
  cutover. Simplest alternative to bind-mounting pelican is installing it in the
  Dockerfile.
- **Persistence**: all services `restart: unless-stopped` + Docker enabled on
  boot; LE certs persist in the `caddy_data` volume.
- **Rotate keys**: add a second value to `API_KEYS` (comma-separated),
  `up -d api`, hand out the new one, later drop the old.
- **Logs**: `docker compose -f docker-compose.prod.yml -f docker-compose.api.yml
  logs -f api`.
