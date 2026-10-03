# Project Brain

Project Brain is an AI-native engineering system designed to serve as a central layer of memory, navigation, impact analysis, and context preparation for other AI agents (such as Claude Code, Codex, and Antigravity) working on large codebases.

Current release: **0.9.0** (`tournament-sandbox`).

Instead of replacing execution tools, Project Brain operates as a reasoning and indexing layer above the codebase, ensuring agents have a stable map of the project, understand cross-component dependencies, and maintain historical context.

---

## 1. What Project Brain Resolves

* **Navigation Degradation**: Prevents agents from getting lost in large codebases.
* **Impact Blindness**: Reduces cases where changes in one surface (e.g., backend API) silently break others (e.g., Android app, web portal).
* **Context Bloat**: Prepares task-specific, highly relevant context packages rather than dumping large amounts of unneeded files into the prompt.
* **Architectural Drift**: Enforces rules and keeps track of past architecture and product decisions.
* **Passive Memory**: Generates evidence-bound proactive insights instead of acting like a static knowledge base.

---

## 2. What Project Brain is NOT

* It is **not** a replacement for Claude Code, Codex, or Antigravity (they remain the "hands" that write code).
* It is **not** a replacement for Git, CI, or IDEs.
* It is **not** a general-purpose chat interface or a personal note-taking wiki.
* It is **not** a monolithic developer platform.

---

## 3. Quickstart (local, single user)

Runs on one machine with your own repositories. **No API keys are needed** to
try it: the shipped `.env.example` uses mock LLM and embedding providers.
The step-by-step guide with measured timings and troubleshooting is
[`docs/release/0.9.0-rc1/INSTALL.md`](docs/release/0.9.0-rc1/INSTALL.md).

### What runs where

| Component | How it runs | Port |
|---|---|---|
| Postgres 15 + pgvector | Docker (`docker-compose.yml`) | 5433 |
| Redis 7 | Docker | 6379 |
| Neo4j 5 Community | Docker | 7474 / 7687 |
| API (FastAPI) | your venv, or the `api` container | 8000 |
| Worker (background jobs) | your venv, or the `worker` container | — |
| MCP server (stdio) | started by your AI client | — |
| Web UI (Next.js, optional) | `apps/web`, Node 20+ | 3100 |
| n8n (optional scheduling) | Docker | 5678 |

### Prerequisites

* Python 3.10+
* Docker with Docker Compose v2
* ~2 GB free RAM for the services
* Optional: Node.js 20+ for the web UI; `postgresql-client` (pg15) for backups

### Steps

```bash
# 1. Get the code and install the package (registers the `brain` command)
git clone https://github.com/Perlitten/project-brain-public.git project-brain
cd project-brain
python3 -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env                 # Windows: copy .env.example .env
# Set TARGET_REPO_PATH to the repository you want indexed.

# 3. Start the storage services
docker compose up -d postgres redis neo4j

# 4. Preflight: python, .env, directories, provider keys, service health
brain doctor

# 5. Index a repository
brain index --repo /path/to/your/repo

# 6. Run the API (and, in a second terminal, the worker for background jobs)
uvicorn apps.api.main:app --port 8000
python -m brain.workers.worker
```

Check it:

```bash
curl http://127.0.0.1:8000/health
curl -H "X-API-Key: change-me-in-production" -H "Content-Type: application/json" \
     -d '{"repo_path": "/path/to/your/repo", "task_description": "add oauth login"}' \
     http://127.0.0.1:8000/context
```

API auth is on by default: every call needs `X-API-Key` equal to
`PROJECT_BRAIN_API_KEY` in `.env`. Change that value before you expose the port
anywhere.

**Everything in Docker instead.** `docker compose up -d --build` also builds and
starts the `api`, `worker` and `n8n` containers. The containers see only the
Project Brain checkout (`/app`); to index another repository from inside them,
mount it into the `api` and `worker` services first.

### Real models

Pick providers in `.env`, set the matching key, then re-index:

| Provider | `.env` |
|---|---|
| Mock (default, offline) | `DEFAULT_LLM_PROVIDER=mock`, `DEFAULT_EMBEDDING_PROVIDER=mock` |
| NVIDIA NIM (production default) | `DEFAULT_LLM_PROVIDER=nvidia`, `DEFAULT_EMBEDDING_PROVIDER=nvidia`, `NVIDIA_API_KEY=nvapi-…` |
| OpenAI / Anthropic / Google | `DEFAULT_LLM_PROVIDER=openai` (or `anthropic`, `google`) and the matching `*_API_KEY` |

