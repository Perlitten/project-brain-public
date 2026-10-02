# ADR 009: n8n as Operations Orchestration Layer

## Status

Accepted — Phase 9 foundation

## Context

Project Brain needs scheduled and event-driven operations (re-index after merge, nightly health checks, weekly benchmarks, notifications) without coupling that logic to the API or MCP server. Heavy work already belongs in background workers; AI agents use MCP over stdio.

## Decision

Introduce **self-hosted n8n** as an external orchestration layer that calls thin **job trigger API** endpoints on Project Brain. Jobs are enqueued to a **dedicated Redis worker queue** (`brain:worker` prefix). n8n uses its own SQLite data volume and Redis DB 2 for internal queueing — not shared with the brain worker queue.

```
n8n → POST /jobs/* → Redis (brain:worker) → worker → existing brain modules
MCP → stdio → brain modules (unchanged)
```

## Consequences

### Positive

- Clear separation: orchestration vs intelligence vs agent interface
- n8n UI for schedules, webhooks, and notifications without code deploys
- API endpoints stay thin; business logic remains in `brain/` modules
- Foundation for PR impact, backups, ADR import workflows

### Negative

- Another service to operate (n8n container, credentials, workflow exports)
- Polling-based status checks in v1 workflows (no push callbacks yet)
- Two Redis usages on same instance (different DB/prefix — must not mix)

## Out of scope (explicit)

- Retrieval, RRF, graph traversal, context packs, embedding generation, indexing internals
- MCP proxy through n8n (SSE vs stdio incompatibility)

## References

- [docs/phase-9-n8n-orchestration.md](./phase-9-n8n-orchestration.md)
- `n8n/workflows/git-merge-reindex.json`
- `apps/api/routers/jobs.py`, `brain/workers/`
