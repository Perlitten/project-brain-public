# LFM2.5-ColBERT canary, cutover and rollback

## Safety boundary

LFM2.5-ColBERT produces a matrix (`tokens × 128`), not a single vector.  Never
change `DEFAULT_EMBEDDING_PROVIDER` or `EMBEDDING_DIMENSION` to 128: doing that
would recreate the existing pgvector column and invalidate NVIDIA embeddings.
The production target keeps matrices inside an authenticated co-located
matrix/rerank service and sends Brain only chunk IDs/hashes plus returned
scores. The service can use GPU/PyLate (`deploy/lfm-colbert-gpu`) or the
CPU/llama.cpp wrapper (`deploy/lfm-colbert-cpu`). The earlier
`late_interaction_embeddings` PostgreSQL table and a raw llama.cpp endpoint
remain reversible research artifacts; Brain never calls the raw endpoint
directly. All gates default off.

## Operator visibility

Cockpit exposes the selected repository's precision-layer state at
`/dashboard/late-interaction` and as JSON at
`/dashboard/late-interaction.json?repo=<repository path>`.

Treat this as the release truth surface, not as an enable switch. It separates
live model/index/lineage identity and base-index freshness, infrastructure
registration from effective traffic, retained diagnostic evidence from
process-local counters, and offline/shadow/online readiness.

The page must show `NO-GO` while any identity, freshness, evidence, quality,
latency or production-approval gate is missing. Do not infer approval from a
healthy sidecar or from a completed query count.

Runtime settings and `deploy/server_up.sh` enforce two independent
authorizations, including when an operator bypasses the script with raw
Compose:

- `LATE_INTERACTION_EXPERIMENT_AUTHORIZED=true` is required for
  `ENABLED`, dual-write, or shadow after complete sync and offline checks;
- `LATE_INTERACTION_PRODUCTION_GATES_PASSED=true` is additionally required
  for rerank or any non-zero canary percentage.

Every active authorization must match the exact repository, build SHA, baked
source digest, model revision, approved index revision, persistent corpus
lineage, identity digest and document count. In production the
build SHA and source digest come from `.brain-source-manifest.json` baked into
the image, so `.env` cannot spoof them.
Production approval also requires a relative path under `REPORT_OUTPUT_DIR`
and the SHA-256 of that actual evidence bundle; preflight and runtime both open
and hash the file. A rebuild, reindex, missing artifact or modified artifact
therefore fails closed instead of inheriting stale `true` values. Remote
provider registration remains allowed with both authorizations false because
it is inert and is needed for bounded sync and offline evaluation.

The NVIDIA dense vector remains the first-stage recall channel and complete
fallback during shadow/canary.  This follows Qdrant's official late-interaction
guidance: retrieve a bounded pool with a fast dense channel, then MaxSim-rerank
that pool rather than brute-force every token vector.

The target request path is:

```text
NVIDIA/pgvector recall (≤500 chunk IDs)
  -> authenticated matrix/rerank service (CPU evaluation or GPU target)
  -> cached BF16 token matrices + batched MaxSim
  -> deterministic final counterfactual rerank
  -> shadow evidence or canary result
```

## Stage 0 — legal and capacity gate

- Confirm the deployment is allowed by LFM Open License v1.0. Commercial use by
  a legal entity with annual revenue at or above USD 10M is not licensed under
  that agreement.
- Keep at least 15 GiB free persistent storage before sync and a verified
  backup of both PostgreSQL and the matrix volume.
- Confirm the rerank service is reachable only over loopback, a private Compose
  network, or an encrypted private VPN. Never bind it to a public interface.
- Confirm `nvidia-smi` and the NVIDIA container runtime work before building
  the image. The Compose service must see a CUDA device.
- Do not proceed if the service raises core Brain p95 latency or resource
  pressure during representative load.

## Stage 1 — isolated GPU runtime

The CPU/GGUF experiment below is retained as evidence, not as the target
deployment. On a GPU host:

