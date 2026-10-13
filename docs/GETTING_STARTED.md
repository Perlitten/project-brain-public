# Getting started

This is the current source installation guide for a local, single-user instance.
It replaces release-specific instructions as the primary onboarding route.

Latest published release: **[v0.9.2](https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.2)**.
The commands pin that published version. Development on `main` can be newer;
use a reviewed commit if deliberately testing unreleased changes.

## Requirements and platform support

| Requirement | Current verification scope |
|---|---|
| Python 3.12 | Python CI and local end-to-end verification |
| Docker Engine with Compose 2.24+ | Linux service startup and source workflow |
| Node.js 22 and npm | Web CI; needed for the dashboard |
| Git, a POSIX shell, network for dependencies | Bootstrap and source checkout |

Linux is the verified installation platform. macOS and WSL2 can run the same
POSIX scripts but have not received equivalent end-to-end verification. Native
Windows installation is unverified; do not assume PowerShell can run these
scripts. Model downloads, larger repositories and graph workloads need additional
RAM and disk beyond a small smoke test. Check Docker resources before indexing.

The local defaults bind datastore ports and the app to loopback. Avoid exposing
this development setup through a tunnel or public reverse proxy; use the
[deployment guide](operations/DEPLOYMENT.md) for an authenticated server.

## Install and choose a repository

For a self-contained first run, index Brain's own source:

```bash
git clone --branch v0.9.2 https://github.com/Perlitten/project-brain-public.git project-brain
cd project-brain
./scripts/bootstrap.sh
.venv/bin/brain index --repo "$PWD"
./scripts/start.sh
```

Bootstrap creates a Python virtual environment, installs the package, creates
`.env` if it does not exist, generates local API/webhook secrets, starts storage
and runs `brain doctor`. It preserves an existing `.env`. Start installs web
dependencies if needed and runs the API, background worker and dashboard.

For your own repository instead, use an existing **absolute** directory:

```bash
./scripts/bootstrap.sh --repo /absolute/path/to/your/repository
.venv/bin/brain index --repo /absolute/path/to/your/repository
./scripts/start.sh
```

The bootstrap `--repo` option sets `TARGET_REPO_PATH`, the server's default
repository. Pass `--repo` explicitly to CLI indexing and use the same canonical
path in MCP calls. `REPO_ROOT` is only a CLI fallback, not a substitute for this
server configuration.

Git indexing reads tracked source and requires a clean committed tree. Commit
legitimate changes before indexing again. Ignored build outputs such as `.next`
and `node_modules` are excluded; do not delete them to make reindexing work.
Non-Git snapshots do not have Git revision provenance.

## Check the running instance

Open **http://127.0.0.1:3100/setup**. Confirm storage, API and worker health and
select the indexed repository. A fresh loopback-only dashboard can open without
a login; public hosts remain locked until authentication is configured.

```bash
.venv/bin/brain doctor
curl --fail http://127.0.0.1:8000/health
```

The worker must run for queued jobs and scheduled maintenance. API health
reports its heartbeat. A green installation check does not establish useful
semantic retrieval or production suitability.

### Moving checkouts (unreleased `main`)

Runtime context defaults to `BRAIN_STALE_CONTEXT_POLICY=warn_and_serve` on
`main`. An older index triggers a throttled background reindex and a
`freshness_warning` with a bounded changed-file list. Changed candidates use
current on-disk slices; code from deleted or escaping paths is not returned as
live evidence. Rules, decisions and eligible learnings remain fresh. Commit
source changes so the queued index can complete its clean-tree verification.

Use `BRAIN_STALE_CONTEXT_POLICY=strict_block` when a workflow requires an exact
indexed revision. The published v0.9.2 runtime retains its original behavior.

## Configure meaningful models

The shipped defaults use `mock` for both roles. Mocks require no provider keys
and test the plumbing; their semantic rankings and generated answers are not
meaningful. Identifier-based lexical retrieval still makes a useful first smoke
task, but benchmark conclusions require real models.

For local embedding inference, install Ollama separately and run:

```bash
ollama pull nomic-embed-text
```

