---
title: README
created: '2026-07-31'
updated: '2026-07-31'
status: active
tags:
- type/note
---
# LFM2.5-ColBERT CPU precision service

This stack runs the pinned BF16 GGUF model in a private llama.cpp sidecar and
publishes only the authenticated matrix/rerank service on loopback. It does not
set any Project Brain production, shadow, dual-write, rerank, or canary flag.

BF16 is intentional for this VPS. A live 2026-07-29 endpoint probe found its
query path materially faster than Q8_0 and Q4_K_M on the virtualized AVX2 EPYC.
Quantized files remain useful portability candidates, but are not the measured
serving default for this machine.

Pinned inputs:

- llama.cpp commit `7e1e28cae36d41fe7bbe9dae7c9625de6565c063`;
- GGUF repository revision `bc240003aba07253e261a8aaf0d2c9683318a967`;
- `LFM2.5-ColBERT-350M-BF16.gguf`;
- model SHA-256 `c21d5cacc004cbc7746dbeeaee496c74b01f0f7bfdef1e1a57570d1744ef871b`;
- digest-pinned Python and Debian base images;
- exact CPU-service Python dependency versions in `requirements.lock`.

The llama.cpp image is compiled with `GGML_NATIVE=ON` for this exact VPS. It is
host-local even though its source commit and base image are pinned; do not move
the built image to a different CPU. Rebuild from the pinned inputs on the target
host instead.

Prepare and start:

```sh
cp .env.example .env
# Set a random service token and acknowledge the license only after review.
docker compose config --quiet
docker compose build
docker compose up -d
curl -fsS http://127.0.0.1:8094/health
curl -fsS -H "X-Late-Interaction-Token: $LATE_INTERACTION_SERVICE_TOKEN" \
  http://127.0.0.1:8094/ready
```

`model-init` downloads into `lfm_colbert_cpu_models`, verifies SHA-256 before
atomically installing the model, and exits. The matrix store persists in
`lfm_colbert_cpu_matrices`. The llama.cpp port is never published to the host.
The authenticated service also joins the existing private `brain_default`
network and is reachable from Brain as `http://lfm-colbert-cpu-service:8090`.

When the inert service is later registered in Brain, the client identity must
match the GGUF service exactly:

```env
LATE_INTERACTION_REMOTE_ENABLED=true
LATE_INTERACTION_ALLOW_REMOTE_PROVIDER=true
LATE_INTERACTION_REMOTE_URL=http://lfm-colbert-cpu-service:8090
LATE_INTERACTION_REMOTE_TOKEN=THE_SAME_INDEPENDENT_SERVICE_TOKEN
LATE_INTERACTION_REMOTE_MODEL=LiquidAI/LFM2.5-ColBERT-350M-GGUF
LATE_INTERACTION_REMOTE_MODEL_REVISION=bc240003aba07253e261a8aaf0d2c9683318a967
LATE_INTERACTION_REMOTE_TIMEOUT_S=2
LATE_INTERACTION_REMOTE_MUTATION_TIMEOUT_S=120
LATE_INTERACTION_REMOTE_MAX_DOCUMENTS=1
LATE_INTERACTION_REMOTE_SYNC_BATCH_SIZE=1
LATE_INTERACTION_REMOTE_SOURCE_PAGE_SIZE=256
```

Registration alone does not authorize retrieval changes. Keep
`LATE_INTERACTION_ENABLED`, dual write, shadow, rerank and canary disabled until
their respective data, latency and quality gates pass. The non-loopback opt-in
also relaxes the legacy provider URL guard, so keep that legacy URL loopback-only.

Runtime settings and `deploy/server_up.sh` require
`LATE_INTERACTION_EXPERIMENT_AUTHORIZED=true` before dual-write/shadow and a
separate `LATE_INTERACTION_PRODUCTION_GATES_PASSED=true` before any live
rerank/canary. Active authorization must match the exact repository, build SHA,
baked source digest, model revision and approved index revision; traffic
approval also requires a real evidence-bundle path and SHA-256. Neither
authorization is needed for this inert provider or its offline sync.

After a complete sync, pin the sidecar itself:

```env
LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID=EXACT_REPOSITORY_ID
LATE_INTERACTION_SERVICE_MINIMUM_INDEX_REVISION=EXACT_VERIFIED_RN
LATE_INTERACTION_SERVICE_MINIMUM_DOCUMENT_COUNT=EXACT_VERIFIED_COUNT
```

The sidecar then rejects other repositories and keeps status, manifest reads
and rerank fail-closed below the revision/document floors even when a caller
omits those preconditions. Mutations remain available for an authoritative
rebuild from `r0`; an exact mutation revision is still enforced as CAS. Its
status returns a persistent `lineage_id` plus a deterministic SHA-256 over
sorted `chunk_id/path/content_hash` identities. Brain binds active approval to
that lineage, identity digest and document count.

The CPU service rejects batches larger than one document. Brain likewise
writes one document per request and uses the
separate 120-second mutation timeout. The 2-second retrieval timeout remains
unchanged:

```sh
brain embeddings late-remote-sync --repo /app --batch-size 1 --max-chunks 25 --json
brain embeddings late-remote-sync --repo /app --batch-size 1 --max-chunks 25 --write --json
```

Do not run an unbounded sync or prune until the bounded write completes and the
service returns to ready state. The service rejects multi-document CPU
mutations before they can occupy the only execution permit.

The source scan reads Postgres in pages (default 256) independently of the
one-document mutation batch. Before a resumed write it fetches the remote
`chunk_id/path/content_hash` manifest and skips exact matches client-side; the
service repeats the same identity check as a second barrier. Path-only renames
are updates, not unchanged documents. The final reconciliation repairs entries
removed after the initial prefilter. Prune is refused while late-interaction
traffic or dual-write/shadow is active and rechecks Postgres immediately before
each bounded delete. A `--max-chunks` write reports
`verification_mode=bounded_subset`; its source and remote digests cover only
the eligible rows scanned in that invocation, so it proves the canary/resume
batch rather than corpus readiness. An unbounded write reports
`verification_mode=exact` after a fresh full Postgres scan and fails unless the
complete authoritative and sidecar inventories are identical. Stale sidecar
IDs therefore require a safe offline prune before exact proof can pass.

Do not raise llama.cpp parallelism or combine documents into one embedding
request on the current VPS. Measurements on 2026-07-29 found that one
512-token document took 10.7 s, two concurrent documents took 33.3 s total,
and one-request batches were likewise slower per document. The measured
single-slot defaults are intentional.

The production defaults cap llama.cpp at two CPU threads/two cores and the
matrix service at one core. A four-core encoder completed the initial corpus
sync faster but starved SSH and other Brain services under sustained load.
The lower cap does not change the pinned BF16 model or MaxSim results; it only
makes background maintenance slower and leaves capacity for the online path.
Container health checks process and encoder liveness only. Corpus readiness is
reported separately by `/ready` and `/v1/index/status`, so a deliberate
identity refresh cannot falsely mark the running service unhealthy.

The official PyLate/PyTorch CPU path was measured as a reference rather than
assumed faster. With PyLate 1.4.0, PyTorch 2.7.1+cpu and four threads it took
43.47 s for one representative document and 23.93 s/document in a batch of
four while using about 1.8 GiB RAM. The temporary benchmark image and cache
were removed; llama.cpp BF16 remains the measured CPU default.

Before any Brain flag is enabled, run runtime parity against the PyLate BF16
reference, complete repository sync without prune first, run the paired RU/EN
evaluation, and verify the production latency gates under representative load.

Rollback without deleting model or matrix data:

```sh
docker compose stop service llama
```