```sh
cd deploy/lfm-colbert-gpu
cp .env.example .env
# Set a random token, a private bind address, and acknowledge the license.
docker compose config --quiet
docker compose build
docker compose up -d
curl -fsS http://127.0.0.1:8090/health
curl -fsS -H "X-Late-Interaction-Token: $LATE_INTERACTION_SERVICE_TOKEN" \
  http://127.0.0.1:8090/ready
```

`/ready` deliberately loads the pinned BF16 model
`LiquidAI/LFM2.5-ColBERT-350M@59633c2e31717b3502343ff566bee9fda3261943`;
the first call may download the model into the persistent cache volume. The
container runs PyLate 1.4.0 on the digest-pinned PyTorch/CUDA base image.

Require a real NVIDIA device, exact 32/512-token × 128 output, no restart/OOM,
and the end-to-end latency gates below before continuing. Capture GPU model,
driver, CUDA, PyTorch and PyLate versions with the benchmark artifact.

### Retained CPU/GGUF experiment

From `~/lfm-colbert-canary` in the deploy user's home on the VPS (see
`deploy/RUNBOOK.md` for the current host and SSH user):

```sh
docker compose build
docker compose create
sh ./fetch-model.sh
docker compose up -d
curl -fsS http://127.0.0.1:8089/health
```

Run `scripts/lfm_colbert_canary_benchmark.py` through an SSH loopback tunnel.
Require:

- exact output shape: query `32 × 128`; document at most `512 × 128`;
- all token rows finite and L2-normalized by the client;
- representative semantic correctness cases pass;
- query embedding p95 ≤ 250 ms and document embedding p95 ≤ 750 ms on this VPS;
- no OOM/restart and memory ≤ 2.5 GiB;
- model volume survives a controlled restart and its SHA-256 remains exact.

These thresholds are local deployment gates, not vendor benchmark claims.

### 2026-07-28/29 CPU VPS outcome

The isolated BF16 canary passed model identity, SHA, health, restart,
resource-stability and 3/3 semantic correctness checks, but failed latency:
query p95 was 2.59 s, representative chunk p95 was 10.08 s and top-50 MaxSim
p95 was 3.56 s. The raw canary was stopped cleanly; port 8089 is closed and the
verified model volume is retained.

On 2026-07-29 the authenticated CPU wrapper was deployed privately on the same
VPS for inert sync/evaluation. A direct 25-candidate rerank took 6.46 s and a
production-shaped search took 5.46 s. One-request batches of 2/4/8 long
documents and llama.cpp `--parallel 2` both reduced throughput versus the
single-document/single-slot profile. Keep the service concurrency and sync
batch at one. **This remains a synchronous production runtime NO-GO.**

An isolated official PyLate 1.4.0 + PyTorch 2.7.1 CPU reference was also
measured on the same host: 43.47 s for one representative document and
23.93 s/document in a four-document batch. It was slower than GGUF and consumed
about 1.8 GiB RAM, so its temporary image/model cache were removed. “Supports
CPU” is true for both runtimes; neither satisfies the online gate on this
shared VPS.

The production acceptance query about per-job schema DDL and malformed LLM
self-diagnosis is now a mandatory quality gate. On the bounded eight-file
comparison, only two of four must-hit files reached top four
(precision@4=0.50, recall@4=0.50); both test files ranked sixth and seventh.
Future GPU/PLAID experiments must achieve 4/4 before any active ranking rollout.

### CPU wrapper deployment

`deploy/lfm-colbert-cpu` wraps the private BF16 llama.cpp endpoint with the same
authenticated sync/status/rerank contract as the GPU service. It uses a local
SQLite matrix store and batched server-side MaxSim. On this VPS it is an
offline/inert evaluation service only.

CPU sync must use `LATE_INTERACTION_REMOTE_SYNC_BATCH_SIZE=1` and the separate
`LATE_INTERACTION_REMOTE_MUTATION_TIMEOUT_S=120`. The ordinary remote timeout
stays at two seconds for retrieval. Never send a 64-document mutation to this
CPU backend: the CPU service enforces a one-document limit and rejects larger
batches before encoding.

