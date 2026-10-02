# ADR-011 — Indexing operations: cost, hazards, and how to run a full re-index

Date: 2026-07-26
Status: active

## Context

A change to chunk sizing (50 lines + 10 overlap, sized to fit the embedding
provider's 2048-char input cap) only affects files as they are re-indexed, so
every indexed repository had to be rebuilt. Preparing that run surfaced one
performance problem and one way to lose data.

## Decision 1 — per-chunk LLM summaries are off by default

Indexing made **two** network round-trips per chunk: an LLM summary and an
embedding. `file_chunks.summary` is consumed by nothing that matters:

- ranking scores embeddings of the chunk **content** (`brain/search/code_search.py`);
- `/ask` injects `c["content"]`, never `c["summary"]` (`apps/api/routers/core.py`);
- its only appearance is the `/search` payload, where it paraphrases the content
  sitting next to it and inflates every response.

Chunk summaries are therefore skipped unless `BRAIN_CHUNK_SUMMARIES=1`.

Measured on live traffic: **2.4 files/min → 53 files per 5 min (~4.4x)**. A
17-chunk file went from roughly a minute to ~8 seconds.

This is deliberately **not** folded into the existing `FAST_INDEX=true` flag.
That flag also stubs out the **file** summary, and `File.summary` is what the
lexical retrieval channel matches against with `ILIKE`. That channel measures
15% Hit@3 against the golden set — the weak half of retrieval. Trading it for
indexing speed would be the wrong trade.

## Decision 2 — verify the source path exists before any clean re-index

`index_repository(..., clean=True)` calls `clear_repository_index()` and
`purge_repository_graph()` **before** it indexes anything. If the source
directory is absent, the result is an emptied index and nothing to refill it.

This is not hypothetical. `/repos/nostia`, `-vault`, `-memory` and `-site` are
staged into the worker with `docker cp` (see `~/nostia-reindex.sh` on the host),
so they live in the **container's writable layer**. Any
`docker compose up -d --build worker` erases them while the Postgres index rows
survive — an index pointing at a source that no longer exists. A clean re-index
over that list would have destroyed ~6800 chunks of Nostia knowledge.

Before enqueuing a clean re-index:

```bash
docker exec brain-worker test -d <repo_path> || echo "REFUSE: clean would wipe this index"
```

**Follow-up (not yet done):** mount those directories read-only, the way
`BRAIN_INDEXED_PROJECTS_DIR` already backs `/indexed`, so a rebuild cannot erase
them. `docker cp` staging is the underlying defect.

## Reliability update (issue #39)

Indexing now publishes a durable heartbeat and file/chunk/graph counters to
`/jobs/{id}` and `/api/status/indexing`. Terminal verification includes repository
identity, exact source revision, duration, embedding inventory, and bounded
failure examples (path, chunk index, error type, HTTP status; no request secrets).

Changed files use a fixed worker count, provider calls share a separate semaphore,
and embeddings use bounded batches with per-chunk fallback. NVIDIA transient
500/429 responses retry with backoff and numeric Retry-After. Graph imports use a
precomputed stem index; relationship writes use scoped UNWIND batches.

The settings in `.env.example` bound concurrency, batch size, heartbeat interval,
and automatic embedding repair size. Local late-interaction dual writes remain
serial because their provider lifecycle is shared.

File hash, chunks, symbols, and embeddings commit together after provider work.
An interrupted attempt retains the old complete file projection. PostgreSQL
repository locks exclude overlapping reindex/repair attempts and release on
worker death; Redis leases renew during long work and duplicate deliveries cannot
replace a live lease. Old running index records are marked interrupted on replay.

Git runs require a clean working tree and tracked indexed files. Content hashes
are captured before indexing, checked against each file read, and compared with
a final scan; terminal verification retains their digest for causal repair.
Repository locks require at least two available PostgreSQL connections; invalid
pool configurations fail at startup. Every write transaction also checks the
durable job fence introduced by the worker reliability changes.

Source changes and runs with no usable projection finish as `failed`. Partial
file/graph projections finish as `degraded` without advancing the indexed commit.
Missing/incompatible/stale vectors after a complete projection also finish as
`degraded` and expose a repair request; a worker enqueues an
idempotent embedding repair with the parent job, run, repository, and revision.
The repair promotes only its latest exact run after full embedding verification.
A bounded repair that leaves gaps remains degraded. Neither `verify_after=false`
nor partial provider success bypasses the completed/exact-commit harness gate.

## Earlier operational consequences

- Repository rows whose path cannot exist inside the container (e.g. Windows
  paths such as `D:/projects/...`) can never be refreshed from the server. They
  are stale by construction and should be deleted rather than re-indexed, after
  a fresh index of the same source lands under an `/indexed/...` path.
- During bulk indexing, `/ask` times out: LLM synthesis queues behind embedding
  load. `search_code` needs only a query embedding and stays usable. Expect this
  for the duration of a full run and do not mistake it for a broken endpoint.
- Chunk boundaries changed, so a full re-index is required for the new chunking
  to apply uniformly; incremental indexing is content-hash driven and will
  otherwise migrate files only as they change.
