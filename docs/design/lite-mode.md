# Lite mode — implementation and limits

**Status:** implemented on unreleased main. Published v0.9.2 still uses the
full PostgreSQL, Neo4j and Redis installation.

## Runtime profile

`brain init --lite` saves a private repository-local profile; `brain index`,
`brain mcp` and `brain serve --lite` select it before importing database engines.
`BRAIN_STORAGE_MODE=lite` and `BRAIN_LITE_DIR` also select the SQLite backend.
Full mode remains the default for existing Compose deployments.

| Responsibility | Lite implementation | Limit |
|---|---|---|
| Relational memory and indexing | SQLAlchemy + aiosqlite, UTC timestamps, integer identities, foreign keys, WAL | Serialized transactions; local single-operator use |
| Vector search | Stored float32 blobs, scoped streaming NumPy exact cosine | 50,000 chunks; no ANN index |
| Graph | SQLite adjacency, scoped identity merges, neighbors and two-hop impact | Arbitrary Cypher requires full mode |
| Maintenance | SQLite queue with expiring ownership; SQL lease fences; API-owned worker | One maintenance worker; supported indexing/health/embedding jobs |
| Cache | SQLite TTL cache with atomic NX claims | Local cache, no Redis service |
| Default embeddings | Offline token-feature hashing | Lexical similarity, not learned semantic retrieval |

The selected Lite profile ignores the full checkout's dotenv configuration;
explicit process variables or `BRAIN_ENV_FILE` configure optional providers.
Chat synthesis is disabled by default. Foreground indexing uses deterministic
summaries plus embeddings; optional chat enrichment requires explicit opt-in.

## Persistence and concurrency

State lives under `.brain` (or an explicit directory). The profile is created
with private permissions and its API binds to loopback. Git's local exclude
file ignores the state directory, and source scans and release packaging
exclude it. No source history or released archives are rewritten.

SQLite uses `BEGIN IMMEDIATE` to serialize transactions, including lease
claims and fenced writes; SQLite ignores `FOR UPDATE`, so normal PostgreSQL
lease code requires this transaction boundary. Repository indexing additionally
uses an OS file lock, released on process exit, to prevent overlapping runs.
Queue results require an unexpired matching ownership token. Interrupted jobs
can be reclaimed after expiry; source verification remains authoritative.

The API lifespan owns the maintenance worker. Stdio MCP needs no daemon;
queued work waits until `brain serve --lite` runs. Direct `brain index` works
without a worker. Graph administration/deep operations requiring Cypher are
explicitly unavailable, not simulated by empty query results.

## Acceptance

A separate CI job provisions no database services. The smoke test rejects
external client construction, indexes a two-file repository, writes scoped
decisions/rules/learnings, calls MCP search/context/impact, checks adjacency,
executes a queued reindex under SQL fencing, and repeats in a new process.
Full-stack tests continue against the original PostgreSQL/Redis/Neo4j profile.

This is local single-operator storage, not a new multi-tenant production claim.
Back up all state with writers stopped. Initial schemas initialize idempotently;
future incompatible schema changes need explicit versioned Lite migrations.
No semantic benchmark improvement or fixed speedup follows from this profile.