Indexing additionally caps each stored source chunk at64,000 characters.
Symbol and overlapping line windows below this limit keep their boundaries;
oversized windows, including single-line JSONL/minified input, are subdivided
without truncating content. A normal incremental reindex repairs legacy
oversized chunks even when the file content hash is unchanged. Reconcile the
sidecar after that reindex; do not increase service limits or claim an exact
inventory while a413 failure remains.

`LATE_INTERACTION_REMOTE_SOURCE_PAGE_SIZE=256` keeps the Postgres scan paged
independently from that mutation limit. Resumed sync first loads the remote
identity manifest and skips exact `chunk_id/path/content_hash` matches before
calling the service; path-only renames remain updates. Every remote inventory
scan must remain on one `index_revision`, and the sync performs a final
reconciliation pass. A destructive prune is refused while any dual-write,
shadow, rerank, canary or active late-interaction flag is enabled; immediately
before deletion it rechecks the candidate IDs in authoritative Postgres. The
JSON proof distinguishes two scopes. A write with `--max-chunks` verifies only
the non-empty rows scanned by that invocation and reports
`verification_mode=bounded_subset`; both identity digests cover that same
projection. It is a canary/resume proof, not evidence that the whole corpus is
current. An unbounded write performs a fresh Postgres scan and reports
`verification_mode=exact`; it succeeds only when the complete Postgres and
sidecar inventories are identical. Consequently, stale remote IDs make an
unbounded non-prune write fail until a safe offline prune reconciles them.

### GPU runtime parity gate

Before interpreting any GGUF quality result, compare the exact remote/GGUF
ranking against a PyLate BF16 reference on the same fixed corpus. Heavy PyLate
dependencies are deliberately optional and must live in the isolated GPU
environment, not in the Brain API image:

```sh
python eval/late_interaction_runtime_parity.py \
  --corpus reports/lfm/parity-corpus.json \
  --pylate-model LiquidAI/LFM2.5-ColBERT-350M \
  --pylate-device cuda \
  --remote-url http://127.0.0.1:8089 \
  --export-reference reports/lfm/pylate-bf16-rankings.json \
  --export-candidate reports/lfm/gguf-rankings.json \
  --json reports/lfm/runtime-parity.json
```

The deterministic CI/review path replays those saved rankings and imports no
PyLate/torch dependency:

```sh
python eval/late_interaction_runtime_parity.py \
  --corpus reports/lfm/parity-corpus.json \
  --reference-rankings reports/lfm/pylate-bf16-rankings.json \
  --candidate-rankings reports/lfm/gguf-rankings.json \
  --json reports/lfm/runtime-parity-replay.json
```

The corpus contract is `eval/runtime_parity_corpus.schema.json`. Require mean
top-10 overlap >= 0.90, mean Spearman >= 0.95, mean Kendall >= 0.90, and maximum
absolute per-query NDCG@10 delta <= 0.02. Record the exact PyLate package,
model revision, GGUF checksum/runtime revision and preprocessing flags with the
artifacts. A parity failure blocks quality conclusions: it means the runtime
contract changed, not necessarily that the model is bad.

## Stage 2 — deploy inert code and synchronize matrices

Deploy Brain with ranking gates false. `LATE_INTERACTION_REMOTE_ENABLED=true`
only constructs the authenticated client; it does not affect retrieval while
`LATE_INTERACTION_ENABLED=false`.

