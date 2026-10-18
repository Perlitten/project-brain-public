# One-team cloud deployment

**Experimental preview, `main` only, not in v0.9.2; full storage only.** This
page packages the [team mode walkthrough](../TEAM.md) for one team on one
Linux host: the API and the dashboard behind HTTPS, optional bounded
execution, backups and upgrades. It has been validated with `docker compose
config` and in CI, not on a live host with real people yet.

The files live in `deploy/team/`:

| File | What it is |
|---|---|
| `docker-compose.team.yml` | Overlay on `deploy/docker-compose.prod.yml`: `caddy` (TLS on 80/443), `web` (dashboard), `worker-executor` (profile `executor`), and API/worker settings for HTTPS, backups and execution. |
| `Caddyfile` | Two sites: `BRAIN_WEB_DOMAIN` -> dashboard, `BRAIN_API_DOMAIN` -> API and remote MCP. Caddy obtains and renews certificates itself. See [The HTTPS front](#the-https-front). |
| `.env.team.example` | The settings this page adds, with placeholders only. |

```text
internet --443--> caddy --> web:3100 (dashboard)  --> api:8000
                        \-> api:8000 (API, /mcp)       |-> postgres, redis, neo4j (internal only)
                                                       '-> worker, or worker-executor (profile executor)
```

`caddy`, `web` and the API share a private `edge` network; the datastores stay
on the default network, which `caddy` and `web` never join.

## Before you start

- A Linux host with Docker Engine and the Compose plugin, nothing else
  listening on ports 80 and 443, and **no other Project Brain stack**: the
  project name stays `brain` and containers are named `brain-*`. One team per
  host.
- Two DNS names pointing at the host, for example `brain.example.com`
  (dashboard) and `api.brain.example.com` (API, agents, MCP). Caddy needs
  ports 80 and 443 reachable from the internet to get certificates.
- A clean checkout of the commit you deploy. `deploy/server_up.sh` refuses a
  dirty tree and, with `ENVIRONMENT=production`, a commit without a green CI
  run (see the [operator runbook](../../deploy/RUNBOOK.md)).
- Model provider credentials, as for any production Brain
  ([Deployment](DEPLOYMENT.md)).

## Install

1. Get the code and create the server configuration in the repository root:

   ```bash
   git clone https://github.com/Perlitten/project-brain-public.git /opt/brain
   cd /opt/brain
   cp deploy/.env.prod.example .env && chmod 600 .env
   ```

   Then merge `deploy/team/.env.team.example` into `.env`: add its keys and,
   where `.env` already has a key, edit that line instead of adding a second
   one (`server_up.sh` reads the first copy of a key, so a second copy can
   silently disagree with what the containers get). Fill in the domains,
   `BRAIN_ACME_EMAIL`, every blank secret (`openssl rand -hex 32`) and the
   provider keys. The stack runs with `ENVIRONMENT=production`, so the API
   refuses to start with the well-known default passwords and keys.

2. Create the host directories. The image runs as uid 999:

   ```bash
   mkdir -p reports context_packs indexed-projects backups executions
   sudo chown 999:999 reports context_packs executions
   ```

   Put the checkouts you want indexed under `indexed-projects/` (mounted
   read-only at `/indexed`). Set `BRAIN_ORIGIN_REMOTES` for any of them that
   has no `origin` remote, so origin polling can tell when the index is behind.

3. Enable the overlay and deploy. `server_up.sh` and `deploy/doctor.sh` pick up
   a `docker-compose.local.yml` in the repository root:

   ```bash
   cp deploy/team/docker-compose.team.yml docker-compose.local.yml
   bash deploy/server_up.sh
   ```

   This builds the API and dashboard images with the release identity, takes a
   pre-deploy database backup into `backups/`, starts everything and waits for
   the API to be ready. The first start can take a minute while Caddy gets the
   certificates.

4. Check it: `https://<BRAIN_WEB_DOMAIN>` shows the dashboard sign-in, and on
   the host `curl http://127.0.0.1:8010/health` (or `BRAIN_API_PORT`) answers
   with `"mode": "solo"`. Through Caddy `/health` and `/ready` answer only a
   team administrator's key (the server key while solo):
   `curl -H "X-API-Key: <key>" https://<BRAIN_API_DOMAIN>/health`. Without
   one they are 404.

Later commands in this page use the containers by name (`docker exec
brain-api ...`), which needs no compose arguments. Never stop or remove
containers by pattern on a shared host.

## The HTTPS front

`deploy/team/Caddyfile` is the only thing listening on the internet. What it
does, and why:

- **Client addresses.** Caddy replaces any `X-Forwarded-For` a visitor sends
  with the address it sees. The overlay pins `caddy` and `web` to fixed
  addresses on the `edge` network (`BRAIN_CADDY_IP`, `BRAIN_WEB_IP` in
  `BRAIN_EDGE_SUBNET`) and sets the API's `BRAIN_TRUSTED_PROXY_IPS` to exactly
  those two, so the invitation and single sign-on throttles count each visitor
  separately instead of everyone sharing caddy's address (where ten bad
  attempts by one person locked invitations for everybody for five minutes).
  The overlay sets `BRAIN_TRUSTED_PROXY_IPS` itself: a value in `.env` does
  not apply. If `172.31.250.0/24` is already used on the host, change all
  three `BRAIN_EDGE_*`/`BRAIN_*_IP` values together.