Embeddings from different providers are not comparable. After switching the
embedding provider run `brain embeddings verify` and, if it reports
incompatible vectors, `brain embeddings backfill`.

#### Any OpenAI-compatible provider

LLM, summarizer and embedding calls go through one generic OpenAI-compatible
client (`brain/llm/providers/openai_compatible.py`), so any API that speaks
`/v1/chat/completions` and `/v1/embeddings` works: OpenAI, OpenRouter, Groq,
Together, DeepSeek, Mistral, Fireworks, NVIDIA NIM, Ollama, LM Studio, vLLM.
`mock`, `anthropic` and `google` keep their native adapters.

`DEFAULT_LLM_PROVIDER` / `DEFAULT_EMBEDDING_PROVIDER` take a preset name
(`openai`, `nvidia`, `openrouter`, `groq`, `together`, `deepseek`, `mistral`,
`ollama`, `lmstudio`) or `openai_compatible` (aliases `custom`,
`openai-compatible`). Presets only supply defaults (`brain/llm/presets.py`);
these universal settings override them for the selected provider:

| Variable | Meaning |
|---|---|
| `LLM_BASE_URL` | API root incl. version path, e.g. `https://openrouter.ai/api/v1` |
| `LLM_API_KEY` | Bearer key; falls back to the preset key (`GROQ_API_KEY`, ...). Optional for local endpoints |
| `LLM_MODEL` / `SUMMARIZER_MODEL` | main (synthesis) model / cheaper model for summaries & classification |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | default to `LLM_*` when both roles use the same provider |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | dimension is required for non-preset models and must match the pgvector column |
| `EMBEDDING_MAX_INPUT_CHARS` / `EMBEDDING_BATCH_SIZE` | per-input char cap / inputs per request |

`LLM_TASK_*_MODEL` still override per-task routing. Examples:

```bash
# OpenRouter (chat); embeddings stay on another provider
DEFAULT_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
LLM_MODEL=anthropic/claude-3.5-sonnet

# Groq
DEFAULT_LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
SUMMARIZER_MODEL=llama-3.1-8b-instant

# Ollama, fully local, no key (768-dim nomic-embed-text)
DEFAULT_LLM_PROVIDER=ollama
DEFAULT_EMBEDDING_PROVIDER=ollama
LLM_MODEL=llama3.1
```

Changing the embedding provider/model/dimension on an existing database
requires a full re-index; `brain doctor` fails early when the configured
dimension is unknown or contradicts the preset, and the embedding client
rejects vectors of the wrong width.

### Tests

```bash
pytest tests/ -q      # hermetic: ignores provider keys in your .env
ruff check .
```

---

## 4. Web UI

The API serves JSON only. The human interface is a separate Next.js app in
[`apps/web/`](apps/web/) that reads the API **server-side**, so the API key never
reaches the browser. Without an API configured it renders demo data.

```bash
cd apps/web
npm ci
cp .env.example .env.local     # BRAIN_API_URL=http://127.0.0.1:8000, BRAIN_API_KEY=<your key>
npm run dev                    # http://localhost:3100
```

| Variable | Purpose |
|---|---|
| `BRAIN_API_URL` | Base URL of the Brain API |
| `BRAIN_API_KEY` | Sent as `X-API-Key` from the server only |
| `WEB_BASIC_AUTH` | `user:password` gate; required when live API credentials are configured |
| `BRAIN_TIMEZONE` | Optional IANA time zone for displayed times (default UTC) |

**Deploy to Vercel:** import the repository, set *Root Directory* to `apps/web`
(framework preset Next.js), add the variables above, deploy. Then set
`BRAIN_WEB_URL` in the API's `.env` to the UI address: `GET /` advertises it and
old `/dashboard` links redirect there. Any Node host works the same way with
`npm run build && npm run start`.

---

## 5. CLI Usage

Once the package is installed in editable mode, the `brain` command-line executable is registered and available.

Version check without CLI dependencies:

```bash
python -m brain.version
```

### Health Checks
To verify connection health across all local or remote database dependencies (PostgreSQL, Redis, Neo4j):
```bash
brain health
```

### Auditing a Repository
To scan a target repository structure and compile the initial read-only codebase audit:
```bash
brain audit --repo <path_to_repository>
```
* **Default behavior**: If `--repo` is omitted, it uses `REPO_ROOT` from `.env` (defaults to the current project directory).
* **Output**: The scan generates a structured report at `reports/initial-audit.md` relative to the Project Brain root.

---

## 6. Memory Semantics