```env
LATE_INTERACTION_ENABLED=false
LATE_INTERACTION_DUAL_WRITE_ENABLED=false
LATE_INTERACTION_SHADOW_ENABLED=false
LATE_INTERACTION_SHADOW_PERSIST_ENABLED=true
LATE_INTERACTION_RERANK_ENABLED=false
LATE_INTERACTION_CANARY_PERCENT=0
LATE_INTERACTION_EXPERIMENT_AUTHORIZED=false
LATE_INTERACTION_PRODUCTION_GATES_PASSED=false
LATE_INTERACTION_APPROVED_REPOSITORY_ID=
LATE_INTERACTION_APPROVED_BUILD_SHA=
LATE_INTERACTION_APPROVED_SOURCE_DIGEST=
LATE_INTERACTION_APPROVED_MODEL_REVISION=
LATE_INTERACTION_APPROVED_INDEX_REVISION=
LATE_INTERACTION_APPROVED_LINEAGE_ID=
LATE_INTERACTION_APPROVED_IDENTITY_DIGEST=
LATE_INTERACTION_APPROVED_DOCUMENT_COUNT=
LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH=
LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256=
LATE_INTERACTION_REMOTE_ENABLED=true
LATE_INTERACTION_REMOTE_URL=http://PRIVATE_GPU_ADDRESS:8090
LATE_INTERACTION_REMOTE_TOKEN=REDACTED_SHARED_TOKEN
LATE_INTERACTION_ALLOW_REMOTE_PROVIDER=true
LATE_INTERACTION_REMOTE_MODEL=LiquidAI/LFM2.5-ColBERT-350M
LATE_INTERACTION_REMOTE_MODEL_REVISION=59633c2e31717b3502343ff566bee9fda3261943
```

Dry-run, write one bounded canary batch, verify, then perform a complete
idempotent sync. `--prune` is accepted only on a complete unbounded write:

```sh
brain embeddings late-remote-status --repo /app --json
brain embeddings late-remote-sync --repo /app --max-chunks 25 --json
brain embeddings late-remote-sync --repo /app --max-chunks 25 --write --json
brain embeddings late-remote-sync --repo /app --write --prune --json
brain embeddings late-remote-status --repo /app --json
```

The service compares content hashes before encoding, so reruns report unchanged
documents instead of recomputing matrices. Matrices never cross the service
boundary. A `bounded_subset` result proves only that canary batch. Require an
unbounded `exact` result with 100% current candidate coverage and zero stale IDs
before any active ranking experiment. After the first complete sync, restart
the sidecar with its own `LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID`,
`LATE_INTERACTION_SERVICE_MINIMUM_INDEX_REVISION`, and
`LATE_INTERACTION_SERVICE_MINIMUM_DOCUMENT_COUNT` set to the verified floor.
Record the returned `lineage_id`, `identity_digest`, and `document_count` in the
Brain approval fields above.

The legacy `late-backfill`/`late-status` commands operate on the old PostgreSQL
matrix store and are not used for the GPU target.

## Stage 3 — dual write plus shadow

Enable remote dual-write and durable shadow only after the full sync. The
FileIndexer commits NVIDIA/pgvector first, then sends authenticated batches to
the GPU service; remote failure opens a local circuit but cannot roll back the
authoritative dense index.

```env
LATE_INTERACTION_ENABLED=true
LATE_INTERACTION_DUAL_WRITE_ENABLED=true
LATE_INTERACTION_SHADOW_ENABLED=true
LATE_INTERACTION_SHADOW_PERSIST_ENABLED=true
LATE_INTERACTION_RERANK_ENABLED=false
LATE_INTERACTION_CANARY_PERCENT=0
LATE_INTERACTION_EXPERIMENT_AUTHORIZED=true
LATE_INTERACTION_PRODUCTION_GATES_PASSED=false
LATE_INTERACTION_APPROVED_REPOSITORY_ID=EXACT_INDEXED_REPOSITORY_ID
LATE_INTERACTION_APPROVED_BUILD_SHA=EXACT_CURRENT_BUILD_SHA
LATE_INTERACTION_APPROVED_SOURCE_DIGEST=EXACT_BAKED_SOURCE_DIGEST
LATE_INTERACTION_APPROVED_MODEL_REVISION=EXACT_PINNED_MODEL_REVISION
LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION=EXACT_FULL_SYNC_INDEX_REVISION
LATE_INTERACTION_APPROVED_INDEX_REVISION=EXACT_FULL_SYNC_INDEX_REVISION
LATE_INTERACTION_APPROVED_LINEAGE_ID=EXACT_PERSISTENT_LINEAGE_ID
LATE_INTERACTION_APPROVED_IDENTITY_DIGEST=EXACT_FULL_SYNC_IDENTITY_DIGEST
LATE_INTERACTION_APPROVED_DOCUMENT_COUNT=EXACT_FULL_SYNC_DOCUMENT_COUNT
LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH=
LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256=
LATE_INTERACTION_REMOTE_ENABLED=true
LATE_INTERACTION_REMOTE_URL=http://PRIVATE_GPU_ADDRESS:8090
LATE_INTERACTION_REMOTE_TOKEN=REDACTED_SHARED_TOKEN
LATE_INTERACTION_ALLOW_REMOTE_PROVIDER=true
```