- **Health.** `/health` and `/ready` on the API name describe repositories,
  workers and versions. Caddy asks the API's admin-only
  `/api/status/diagnostics` first and answers 404 unless the request carries a
  team administrator's key. `server_up.sh` and the compose health checks call
  the API directly on the host or in its container and are unaffected.
- **Body size.** Requests over 25 MiB are refused with 413 on both names, the
  same limit as the API's `API_MAX_REQUEST_BODY_BYTES`. The API itself checks
  only a declared `Content-Length`; Caddy also stops chunked bodies.
- **Security headers.** The API sets its own (HSTS with `includeSubDomains`,
  `nosniff`, `X-Frame-Options`, referrer and permissions policies). Caddy adds
  the same set to answers that lack them, mainly the dashboard's, and never
  replaces a header the app chose (for example `no-referrer` on `/invite`).
- **Single sign-on and cookies.** The overlay sets `BRAIN_WEB_URL` to
  `https://<BRAIN_WEB_DOMAIN>`, so the callback the provider is told is https;
  the dashboard marks its cookies `Secure` because Caddy tells it the visitor
  used https.
- **Streaming.** Remote MCP answers are passed on unbuffered.
- **Unknown names.** Port 80 only redirects to https. A TLS handshake for a
  name Caddy has no certificate for fails; a known name with another `Host`
  header gets an empty answer and reaches neither the API nor the dashboard.

### Checking the HTTPS front

`scripts/team_proxy_check.py` builds the API and dashboard images from the
checkout and starts the real production compose file, this overlay and a
test-only override (`deploy/team-proxy-check/`) under the project name
`brain-proxycheck`: throwaway Postgres and Redis, no Neo4j, no published ports
or bind mounts, and the real Caddyfile with Caddy's local certificate
authority instead of ACME. Two outside clients and one container on the edge
network then check everything listed above and the stack is removed. It needs
Docker with Compose 2.24.4 or later and Python 3.10+ (standard library only):

```bash
python3 scripts/team_proxy_check.py              # about 3 minutes with a build
python3 scripts/team_proxy_check.py --no-build   # reuse the images from the last run
python3 scripts/team_proxy_check.py --keep       # leave the stack up to inspect it
```

Use `--build-network host` if image builds cannot reach the package mirrors
(some WSL engines). The **Team proxy** workflow runs the same script on pull
requests that touch the front, the overlay or the code it checks.

## First administrator and the team

Until the first member exists the Brain is a team of one and the dashboard
uses the server key. Create the team on the server, then make yourself its
first administrator, exactly as in the [walkthrough](../TEAM.md):

```bash
docker exec brain-api brain team init --name "My team"
docker exec brain-api brain team project add core
docker exec brain-api brain team repo attach --project core --repository /indexed/core
docker exec brain-api brain team invite create --admin --name owner --project core
```

The `pbi_` token is printed once. Paste it on `https://<BRAIN_WEB_DOMAIN>/invite`
(or run `docker exec -it brain-api brain team invite accept`): you get your
`pbk_` member key once and the Brain switches to team state. From then on the
shared key is refused for project data and everyone uses a named key.

## Invite members

As an administrator, open **Team** in the dashboard and create an invitation,
or use `POST /team/invitations`. Send the `https://<BRAIN_WEB_DOMAIN>/invite#token=...`
link over a private channel. Details and guard rails:
[Inviting team members](TEAM_INVITATIONS.md).

## Single sign-on

Register `https://<BRAIN_WEB_DOMAIN>/auth/oidc/callback` as the redirect URI
at your identity provider, then set in `.env`:

```bash
BRAIN_OIDC_ENABLED=true
BRAIN_OIDC_ISSUER=https://idp.example.com/realms/acme
BRAIN_OIDC_CLIENT_ID=project-brain
BRAIN_OIDC_CLIENT_SECRET=...            # from the identity provider
BRAIN_OIDC_ALLOWED_EMAIL_DOMAINS=example.com
```

