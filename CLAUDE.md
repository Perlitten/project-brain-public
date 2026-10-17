# Project Brain — agent map

What the system does and how to run it locally is in [README.md](README.md). This file only
covers what saves steps: **where to go for a given task**, layer boundaries, and pitfalls.

Use the [current architecture guide](docs/ARCHITECTURE.md) to find the relevant runtime layers.
Historical research and replay tools are cataloged separately in [docs/historical](docs/historical/README.md).
Start with the files for the task rather than relying on a stale repository size estimate.

## Navigation spine: router ↔ package

`apps/api/routers/<X>.py` almost always maps to `brain/<X>/`. Find one side and you know the other.

| Router | Core | Tests |
|---|---|---|
| `routers/core.py` | `brain/search/`, `brain/database/` | `test_repo_scoped_search.py`, `test_vector_search.py` |
| `routers/web.py` (JSON for the web UI) | `brain/embeddings/integrity.py`, `apps/api/helpers.py` | `test_web_router.py` |
| `routers/graph_v2.py` | `brain/graph/` | `test_graph_and_memory.py`, `test_graph_v3_identity.py` |
| `routers/impact.py` | `brain/insights/impact_*.py`, `brain/analyzers/` | `test_impact_analysis.py`, `test_impact_bounded.py` |
| `routers/drift.py` | `brain/insights/drift_*.py` | `test_drift_analyzer_v3.py`, `test_architecture_change_guard.py` |
| `routers/coupling.py` | `brain/insights/coupling_*.py` | `test_coupling_intelligence.py` |
| `routers/remediation.py` | `brain/insights/remediation_*.py` | `test_remediation_planner.py` |
| `routers/freshness.py` | `brain/freshness/` | `test_freshness_incremental.py`, `test_repo_freshness.py` |
| `routers/ledger.py` | `brain/ledger/` | `test_evidence_ledger.py` |
| `routers/workspace.py` | `brain/workspace/` | `test_workspace_registry.py` |
| `routers/harness.py` | `brain/database/harness_models.py`, `brain/memory/harness_store.py` | `test_harness_api.py`, `test_harness_store.py`, `test_memory_observation_protocol.py` |
| `routers/core_memory.py` (episodes, learnings, skills) | `brain/memory/consolidation.py`, `skill_distiller.py`, `learning_retrieval.py`, `skill_store.py` | `test_consolidation_gates.py`, `test_memory_consolidation_db.py`, `test_skill_distiller.py`, `test_runtime_memory_projection.py` |
| `routers/jobs.py` | `brain/workers/` | `test_worker_queue.py`, `test_worker_capacity_readiness.py` |
| `routers/lab.py` | `brain/lab/` | `test_change_laboratory.py` |
| `routers/experiments.py` | `brain/experiments/` | `test_remediation_experiments.py` |
| `routers/portfolio.py` | `brain/portfolio/` | `test_portfolio_graph.py` |
| `routers/control.py` | `brain/control/` | `test_control_plane.py` |
| `routers/team.py` (team admin, tasks, runs), `routers/team_invitations.py`, `routers/team_oidc.py` (SSO; web half in `apps/web/lib/oidc.ts`); `apps/api/team_access.py` (repository authority for every personal router: solo = team of one) | `brain/team/` (`provisioning.py`, `invitations.py`, `oidc.py`, `oauth_link.py`, `authorization.py`, `store.py`) | `test_team_provisioning.py`, `test_team_invitations.py`, `test_team_oidc.py`, `test_team_oauth_link.py`, `test_team_transport.py`, `test_team_foundations.py` |

## Routes for common tasks