With dual-write enabled, the approved full-sync `rN` is a minimum readiness
floor rather than a permanently fixed value. Status, manifest reads and rerank
fail closed below that floor. Mutations do not: an exact `index_revision`, when
supplied, remains a compare-and-swap precondition, while
`minimum_index_revision` is deliberately ignored by mutation endpoints. This
lets an empty or restored store rebuild from `r0` while all retrieval remains
not-ready until the verified floor is reached. With dual-write disabled, the
client still sends an exact revision for reproducible offline evaluation.
Every scored candidate is matched by repository ID, chunk ID, path and SHA-256
content hash, and every rerank response carries the current corpus lineage,
identity digest and document count for a hot-path release check.

There is intentionally no remote HTTP reset endpoint. For restore or reset:

1. turn off dual-write, shadow, rerank, canary and all active LFM flags;
2. stop the sidecar and take a recoverable copy of its SQLite volume;
3. either restore the complete SQLite file (which preserves its lineage), or
   move the damaged file aside and start an empty store (which gets a new
   lineage on its first successful mutation);
4. run a complete authoritative sync using exact revision CAS. The configured
   service floor may stay in place: writes below it work, but reads/rerank stay
   fail-closed;
5. verify revision, lineage, identity digest and document count, then update the
   approval tuple before re-enabling any active flag.

The approved lineage remains exact. In exact/offline mode, identity digest and
document count must also match exactly; in dual-write mode the approved document
count is a minimum completeness floor while the digest may advance with
authoritative mutations.

Run for at least seven days. Track provider error rate, coverage,
p50/p95 latency, top-rank deltas and golden retrieval metrics. Shadow results
cannot reorder user-visible results.

Shadow evidence is durable in `late_interaction_shadow_events`. Surface code
constructs `brain.late_interaction.shadow.ShadowEvent` only after both final
rankings are available and awaits `record_shadow_event(event)` without making
its boolean result part of retrieval success:

- raw query text is not a field and Pydantic rejects it as extra input;
- `hash_query(repository_id, query)` scopes the SHA-256 identity to one repo;
- `make_shadow_idempotency_key(repository_id, request_id)` hashes the request
  identity so an HTTP telemetry retry carrying the same `X-Request-ID` (or
  `X-Correlation-ID`) is `ON CONFLICT DO NOTHING`; surfaces without a genuine
  caller identity intentionally use a fresh event identity and are counted as
  separate request occurrences;
- each final ranking is limited to 50 items; numeric metadata and the whole
  event JSON are bounded (64 KiB);
- scalar indexes cover creation time, repository+time and status+time; large
  JSON rankings are never indexed;
- persistence and retention cleanup are fail-open.

Default retention is 30 days. Run bounded cleanup until it returns zero:

```python
import asyncio
from brain.late_interaction.shadow import cleanup_shadow_events

removed = asyncio.run(cleanup_shadow_events(retention_days=30, batch_limit=10_000))
print(removed)
```

Keep at least seven full days and 500 successfully recorded eligible queries
after exclusions. Report `scored`, `skipped`, and `error` counts separately;
never turn missing telemetry into a successful quality gate.