The overlay sets `BRAIN_WEB_URL` from `BRAIN_WEB_DOMAIN`, so the redirect URI
defaults correctly. Redeploy (`bash deploy/server_up.sh`). The rules for
binding members to identity provider accounts are in
[Team single sign-on](TEAM_SSO.md).

## Bounded execution (optional)

Read [bounded team execution](TEAM_EXECUTION.md) first: the subprocess backend
is not a security boundary. To run executions:

1. In `.env`: `BRAIN_EXECUTION_ENABLED=true` (the API needs it to accept
   requests), `BRAIN_EXECUTION_TOOLS` and, if used, `BRAIN_EXECUTION_SECRETS`.
   Set `BRAIN_PLAIN_WORKER_REPLICAS=0`: the executor replaces the plain worker,
   and two workers with different execution settings would race for the same
   jobs.
2. Add `COMPOSE_PROFILES=executor` to `.env`, so every compose command, the
   deploy included, starts `brain-worker-executor`.
3. Redeploy. Working copies land in `executions/` on the host.

The plain worker always runs with execution off. Tools must exist in the
`brain-api` image; `brain team doctor` reports a tool it cannot resolve.

## Diagnostics: `brain team doctor`

```bash
docker exec brain-api brain team doctor          # add --json for automation
```

Read-only. Each check prints `PASS`, `WARN`, `FAIL` or `UNKNOWN` with a
one-line fix, and the command exits 1 when any check fails:

| Check | Looks at |
|---|---|
| `team_state` | Solo or team, and why. |
| `oidc` | `BRAIN_OIDC_*` consistency, redirect URI vs. `BRAIN_WEB_URL`. |
| `shared_key` | Whether the shared server key is accepted (solo) or refused (team). |
| `administrators` | At least one administrator with a live key carrying `team:admin`. |
| `member_credentials` | Members with no live key (and no SSO identity when SSO is on). |
| `revocations` | Revoked members that still have live keys or open runs. |
| `restore_fence` | An open restore fence (see Restore below). |
| `invitations` | Expired invitations still pending. |
| `execution_leases` | Executions whose lease expired, or requested and never picked up. |
| `execution_config` | `BRAIN_EXECUTION_*` on this process: backend, environments, egress proxy image, root exists, docker CLI or host tools resolve, secrets set. The subprocess backend is a warning (no isolation). |
| `origin_settings`, `source_freshness:<repo>` | `BRAIN_ORIGIN_REMOTES`, and each repository's indexed revision against the polled origin. |
| `backup` | The newest backup under `BRAIN_BACKUP_DIR` and its age (`BRAIN_BACKUP_MAX_AGE_HOURS`, default 48); `unknown` when no location is configured. |

The same report is at `GET /team/doctor` for team administrators (any
authenticated caller while the Brain is solo). It runs on the API container, so `execution_config`
describes the API's settings; run the CLI in `brain-worker-executor` to check
the executor's. While a restore fence is open the API refuses every credential;
the CLI still works.

## Backup

`server_up.sh` dumps PostgreSQL to `backups/<UTC stamp>/brain_db.sql.gz` before
every deploy and keeps the last five. Schedule the same dump daily (root's
crontab, repository root `/opt/brain`):

```cron
0 3 * * * cd /opt/brain && d=backups/$(date -u +\%Y\%m\%dT\%H\%M\%SZ) && mkdir -p "$d" && docker exec brain-postgres pg_dump -U postgres brain_db | gzip > "$d/brain_db.sql.gz"
```

Use your `POSTGRES_USER` and `POSTGRES_DB` if you changed them. Deploys prune
`backups/` to the five newest entries, so copy backups off the host as well.
The overlay mounts `backups/` read-only into the API as `/backups`
(`BRAIN_BACKUP_DIR`), and `brain team doctor` fails on a dump too small to be
real, which is what a failed `pg_dump` leaves behind. Neo4j is not backed up
this way: the graph is rebuilt from the index.

## Restore

