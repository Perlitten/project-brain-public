# FAQ

Latest published release: **[v0.9.2](https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.2)**.
Use the [current getting-started guide](GETTING_STARTED.md) for installation.
The dashboard repeats these practical answers on its [Help screen](DASHBOARD.md).

**Scope:** the recommended tagged install is v0.9.2. Features explicitly labelled
unreleased `main` below are not in that release, even while source metadata still
says 0.9.2. See [main-only setup](GETTING_STARTED.md#lite-mode-unreleased-main)
and the [Unreleased changelog](../CHANGELOG.md#unreleased).

- [Using Brain](#using-brain) — problems people run into first, and how to fix them.
- [Limits and evidence](#limits-and-evidence) — what Brain does and does not establish.

## Using Brain

### My agent isn't using Brain

Connecting Brain makes its MCP tools available; it does not make an agent call
them. Check that the client lists Brain's tools, then add the
[suggested agent workflow](MCP.md#suggested-agent-workflow) to the agent's
instructions: call `prepare_task_context` before planning a change and
`review_diff` before finishing. Pass the absolute indexed checkout as `repo_path`.

### Brain answers about the wrong repository

Brain scopes answers to one indexed checkout. Align `TARGET_REPO_PATH` in `.env`,
the CLI `--repo` option and the `repo_path` your agent sends; they must name the
same absolute path.

### Brain says it is behind the code

Brain answers from the last commit it indexed. Re-index from the dashboard (or
run `.venv/bin/brain index --repo /absolute/path` from the Brain checkout);
later runs re-read only what changed. Brain indexes committed source only: if a re-index refuses the tree,
commit your source edits first. Ignored build output such as `node_modules` and
`.next` does not count, so do not delete it to make re-indexing work.

On unreleased `main`, `BRAIN_STALE_CONTEXT_POLICY=warn_and_serve` is the default:
runtime context reports a freshness warning, schedules a throttled reindex and
uses bounded current source slices for changed candidates. `strict_block` restores
blocking. This policy is absent from v0.9.2; its v2 runtime context blocks stale
indexes. See [moving checkouts](GETTING_STARTED.md#moving-checkouts-unreleased-main).

### A job is queued and never finishes

Jobs run in the background worker, not in the API. Check worker health on the
dashboard's Setup screen or in `curl http://127.0.0.1:8000/health`, and read the
`start.sh` terminal. `./scripts/start.sh` runs the API, the worker and the
dashboard together.

Unreleased `main` Lite mode is different: `brain serve --lite` owns its local
maintenance worker; stdio-only `brain mcp` does not start one. Lite defaults to
token-hash local embeddings and a disabled LLM, not the full profile's mocks.

### Search results look random

The shipped defaults use `mock` models. They test the plumbing without provider
keys, but their semantic rankings and generated answers are not meaningful.
Configure real embedding and language models (see
[Getting started](GETTING_STARTED.md#configure-meaningful-models)), restart the
API and worker, then run `.venv/bin/brain embeddings verify --repo /absolute/path`
and `.venv/bin/brain embeddings backfill --repo /absolute/path`. Verify again;
an incremental re-index alone leaves unchanged files on their old vectors.

### The dashboard shows example numbers

The dashboard is not connected to a Brain server, so it shows demo data. Start
Brain with `./scripts/start.sh`, which gives the dashboard its server address and
credential. A web app started on its own stays a demo.

### How do I update Brain?

Back up `.env` and your data first. A release checkout is normally on a detached
tag, so update it with `git fetch --tags origin` and `git checkout vX.Y.Z`, using
the new release tag. Re-run `./scripts/bootstrap.sh`, refresh dashboard packages
with `(cd apps/web && npm ci)`, and restart with `./scripts/start.sh`; these steps
preserve `.env` and the database volumes. Restart MCP clients afterwards. On a
server, follow [Start or Update](../deploy/RUNBOOK.md#start-or-update) in the
runbook. Never run `docker compose down --volumes` on data you need to keep; see
the [backup and restore runbook](runbooks/backup-restore.md).

## Limits and evidence

### Can I cancel or permanently deduplicate background jobs?

Not generically. Full Redis supports internal cancellation only before a worker
dequeues the delivery; no generic `/jobs` cancel endpoint is exposed. Main Lite
does not expose queue cancellation. Full Redis idempotency claims expire after
7 days, whereas Lite keeps a completed-key row associated with the original ID.
Neither mode guarantees exactly-once external side effects. See the
[queue contract](operations/DEPLOYMENT.md#background-queue-cancellation-and-deduplication).

### How is this different from Obsidian / Mem0 / Letta / plain RAG?

- **vs Obsidian** — Brain is not a notes app: it indexes *code* (chunks +
  dependency graph), and its memory is written by agents, for agents, through
  MCP tools. You never open a vault; your agent queries it.
- **vs other memory systems** — Brain combines project-scoped memory with an
  indexed code graph and task context. Other systems also offer memory and
  scoping; no head-to-head performance advantage has been established. Explicit
  rules and decisions can be recorded directly; learned guidance follows the
  [L1–L4 evidence and approval policy](MEMORY_LESSON_PROTOCOL.md).
- **vs plain RAG** — retrieval is only one tool of thirteen. The graph,
  impact analysis, drift detection and recorded decisions answer questions a
  vector store cannot ("what breaks if I change X").

### What does it cost in tokens?

Costs depend on the selected models, indexing, retrieval, follow-up source reads
and maintenance. Payload limits differ between tools and rollout modes; the
compact v2 locator is opt-in on v0.9.2 (`BRAIN_AGENT_CONTEXT_V2_MODE=off`).
On unreleased `main`, `BRAIN_AGENT_CONTEXT_V2_MODE=on` and
`RETRIEVAL_V2_ENABLED=true` are defaults. Turn both off for legacy responses;
bounded runtime context is not a promise about every tool's payload size.
Main also supplies deterministic source summaries before optional asynchronous
LLM enrichment, so a successful initial index is not proof of generated summaries.
Brain adds no per-call fee, but provider and infrastructure costs remain yours.
Net coding token, time or cost savings have **not been established**.

### Does it work offline?

After downloading models and dependencies, configure **both** LLM and embedding
providers as local Ollama endpoints for local model execution. For example,
use `DEFAULT_LLM_PROVIDER=ollama`, `DEFAULT_EMBEDDING_PROVIDER=ollama`,
`LLM_MODEL=llama3.1` and `EMBEDDING_MODEL=nomic-embed-text`. Check that endpoint
overrides are local, disable external notifications/integrations, and use a local
agent connection. Mock providers also run offline, but their results are only a
smoke test. Changing embedding models requires compatibility checks/reindexing.

### Where does my data go?

The code index and memory are stored in the databases you operate. This does
**not** mean source code always stays on that machine:

- External embedding providers receive text chunks selected for embedding.
- External LLM providers receive prompts that can include retrieved source,
  decisions, rules, lessons and task descriptions.
- Connected agents receive tool results; their own model/service policies apply.
- Optional notifications, webhooks and remote services can transmit their payloads.

For local model execution, configure both provider roles and all endpoint
overrides to locally hosted models. Cloud-provider configurations are not a
local-only guarantee. Review provider retention policies before indexing private
code. Keep server-side credentials and `.env` files private; scoped keys and OAuth
control access but do not prevent an authorized recipient from receiving context.

### Do learned lessons always require approval?

No. L1 observations/lessons record reported evidence, not proof Brain executed
the validation. Eligible L3 clusters can promote automatically by default,
subject to evidence, severity and contradiction gates. Set
`MEMORY_CONSOLIDATION_REQUIRE_APPROVAL=true` for approval of new promotions;
duplicate reinforcement has separate behavior. Distilled L4 procedures default
to pending approval. Direct rules, decisions and curated procedures are distinct
write paths. See the [complete policy](MEMORY_LESSON_PROTOCOL.md).

### Do I need Neo4j?

Graph-dependent tools require Neo4j. Some search/API paths can operate without
it, but the recommended source setup includes it and checks its health. A
partially available service is not equivalent to a verified full installation.

Unreleased `main` also offers [Lite mode](GETTING_STARTED.md#lite-mode-unreleased-main)
with SQLite graph adjacency and a reduced local capability set. That is not a
claim of parity with the full Neo4j deployment or a published v0.9.2 feature.

### Is this production-ready?

It is early, self-hosted software run by a trusted operator, not a multi-tenant
service. Team mode on `main` is an [experimental preview](TEAM.md): not in
v0.9.2, not deployed and not piloted. The published release has installation, indexing and
MCP smoke coverage. Public exposure requires TLS and the scoped-key
discipline in [Deployment](operations/DEPLOYMENT.md). Production operation and useful coding outcomes
must be evaluated for your own workflow; passing tests do not establish them.