The non-loopback opt-in is mandatory because source chunks are sent during
indexing. Rerank requests contain only the query plus chunk IDs, paths and
content hashes; token matrices never leave the GPU service. Keep the endpoint
private and the shared token independent from Brain/n8n credentials.
Late-interaction ranking and dual-write are fail-open. Dual-write opens its
local circuit after three consecutive failures. The hot retrieval path has a
hard 2-second whole-rerank budget, caps the pool at 500 chunks, and active
ranking additionally requires the configured coverage threshold.

## Quality-first asynchronous lane (no online cutover)

The CPU sidecar may be useful even when the latency and paired-quality gates
above block online traffic. This lane is deliberately separate from
`LATE_INTERACTION_ENABLED`, shadow, rerank and canary:

```env
LATE_INTERACTION_DEEP_ENABLED=true
LATE_INTERACTION_DEEP_TIMEOUT_S=120
LATE_INTERACTION_DEEP_VECTOR_LIMIT=200
NIGHTLY_DEEP_MAINTENANCE_ENABLED=true
NIGHTLY_DEEP_MAINTENANCE_REPO_PATH=/app
NIGHTLY_DEEP_MAINTENANCE_QUALITY_QUERIES=3
WORKER_MAX_JOB_TIMEOUT_SECONDS=21600
```

`nightly-deep-maintenance.json` runs at 00:30 UTC. The single worker performs
an incremental reindex, verifies and repairs dense embeddings, exactly
reconciles and prunes the approved LFM corpus, then runs three relevance probes
(including Russian). A probe passes only when LFM scoring is applied with
100% candidate coverage and at least one expected source file reaches top 10.
The job and its n8n execution fail closed on any final contract failure.

`POST /jobs/deep-context` (or MCP `prepare_deep_context`) is the explicit
interactive entry. It reconciles the exact corpus first, spends up to the Deep
timeout on reranking, persists the context pack, and only then marks the
background job complete. `GET /jobs/{job_id}` (or MCP `get_background_job`)
returns the result. Ordinary `/ask`, `/search` and `/context` requests continue
to use the NVIDIA/pgvector baseline; enabling this lane is not evidence or
authorization for a production ranking cutover.

## Stage 4 — ranking canary

Only after shadow quality passes:

```env
LATE_INTERACTION_PRODUCTION_GATES_PASSED=true
LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH=lfm/release-approval.json
LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256=SHA256_OF_IMMUTABLE_APPROVAL_BUNDLE
LATE_INTERACTION_RERANK_ENABLED=true
LATE_INTERACTION_CANARY_PERCENT=1
```

Create `reports/lfm/release-approval.json` as the reviewed evidence bundle and
hash that exact file. `server_up.sh` resolves the relative path against the
host `reports/` directory; API and worker resolve it against `/app/reports`.

The sample is a stable hash of repository + query. Increase 1% → 10% → 50% →
100% only when every stage has:

- paired replay over the original 20 golden queries plus
  `eval/lfm_eval_extension.json` (30 separate curated fixtures, total 50);
- at least 15 RU/RU-to-EN queries, reported as a separate language slice;
- median paired delta NDCG@10 >= +0.03 and NDCG improvement on >= 60% of
  queries;
- Recall@50 no worse than baseline, aggregate Hit@3 regression <= 10 pp, and
  no language/query-class Hit@3 or Recall@10 regression greater than 5 pp;
- p95 end-to-end retrieval increase ≤ 500 ms;
- provider failure rate < 0.1%;
- fail-open tests proving the NVIDIA result remains available.

The official model card lists 11 languages but does not list Russian. The
RU/RU-to-EN slice is therefore a hard local evidence gate, not an extrapolation
from vendor multilingual results. A failure blocks active rollout even if the
English aggregate passes.

Capture paired final rankings through the production retrieval pipeline while
active reranking stays disabled. The capture forces deterministic, no-LLM
classification/reranking so both arms have identical token spend:

```sh
python eval/run_paired_lfm_retrieval.py \
  --repo /app \
  --file-limit 50 \
  --output reports/lfm/paired-rankings.json

python eval/paired_retrieval_eval.py \
  --runs reports/lfm/paired-rankings.json \
  --production-gate \
  --json reports/lfm/paired-eval.json
```

The capture always disables durable shadow persistence. Its curated and
translated fixtures remain in the JSON artifact and cannot count toward the
seven-day/500-event production shadow gate.

Production evaluation uses the datasets baked into the immutable image; the
production Compose file no longer bind-mounts host `eval/`. The report records
SHA-256 and byte size for the capture, both datasets, the capture runner and the
evaluator. `--production-gate` rejects alternate datasets and threshold
overrides and re-hashes all inputs after evaluation to detect mid-run changes.

The capture command exits non-zero if any query has missing, skipped or partial
late-interaction telemetry, if the scored counterfactual is absent, or if either
arm returns fewer than 50 unique paths. Do not evaluate or approve an incomplete
capture. The evaluator also refuses an object envelope unless `complete` is
exactly `true` and `failures` is an empty array; editing those fields cannot
hide a per-result non-scored status.

Depth 50 is evidence about the input ranking, not the number of relevant files.
The capture asks the existing pipeline for `--file-limit 50`: its precision
reranker still orders the first ten, then the current recall pool fills the
ranking to the requested depth. Every variant is stored as:

```json
{
  "ranking": [{"path": "brain/...", "score": 1.23}],
  "depth": {
    "requested": 50,
    "returned_unique": 50,
    "corpus_size": 1234,
    "status": "sufficient"
  }
}
```

A legacy array is eligible for Recall@50 only when it contains at least 50
actual unique paths. A metadata-bearing ranking is eligible only when its
`returned_unique` matches the normalized ranking, requested and returned depth
are both at least 50, and status is `sufficient`. Otherwise `recall@50` is
reported as unavailable (`null`) and the gate fails closed; ten returned items
are never relabelled as Recall@50.

Repositories with fewer than 50 indexed files are recorded explicitly as
`insufficient_corpus_depth`. This is an evidence status, not a waiver: the
Recall@50 gate remains failed until evaluated on a corpus capable of producing
50 unique results.

`fastplaid` remains optional as an experiment, but once present its gate
requires a ranking for every dataset query, a paired delta against the same
baseline for every query, and complete depth-50 evidence for both arms. A
partial FastPLAID subset cannot pass on aggregate metrics. The report includes
per-variant query/depth counts, Hit@3, Recall@10/50, MRR@10, NDCG@10, paired
per-query deltas, language/class slices and machine-readable gate verdicts.

All 50 checked-in queries are curated evaluation fixtures. The RU entries are
translations or synthetic bilingual probes and are explicitly marked
`production_real=false`; they must not be described as production traffic.
Replace/add anonymized real queries later with explicit provenance rather than
silently relabelling these fixtures.

This is a ColBERT precision cutover, not proof that the dense recall channel can
be removed. Replacing dense recall requires a separately benchmarked PLAID (or
equivalent) candidate index; exhaustive MaxSim over all production chunks is a
no-go on this CPU-only host.

## Immediate rollback

Set, in this order:

```env
LATE_INTERACTION_CANARY_PERCENT=0
LATE_INTERACTION_RERANK_ENABLED=false
LATE_INTERACTION_SHADOW_ENABLED=false
LATE_INTERACTION_DUAL_WRITE_ENABLED=false
LATE_INTERACTION_ENABLED=false
LATE_INTERACTION_REMOTE_ENABLED=false
LATE_INTERACTION_ALLOW_REMOTE_PROVIDER=false
```

Restart API/worker, verify `/ready`, then stop the GPU service if required. Do
not drop the legacy additive table or delete either matrix volume during
incident rollback. NVIDIA data and routing were never overwritten, so rollback
requires no reindex.
