# Lite mode — design notes

**Status:** research only. Goal: decide whether a single-container (or
single-binary) install is viable, what it costs, and whether a cheap
intermediate step exists.

## The real barrier today

A first install pulls three backing services — Postgres+pgvector, Neo4j,
Redis — via `docker-compose.yml`. That is ~2 GB RAM resident and three new
concepts for someone who just wants `brain index` on their laptop.

What each service actually does:

| Service | Used for | Touch points |
|---|---|---|
| Postgres + pgvector | All relational state (jobs, memory, repos, chunks) **and** vector search (`vector(N)`/`halfvec(N)` columns, HNSW indexes) | `brain/database/` (async SQLAlchemy + asyncpg), `brain/embeddings/pgvector_sql.py` |
| Neo4j | Code graph: modules/imports/dependents for `impact`, couplings, architecture rules | `brain/graph/graph_client.py`, `builder_v2.py`, `extractors_v2.py` |
| Redis | Job queue for worker (`brain/workers/queue.py`), freshness/caches | `brain/workers/`, `brain/config/settings.py` (`REDIS_URL` optional already) |

Important: `REDIS_URL` is already `Optional` — the queue degrades, and jobs
can run in-process. So the queue is not the hard part; the two databases are.

## Option A — SQLite + sqlite-vec, in-process everything (the "single file")

Replace Postgres with SQLite + `sqlite-vec` for embeddings, replace Neo4j
with edge tables in the same SQLite, drop Redis entirely (in-process queue
+ `asyncio` scheduler).

**What you lose:**
- pgvector `halfvec` + HNSW tuning knobs; sqlite-vec is brute-force per
  partition — fine under ~100k chunks, degrades after.
- Concurrent writers: single-writer SQLite blocks the worker during API
  writes. Mitigable with WAL + a write queue, but that IS Redis by another
  name.
- Neo4j graph queries (variable-length traversals) become recursive CTEs —
  the v2 extractor/builder SQL is written in Cypher; every query path needs
  a second implementation or an abstraction layer.
- Test matrix doubles unless lite is a separate supported dialect
  (`brain/database/session.py`, `migrations.py` are asyncpg/SQLAlchemy —
  SQLAlchemy covers most of it, raw `pgvector_sql.py` doesn't).
- The public claim "production-ready self-host" forks into "lite" vs "real"
  with different limits — doc and support surface grows.

**Effort estimate:** 3–5 days for a *working* spike (SQLite dialect +
sqlite-vec DDL + Cypher→CTE port for the two hottest graph queries), 2+
weeks to make it supportable (test matrix, migrations parity, docs). This
is a product line, not a flag.

## Option B — all-in-one container (the cheap step)

One image that bundles Postgres+Neo4j+Redis via a process supervisor, with
named volumes. User runs `docker run -p 8000:8000 project-brain`.

- **What you lose:** almost nothing — same binaries, just orchestrated
  inside one container instead of compose. RAM footprint identical; the win
  is "one command" UX.
- **Effort:** ~half a day (supervisord/s6 config, health gating, docs).
  Builds on the existing Dockerfile; CI gets a `brain-all-in-one` image tag
  in the release workflow — trivially additive.
- **Honest verdict:** this removes the *cognitive* barrier (three services,
  compose file, mounts) but not the *resource* one (~2 GB RAM stays).

## Option C — hosted-lite (no local services at all)

`brain` CLI talks to a hosted Brain API. Zero local infra; needs a hosted
service that doesn't exist yet — roadmap item, not an install fix.

## Recommendation

1. **Now:** Option B (all-in-one image) as the "docker pull and run" path —
  half a day, no product fork.
2. **Later, only if single-binary is a stated goal:** Option A as a real
   project with a lite dialect — do NOT sneak it in as a config flag; it
   needs its own test matrix and doc.
3. Keep `REDIS_URL` optional semantics — the in-process fallback already
   exists and is the right pattern for lite.