| Task | Start with |
|---|---|
| Web UI layout/branding | `apps/web/` (Next.js, deployed separately, e.g. on Vercel; the API serves JSON only) |
| New or changed endpoint | `apps/api/routers/<topic>.py` → `apps/api/schemas.py` → registration in `apps/api/main.py` |
| Health check, behaviour when the DB is down | `apps/api/main.py`, `brain/database/session.py`, `tests/test_health.py` |
| API response shape / DTOs | `apps/api/schemas.py`, `brain/database/models.py` |
| Code search, ranking | `brain/search/code_search.py`, `lexical_ranking.py`, `similarity.py` |
| Context pack assembly, budgets | `brain/context/context_pack_builder.py` (selection and storage), `rendering.py` (artifact contract), `budget.py`, `brain/search/code_search.py` |
| Retrieval pipeline, rerank | `brain/retrieval/pipeline.py` (I/O orchestration), `fusion.py` (in-memory scoring), `service.py`, `reranker.py` |
| Repository indexing | `brain/indexers/repo_indexer.py`, `file_indexer.py` (SQL/providers), `graph_enrichment.py` (buffered projection), `symbol_extractors.py` |
| Embeddings, pgvector | `brain/embeddings/store.py`, `pgvector_sql.py`, `integrity.py` |
| ColBERT / late interaction | `brain/late_interaction/provider.py`, `store.py`, `client.py` |
| Memory, decisions, rules | `brain/memory/decision_store.py`, `rule_store.py`, `relevance.py` |
| Team memory proposals, review queue, personal tier (draft isolation, CAS approval) | `brain/memory/proposals.py`, `apps/api/routers/memory_review.py`, `apps/web/app/review/`; `tests/test_memory_proposals.py`; `docs/MEMORY_REVIEW.md` |
| Proactive snapshot checks | `brain/insights/detectors.py` (pure checks), `candidates.py` (records); `proactive.py` orchestrates drift, models and persistence |
| Memory consolidation L1→L2→L3→L4 (gates G1–G4, evidence levels, supersede, skills) | `brain/memory/consolidation.py`, `skill_distiller.py`, `learning_store.py`; input — `routers/harness.py` (`/lessons`, `/observations`); protocol — `docs/MEMORY_LESSON_PROTOCOL.md` |
| L3 retrieval into context/ask (hybrid pgvector + lexical) | `brain/memory/learning_retrieval.py`, `brain/context/runtime_context_builder.py` |
| Repository scope and index freshness | `brain/memory/repo_scope.py`, `repo_freshness.py`, `source_manifest.py` |
| MCP tools | `apps/mcp_server/server.py` (local), `remote_server.py` (remote), `tests/test_mcp.py` |
| MCP protocol: annotations, resources, prompts, elicitation and sampling | `apps/mcp_server/protocol.py`, `tests/test_mcp_protocol.py` |
| Native Streamable HTTP with principal-bound sessions | `apps/api/mcp_http.py`, `tests/test_mcp_http.py` |
| OAuth 2.1: DCR, PKCE, consent, scoped credentials and refresh rotation | `apps/api/mcp_oauth.py`, `brain/auth/oauth_store.py`, `tests/test_mcp_oauth_db.py` |
| Team provisioning (team, projects, members, repos), OAuth clients as team members | `apps/cli/commands/team.py`, `brain/team/provisioning.py`, `brain/team/oauth_link.py`; ADR — `docs/adr/team-collaboration.md` |
| Who may read/write a repository (solo vs. team state, roles, `Named credential required`) | `brain/team/authorization.py` (`team_state`, `authorize_repository`), `apps/api/team_access.py`, `apps/api/auth.py::named_credential_required`; `tests/test_team_transport.py` |
| Deferred MCP results and inline byte budgets | `apps/mcp_server/deferred_payload.py`, `tests/test_mcp_deferred_payload.py` |
| Bounded team execution (M2: grants, capabilities, leases, subprocess backend) | `brain/team/execution.py`, `brain/execution/bounded.py`, `docs/operations/TEAM_EXECUTION.md`, `tests/test_team_execution.py` |
| Background jobs | `brain/workers/tasks.py`, `queue.py`, `worker.py` |
| DB schema, migrations | `brain/database/models.py`, `harness_models.py`, `migrations.py` |
| Settings, paths | `brain/config/settings.py`, `paths.py` |

## Entry points

- **HTTP API** — `apps/api/main.py`, routers in `apps/api/routers/`.
- **CLI** — `brain` (`[project.scripts]`) → `apps/cli/main.py`, commands in `apps/cli/commands/`.
- **MCP** — `apps/mcp_server/server.py` and `remote_server.py`.
- **Worker** — `brain/workers/worker.py`; queue `queue.py`, tasks `tasks.py`.
- `brain/__main__.py` — a thin shim over the CLI, no logic there.

## Layer boundaries

- `brain/` — the core: logic, storage, analysis. **It must not import `apps/`.**
  Existing exceptions: 4 lazy imports of `apps.api.helpers` inside functions in
  `brain/insights/proactive.py`, and the `brain/__main__.py` shim. Do not add new ones — move
  shared code into `brain/` instead of pulling transport into the core.
- `apps/` — transport: HTTP, CLI, MCP. Business logic does not belong here.
- Storage: Postgres + pgvector (relational data and vectors), Neo4j (graph, via
  `brain/graph/graph_client.py`), Redis (cache). Services are in `docker-compose.yml`.

## Commands

```bash
docker compose up -d                          # postgres, redis, neo4j, api, worker (+scheduler)
python -m pytest tests/ -q                    # full run takes ~9 min — run it in the background
python -m pytest tests/test_health.py -q      # normal loop: one file
python -m ruff check .                        # line-length 120, select E,F,W
python -m brain.insights.drift_cli check --repo . --base master --candidate HEAD
```

On Windows, if `python` and `pip` on PATH point at different interpreters, use `py -3`.
Deployment: `deploy/RUNBOOK.md` and `deploy/server_up.sh`.

## Pitfalls

- **`reports/` is owned by the containers** (uid 999). Unpacking a release tar over it in
  production always fails on that directory — expected, not a deploy failure; exclude it with
  `--exclude='reports'`. Do not chain the release stamp with `&&` after `tar`.
- **`tests/conftest.py` only neutralises the outside environment** (it clears `.env` keys that
  flip behaviour — `PROJECT_BRAIN_API_KEY` and the like). There is no logic there: tests still
  set settings themselves before importing application modules — hence `E402` in
  `per-file-ignores` for `tests/**`. Do not "fix" the import order.
- **`brain/graph/` holds two live halves plus legacy**: `schema.py` + `graph_client.py` +
  `write_buffer.py` are the write path (used by `file_indexer`); `schema_v2.py`,
  `builder_v2.py`, `extractors_v2.py` + `identity.py` are the query/enrichment path
  (used by `routers/graph_v2.py`). `migration_v3.py` is a LEGACY one-shot migration
  script, not on the runtime path.
- **`rules/golden_tasks.yaml` is an eval contract**, not documentation: "task description →
  expected files and modules". If you change the module layout, update it too, or the eval
  starts lying. A new common task → a new entry there (and a row in the routes table above).