Then set `DEFAULT_EMBEDDING_PROVIDER=ollama` in Brain's `.env` before indexing.
For fully local model inference, also pull a suitable chat model and configure
`DEFAULT_LLM_PROVIDER=ollama` and `LLM_MODEL`. Verify both endpoint overrides
are local. Cloud providers receive selected source and prompts; see
[provider configuration](MODEL_PROVIDERS.md) and [data boundaries](ARCHITECTURE.md#trust-and-data-boundaries).

Changing embedding model, provider or dimension on an existing database needs
compatibility verification and reindexing; vectors from different models are
not interchangeable. See `brain embeddings verify` and the model guide.
Restart API, worker and MCP clients after configuration changes.

## Lite mode (unreleased main)

Lite is available from the current source branch, not the published v0.9.2
artifacts. Install that checkout with `python -m pip install -e /absolute/path/project-brain`.
Then open a terminal in the repository you want to index:

```bash
brain init --lite
brain index
brain mcp
# Optional loopback API with background indexing/enrichment:
brain serve --lite --port 8000
```

The saved `.brain/config.json` selects SQLite relational/vector storage,
SQLite graph adjacency and a durable SQLite maintenance queue/TTL cache.
`brain init` creates a private API key and adds `.brain/` to Git's local
exclude file; it does not modify your committed ignore rules. Use an absolute
`BRAIN_LITE_DIR` to store state elsewhere. Explicit `BRAIN_STORAGE_MODE=full`
selects the existing full profile; Compose deployments retain full mode.

Configure an MCP client with the absolute `brain` executable, arguments `["mcp"]`,
and your repository as its working directory. No API or backing services are
needed for stdio search, task context, memory or bounded impact. `brain serve`
runs one local maintenance worker; without it, queued reindex jobs wait safely
until the API starts, or you can run `brain index` directly. Dirty Git source
must be committed before verified reindexing.

The default offline `local-token-hash-v1` embeddings are deterministic lexical
features, not a trained semantic model. No chat provider is enabled by this
profile. Configure providers explicitly before using model-generated answers
or optional enrichment; external providers can send code off-machine and incur
costs. Reindex into a separate store when changing embedding dimensions/models.

Lite performs exact float32 cosine scans over at most 50,000 scoped chunks.
SQLite serializes writers. It supports code indexing, scoped memory, locator,
runtime context, direct neighbors and bounded two-hop impact; arbitrary Cypher,
ANN indexes, advanced graph administration and deep/scheduled cloud operations
require full mode. Back up the entire `.brain` directory with all processes stopped;
the store contains project knowledge and source chunks. New tables initialize
idempotently; future incompatible Lite schemas need explicit migration support.

## Connect MCP and run one task

On unreleased `main`, initial indexing defaults to `INDEX_LLM_SUMMARIES_MODE=lazy`:
symbols, deterministic docstrings/signatures, graph edges and embeddings finish
without chat-model summaries. Embedding providers can still incur costs. Set
`inline` to retain foreground chat summaries, or `off` to disable enrichment.
Optional `INDEX_LLM_SUMMARIES_BACKGROUND=true` schedules bounded maintenance
enrichment after verified completion; it can call your configured chat provider.
Index readiness never waits for that job. No fixed indexing speed is promised.

Local stdio MCP connects directly to Brain's databases; those services must
remain running. The API is not required by this local transport.

```bash
claude mcp add project-brain -- /absolute/path/project-brain/.venv/bin/python -m apps.mcp_server.server
```

For another client, use that absolute Python path, arguments
`["-m", "apps.mcp_server.server"]`, and the Brain checkout as its working
directory. See [MCP client examples](MCP.md#client-configuration).

Ask your agent to call `search_code` with query `FileIndexer` and `repo_path`
set to the absolute indexed checkout. Open the returned file. Then call
`prepare_task_context` with the same repository and the task description:
"Explain how ignored generated files are excluded during reindexing."
Inspect the implementation, test references and retrieved constraints.

Success means the client actually invokes Brain, receives scoped source
references and can verify them. It does not prove faster or better coding.
MCP tools are available to your client; automatic use depends on your agent's
instructions and tool-selection behavior.

## Stop, restart and reinstall safely

Press Ctrl+C in the `start.sh` terminal to stop the API, worker and dashboard.
Storage stays running. Stop it with:

```bash
docker compose stop
```

Restart with `./scripts/start.sh`; it starts storage and app processes again.
Index another clean committed revision with the same `brain index --repo`
command, then inspect indexing status. For a CLI/MCP-only instance, use
`./scripts/start.sh --no-web`.

Re-running bootstrap is non-destructive: it preserves `.env` and datastore
volumes. For a clean code/dependency reinstall, back up `.env` and data first,
clone into a separate directory and restore the reviewed configuration.
Do not run `docker compose down --volumes` on data you need to retain.
Follow the [backup/restore runbook](runbooks/backup-restore.md) for data recovery.

## Troubleshooting and other installation paths

- **Doctor fails:** resolve the reported service, configuration or provider
  failure before indexing. Do not treat a partially successful bootstrap as done.
- **Queued job never completes:** inspect worker health and the `start.sh`
  terminal; the API alone cannot execute jobs.
- **Wrong repository:** align `TARGET_REPO_PATH`, CLI `--repo` and MCP `repo_path`.
- **Dashboard shows example data:** run `start.sh` so its server-side API URL
  and credential are configured. A standalone unbound web app is a demo.
- **Reindex refuses a Git tree:** commit real source edits; ignored generated
  artifacts should not trigger the dirty-source requirement.

Release wheels and source archives are on the release page with checksums and
an SBOM. A wheel provides Python runtime/CLI modules; it is not the full
repository's Compose, bootstrap and Next.js installation. A source archive
retains that source workflow; Git revision metadata is available only in a clone.
See [release verification](release/README.md) for the verification procedure.

Prebuilt GHCR images are experimental and not a supported newcomer route.
For remote servers, scoped credentials, TLS and operational controls, read
[Deployment](operations/DEPLOYMENT.md). Return to the [documentation index](README.md).
