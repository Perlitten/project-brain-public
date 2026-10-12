# ADR 009: External Operations Orchestration Layer

## Status

Superseded by [ADR 013](013-worker-scheduler.md) (2026-10-06) — the external orchestrator was removed; scheduling runs inside the worker.

Previously: Accepted — Phase 9 foundation

## Context

Project Brain needs scheduled and event-driven operations (re-index after merge, nightly health checks, weekly benchmarks, notifications) without coupling that logic to the API or MCP server. Heavy work already belongs in background workers; AI agents use MCP over stdio.

## Decision

Introduce a **self-hosted external orchestration layer** (workflow engine) that calls thin **job trigger API** endpoints on Project Brain. Jobs are enqueued to a **dedicated Redis worker queue** (`brain:worker` prefix). The orchestrator uses its own SQLite data volume and Redis DB 2 for internal queueing — not shared with the brain worker queue.

```
orchestrator → POST /jobs/* → Redis (brain:worker) → worker → existing brain modules
MCP → stdio → brain modules (unchanged)
```

## Consequences

### Positive

- Clear separation: orchestration vs intelligence vs agent interface
- the orchestrator UI for schedules, webhooks, and notifications without code deploys
- API endpoints stay thin; business logic remains in `brain/` modules
- Foundation for PR impact, backups, ADR import workflows

### Negative

- Another service to operate (the orchestrator container, credentials, workflow exports)
- Polling-based status checks in v1 workflows (no push callbacks yet)
- Two Redis usages on same instance (different DB/prefix — must not mix)

## Out of scope (explicit)

- Retrieval, RRF, graph traversal, context packs, embedding generation, indexing internals
- MCP proxy through the orchestrator (SSE vs stdio incompatibility)

## References

- `the orchestrator/workflows/git-merge-reindex.json` (removed with the orchestrator)
- `apps/api/routers/jobs.py`, `brain/workers/`