Explicitly historical Markdown remains searchable but is tagged as historical
evidence and heavily demoted in ordinary current-state retrieval. A query that
explicitly asks for history or archived context restores its normal retrieval
weight. Decision memory is canonical by normalized title within an optional
`repo_path`: recording the same title in the same scope updates that decision
instead of creating another active instruction. Context packs include global
decisions plus decisions owned by the requested repository, so one project's
normative memory cannot leak into another project's task.
Rules and decisions are read fresh for each context pack; only repository files
and symbols use the process cache, so a write or revocation takes effect on the
next delegated task without an API restart.
Time-bound audit snapshots, archived design sources, and repository Task
Contracts receive the same non-current authority treatment automatically; the
live harness injects its exact active contract separately.

**Brain Insights** is a proactive inbox backed by the worker queue and the `insights` database table, shown in the web UI. Manual scans enqueue `proactive_insights`; scheduled scans can be imported through n8n.

---

## 7. Eval & Adversarial Verification

```bash
# Golden tasks against your target repository.
brain eval --repo <path-to-target-repo> --golden rules/golden_tasks.yaml

# Validate a golden task YAML file.
brain eval --repo <path-to-target-repo> --golden rules/golden_tasks.yaml --validate
```

### Embedding integrity

```bash
brain embeddings status
brain embeddings verify --json
brain embeddings backfill              # sync pgvector + regenerate incompatible
brain embeddings backfill --pgvector-only  # JSON -> pgvector only
```

### Proactive insights

```bash
# Queue a deterministic insight scan through the API.
curl -X POST http://localhost:8000/jobs/proactive-insights \
  -H "X-API-Key: $PROJECT_BRAIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'

# List stored insights.
curl http://localhost:8000/insights -H "X-API-Key: $PROJECT_BRAIN_API_KEY"
```

By default, insight scans use deterministic checks only. To let the low-latency
NVIDIA NIM route synthesize additional evidence-bound insights:

```text
DEFAULT_LLM_PROVIDER=nvidia
NVIDIA_API_KEY=<secret>
LLM_TASK_INSIGHT_MODEL=meta/llama-3.1-8b-instruct
PROACTIVE_INSIGHTS_LLM_ENABLED=true
```

The LLM stage is bounded by `PROACTIVE_INSIGHTS_MAX_SNAPSHOT_CHARS` and `PROACTIVE_INSIGHTS_TIMEOUT_S`. If the model fails or times out, deterministic insights still run and the failure is reported in the job result.

### External imports

```bash
# Grafify graph JSON -> Neo4j (uses GRAFIFY_OUTPUT_PATH or ../graphify-out/graph.json)
brain import grafify
brain import grafify --path ../graphify-out/graph.json

# Obsidian vault -> rules/decisions (requires OBSIDIAN_VAULT_PATH or --vault)
brain import obsidian --vault /path/to/vault
```

### Browser automation (optional)

Project Brain does **not** control Cursor's embedded Simple Browser or the OS desktop. For
optional local browser checks, a Playwright eval scaffold lives under `eval/browser_automation/`.

```bash
pip install -e ".[dev]"
playwright install chromium
python eval/browser_automation/smoke_test.py
```

Set `BROWSER_HEADLESS=false` in `.env` to watch the browser window. Platform limits (Computer
Use, Cursor UI, Puppeteer vs Playwright) are documented in [`docs/tooling-gaps.md`](docs/tooling-gaps.md).

---

## 8. Production Deployment

Use [`DEPLOYMENT.md`](DEPLOYMENT.md) for the full git-safe self-host guide. It
covers a fresh Ubuntu VPS, Docker Compose, nginx/TLS, n8n, repository mounts,
indexing, smoke tests, MCP client setup, backups, and safety rules. Day-2
operations (release layout, upgrades, rollback) are in
[`deploy/RUNBOOK.md`](deploy/RUNBOOK.md) and
[`docs/release/0.9.0-rc1/UPGRADING_AND_ROLLBACK.md`](docs/release/0.9.0-rc1/UPGRADING_AND_ROLLBACK.md).
The web UI deploys separately (section 4).

If an AI coding agent will do the deployment, start with
[`AGENT_DEPLOYMENT.md`](AGENT_DEPLOYMENT.md). It is stricter than the human
guide and includes command gates, failure playbooks, and a final report template.

The production compose file is [`docker-compose.prod.yml`](docker-compose.prod.yml).
It keeps Postgres, Redis, and Neo4j private to Docker, exposes the API and n8n on
loopback only, and expects nginx to terminate public HTTPS.

Minimal server flow:

