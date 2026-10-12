# Single-user journey validation — 2026-10-02

Validation of the release path a new user actually follows: **install → configure
credentials → connect an agent → index a repository → complete a task → restart →
upgrade without losing state**, on the single-user distribution profile
(own instance, own repos, own provider keys). Executed on integrated master
`8463044` plus the two validation fixes found on the way (#95, #96).

Every figure below was measured on a live box. "Verified" means run and observed
end-to-end; "untested" means not exercised this pass — they are listed, not
silently omitted.

## Environment

| Component | Version / image |
|---|---|
| OS | Ubuntu (Devin VM), x86_64 |
| Python | 3.12.13 (project `.venv`) |
| FastAPI / uvicorn / pydantic | 0.141.1 / 0.54.0 / 2.13.5 |
| Docker / compose | 29.7.2 / v5.4.0 |
| Postgres | `pgvector/pgvector:pg15` (47af0b65960b) on host :5433 |
| Redis | `redis:7-alpine` (858f009f9709) on :6379 |
| Neo4j | `neo4j:5.12-community` (211c4afab9a7) on :7474/:7687 |
| Sandbox image | `debian:bookworm-slim` (3783cc01769c) |
| Target repo indexed | `bpmn-builder` (550 TS files) + `Brain` itself (714 files) |
| Providers | mock LLM + mock embeddings (no provider keys on this box) |
| API auth | ON — `.env` shipped default `PROJECT_BRAIN_API_KEY=change-me-in-production` |

## Journey steps — all verified

| # | Step | Command | Result | Time |
|---|------|---------|--------|------|
| 1 | Install | `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` | clean | ~45s |
| 2 | Configure | `cp .env.example .env`; set `TARGET_REPO_PATH` | works as shipped | — |
| 3 | Services | `docker compose up -d postgres redis neo4j` | all 3 healthy | ~16s |
| 4 | Index target repo | `brain index` on bpmn-builder | 550 files indexed | ~18s |
| 5 | Start API | `.venv/bin/uvicorn apps.api.main:app --port 8000` | `/health` all green | <3s |
| 6 | First useful task | POST `/context` for an indexed repo | real pack built | ~50s **install→first task** |
| 7 | Search via HTTP | `POST /search` | real ranked results | <1s |
| 8 | Agent integration | MCP stdio `python -m apps.mcp_server.server` | `initialize` + `tools/list` (10 tools) + `search_code` | 1.1s |
| 9 | Restart | `docker compose restart` | state persists — same search results, index intact | ~10s |
| 10 | Upgrade leg | pull master; `scripts/check_migrations.py`; `brain index` incremental | migrations idempotent; re-index skipped 714/714 unchanged | 4s |

`time-to-first-task` (venv already installed, from `docker compose up`): **~50s**.
Cold install→first task is dominated by `pip install` (~45s) + image pulls (~60s first time).

## Reliability exercises — verified live

| Exercise | Method | Evidence |
|---|---|---|
| Backup | `scripts/backup_runner.py` against live stack | `postgres_dump.sql` **103MB**, `neo4j_export.jsonl` **5.3MB** (3,628 nodes / 13,964 rels), manifest checksums |
| Restore drill | `verify_restore_drill --pg-url` into scratch DB | **19 tables restored**, checksums `ALL_MATCHED`, `restore_drill_status: verified`, scratch dropped |
| Long-job crash recovery | `POST /jobs/reindex` (`clean:true`), SIGKILL worker mid-run | lease expiry ~65s → `recover_stale` requeued → attempt 2/3 → **completed**; killed attempt recorded as `failed`/`interrupted ("worker restart")` — no phantom success |
| Job ownership/fencing | as above | only lease-holder commits; recovered job re-claimed by replacement worker |
| Index completeness | `indexing_runs` table | run 6: `file_counts {discovered 714, indexed 714, failed 0}`; `embedding_verification`: 7043/7043 chunks, `pgvector_coverage_pct 100.0`, `pass: true` |
| Incremental index | re-run on unchanged tree | 714/714 `unchanged`, 0 processed — no duplicate writes |
| Docker sandbox backend | `resolve_sandbox_backend()` → `wrap_docker`, executed | real container: hostname differs, uid 1000, `--network none` (curl → **NET-BLOCKED**), `--read-only` rootfs (**ROOTFS-RO**), `--cap-drop ALL`, `no-new-privileges`, pids/mem/cpu caps; workspace rw-mounted at its host path |
| Full test suite on configured box | `pytest tests/ -q` with `.env` + real services | **1508 passed, 2 skipped, 0 failed** in 50.7s (after #96; was 26 failed before) |

## Bugs found and fixed during validation

| PR | Bug | Why it survived |
|----|-----|-----------------|
| #95 | `restore_verification_drill` live leg invoked `createdb --dbname` (option never existed) and corrupted `--pg-url` without a db path | live leg only ran in ops contexts with pg tooling; no unit coverage of `_live_restore_postgres` |
| #96 | Suite not hermetic: configured `.env` (`PROJECT_BRAIN_API_KEY`) enabled auth inside tests → 26×401 | suite previously only ran on unconfigured checkouts |

## Manual workarounds required (friction to fix or document)

1. **`pg_dump`/`psql` must exist on PATH** — host `postgresql-client` (pg14) mismatched the pg15 server; workaround: shim that docker-execs `pg_dump` inside `brain-postgres`. Options for release: document "install matching postgresql-client", or have backup_runner prefer `docker exec` when a compose-managed postgres is detected. (Blueprint suggestion filed for session env.)
2. **`debian:bookworm-slim` must be pulled** before the lab sandbox selects docker (`docker pull debian:bookworm-slim`). Otherwise `auto` silently falls to `unshare`/`off` — correct behavior, but the installer should prefetch or the docs should say so.
3. **`.env` ships a working default API key** (`change-me-in-production`) — auth is on out of the box; user must send `X-API-Key`. Good default; document the header.
4. `n8n` backup artifact skipped via `--allow-missing` — n8n not running during drill (optional service).

## Untested paths this pass (explicit)

- **Real provider credentials** — no OPENAI/ANTHROPIC/GOOGLE/NVIDIA key on this box; all LLM/embedding legs ran mock. Provider auth/format paths unverified against live APIs.
- **Remote MCP server** (`remote_server.py`, HTTP) — only the local stdio server was exercised.
- **n8n artifact restore** — n8n container not running; manifest tolerated it.
- **unshare sandbox backend** — docker present so `auto` never fell through; unshare path untested on this box (covered by unit tests with mocks).
- **Dashboard human login** — `BRAIN_DASHBOARD_AUTH_ENABLED` unset (disabled in local); session-cookie path not exercised end-to-end.
- **Wheel/sdist install** (`pip install dist/*.whl` into a clean venv) — not re-run this pass; #69's smoke verified it earlier.
- **Upgrade across a real version boundary** — exercised as pull-master + migrate + reindex on the same major schema; no cross-version migration fixture.

## Verdict for this gate

The single-user journey works on the documented commands with two small
frictions (pg client, sandbox image prefetch). Install→first-task is ~50s on a
warm box. Crash recovery, backup/restore, fencing, index completeness, and the
docker sandbox all verified live against real services. Remaining gaps are
enumerated above, not assumed.