A snapshot cannot know about revocations made after it was taken, so a team
snapshot must be restored **behind the access fence**, in the same transaction
as the data. Background: [restoring a team Brain](../runbooks/backup-restore.md#restoring-a-team-brain).

```bash
POSTGRES_USER=postgres POSTGRES_DB=brain_db           # yours, if you changed them
docker stop brain-web brain-worker brain-api        # plus brain-worker-executor if used
docker exec brain-postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$POSTGRES_DB' AND pid<>pg_backend_pid()" \
  -c "DROP DATABASE \"$POSTGRES_DB\"" -c "CREATE DATABASE \"$POSTGRES_DB\""
{ gunzip -c backups/<stamp>/brain_db.sql.gz; cat scripts/team_restore_fence.sql; } \
  | docker exec -i brain-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1 \
      --single-transaction --set fence_reason="restored from backups/<stamp>" -f -
docker start brain-api
```

The API comes back fenced: every credential is refused and
`brain team doctor` fails `restore_fence`. Review and reopen access with the
break-glass CLI, then replay what changed after the snapshot:

```bash
docker exec brain-api brain team reconcile                  # prints the state and a digest
docker exec brain-api brain team reconcile --attest <digest>
```

Then give members new keys as the runbook describes, start the remaining
containers (`docker start brain-worker brain-web`), and reindex so the graph
matches the restored database.

## Upgrade

```bash
cd /opt/brain
git fetch origin && git checkout <commit>     # a commit with a green CI run
diff deploy/team/docker-compose.team.yml docker-compose.local.yml
bash deploy/server_up.sh
docker exec brain-api brain team doctor
```

If the overlay changed, copy it to `docker-compose.local.yml` again (keep your
own additions). `server_up.sh` takes the pre-deploy backup; the API applies
forward-only schema migrations on start. Rolling back across a migration means
restoring that backup together with the old code
([runbook](../../deploy/RUNBOOK.md)); for a team Brain use the fenced restore
above.

### Workspace registry: file to table

Repository trust (the workspace registry) used to live in a file,
`.brain/workspace/repository_registry.json`, in each process's working directory
(`/app` in the containers, with no volume), so the worker re-authorized jobs
against its own, usually empty, copy. In team state the registry now lives in
PostgreSQL (`workspace_registry_entries` and `workspace_registry_events`, created
on API start; no new environment variables). Solo state still uses the file and
does not change.

- **Import.** The first time the API runs in team state (at startup, or on the
  first registry read after the team gains its first member), it copies its own
  file, entries and events, into the empty table and logs
  `Imported N workspace registry entries ... into workspace_registry_entries`.
  The import runs once: as soon as the table has an entry, the files are no longer
  read in team state (edits to them are ignored). Files in the worker container
  are never imported.
- **Until then.** While the table is empty, trust checks read the local file and
  log `Workspace registry table is empty; reading the legacy registry file ...`.
  An unreadable file still fails closed.
- **Keep the API's file across the upgrade.** The file is in the container's
  writable layer, so recreating `brain-api` loses it. If your old API container
  has one, copy it out before upgrading and back in afterwards, then restart the
  API and look for the import line:

  ```bash
  docker cp brain-api:/app/.brain/workspace ./workspace-registry-backup   # before server_up.sh
  bash deploy/server_up.sh
  docker exec brain-api mkdir -p /app/.brain
  docker cp ./workspace-registry-backup brain-api:/app/.brain/workspace
  docker exec -u 0 brain-api chown -R "$(docker exec brain-api id -u)" /app/.brain   # docker cp writes as root
  docker restart brain-api && docker logs brain-api 2>&1 | grep "workspace registry"
  ```

  No file, or an empty one, needs nothing: register repositories again through
  the API or with `brain workspace repositories register --project <project>`.
- **After the import** the file stays on disk but is not read in team state. If the
  team is dissolved back to solo, Brain reads the file again, which may be older
  than the table.
- **Offline CLIs.** `brain workspace` and the offline tools that read the registry
  (`python -m brain.lab.cli`, `brain.freshness.cli`, `brain.experiments.cli`,
  `brain.portfolio.cli`, `brain.control.cli`) check the team state on each run
  (`brain.team.cli_registry`). In team state they read the shared table (and do
  the one-time import of their own file while it is empty); in solo state, or with
  Lite storage, they read the local file as before. `GET /control/*` reads the
  same registry as the other API routes.
- **Database down.** When the CLI cannot reach the team database it falls back to
  the local file with a stderr warning (`solo/offline fallback`), so a solo Brain
  without PostgreSQL keeps working. A team-state run leaves a marker,
  `<registry dir>/team_state.json` (the API writes one in its own working directory
  at startup); while that marker exists, or `BRAIN_TEAM_MODE` is on, an
  unreachable database is an error (exit code 2) instead, because the file may be
  stale. Start the database, or delete the marker only if the Brain is back to solo
  use. A solo run with a reachable database removes the marker.

## Limits

- One team per host and one host per team; no high availability.
- The `executor` profile uses the subprocess backend: no container isolation
  and no egress control.
- The dashboard keeps member sessions sealed with `PROJECT_BRAIN_API_KEY`;
  rotating it signs everyone out.
- Caddy does no rate limiting of its own: only the API's invitation and single
  sign-on throttles, per visitor address, limit repeated attempts.
- Not piloted: no real team has run this package yet.