```bash
sudo mkdir -p /opt/project-brain
sudo chown -R "$USER:$USER" /opt/project-brain
git clone https://github.com/Perlitten/project-brain-public.git /opt/project-brain
cd /opt/project-brain

cp deploy/.env.prod.example .env
chmod 600 .env
# Fill BRAIN_PUBLIC_HOST, N8N_HOST, N8N_PUBLIC_URL, NVIDIA_API_KEY (or your provider key),
# BRAIN_TARGET_REPO_DIR, and BRAIN_INDEXED_PROJECTS_DIR.

bash deploy/server_up.sh
```

Do not commit `.env`, `.secrets/`, `reports/`, or `context_packs/`. `.mcp.json` is
tracked but must stay secret-free: it carries `${VAR}` references, never a literal
`BRAIN_API_KEY`. `BRAIN_API_KEY` accepts either the shared `PROJECT_BRAIN_API_KEY`
or a scoped `pbk_` credential — prefer the scoped one: the remote MCP tools need
`core:write,jobs:read`, and a per-agent key is revocable without rotating the
shared secret (`scripts/mint_api_credential.py --name <agent> --scopes core:write,jobs:read`).

---

## 9. MCP Server

Project Brain ships with a Model Context Protocol (MCP) server exposing **10 tools**:

| Tool | Purpose |
|------|---------|
| `ask_project` | LLM Q&A with retrieved code context |
| `prepare_task_context` | Build a context pack for a task |
| `prepare_deep_context` | Queue a quality-first LFM context pack in the background |
| `get_background_job` | Poll an asynchronous Brain job and retrieve its result |
| `impact_analysis` | Analyze change impact and risks |
| `record_decision` | Create or update a canonical global or repo-scoped architectural decision |
| `record_rule` | Create or update a global or repo-scoped normative rule |
| `search_code` | Hybrid file/symbol/chunk search |
| `find_related_files` | Neo4j graph neighborhood lookup |
| `review_diff` | Review git diff for rule violations |

`search_code` and `ask_project` accept an optional `repo_path` argument. Use it when
multiple repositories are indexed, for example `/indexed/forex-bot`, so retrieval
does not mix files from unrelated projects. The HTTP `/search` and `/ask` endpoints
accept the same `repo_path` field. For the remote MCP server, `BRAIN_REPO` is used
as the default repo scope when `repo_path` is omitted.

`record_decision` also accepts optional `repo_path`. Omit it only for a genuinely
global control-plane decision. Repository task state and architecture should use
the same canonical path passed to `prepare_task_context`.

`record_rule` follows the same scope rule. Context packs, Q&A, and diff review
load only global rules plus rules matching the selected canonical repository.

### Connecting an AI client

**Local (stdio).** The client starts the server itself; it reads the install's
`.env` and talks to Postgres, Redis and Neo4j directly, so the storage services
from the quickstart must be running.

```bash
# Claude Code (Windows: .venv\Scripts\python.exe)
claude mcp add project-brain -- /path/to/project-brain/.venv/bin/python -m apps.mcp_server.server
```

Other clients (Claude Desktop, Cursor) take the same command in their JSON config:

```json
{
  "mcpServers": {
    "project-brain": {
      "command": "/path/to/project-brain/.venv/bin/python",
      "args": ["-m", "apps.mcp_server.server"],
      "cwd": "/path/to/project-brain"
    }
  }
}
```

**Remote (HTTP).** To use a deployed Brain from another machine, run
`apps.mcp_server.remote_server` with `BRAIN_API_URL`, `BRAIN_API_KEY` and
`BRAIN_REPO` set; no local datastores are needed. The tracked `.mcp.json` does
exactly this from environment variables, and `.mcp.json.example` is the
template for clients that keep their config elsewhere. Details:
[`DEPLOYMENT.md`](DEPLOYMENT.md), section 10.

### MCP stdio E2E verification

```bash
python eval/mcp_stdio_e2e.py
# writes reports/mcp-stdio-e2e.json
```

### Protocol rules for AI Agents (Antigravity / Claude Code / Codex)
Agents must respect these rules prior to modifying the main repository:
1. **Before writing code**: Invoke `prepare_task_context` to request the relevant files, tests, and architectural constraints.
2. **Read the Context Pack**: Analyze the generated context pack before initiating changes.
3. **Minimize footprint**: Do not modify files outside the context pack scope.
4. **After editing**: Invoke `review_diff` to run checks on generated diffs, checking for rule violations or untested files.

---

## License

[MIT](LICENSE) © 2026 Andrei Damashkevich.

Project Brain builds on third-party libraries, fonts, container images and
models that keep their own licenses — see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Product names mentioned in
this repository are trademarks of their owners; this is an independent
project, not affiliated with any of them.
