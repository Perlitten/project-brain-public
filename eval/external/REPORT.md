# Project Brain — external retrieval benchmark

Independent black-box evaluation of Project Brain's code-context retrieval against
public benchmarks and cheap baselines. Protocol: `PROTOCOL.md` + `PROTOCOL_AMENDMENTS.md`
(frozen before any scoring). Raw rows: `results/*.jsonl`. Harness: `harness/`.

## TL;DR

TBD — filled after all runs complete.

## Setup

- Branch: `devin/1791322327-external-retrieval-eval` (all artifacts under `eval/external/`)
- DB: dedicated `brain_db_eval` Postgres+pgvector (prod `brain_db` untouched); Neo4j 5.26 local; Redis up
- Embedder: `BAAI/bge-small-en-v1.5` (384-dim, fastembed ONNX, local). Prod default
  `jina-code` needed a remote model that ran at 0.57 chunks/s on this box — switched
  before any test-set scoring (Amendment A2). Same embedder used for all systems.
- Seed: 20261006 everywhere. Bootstrap CIs: percentile, B=10000.
- LLM key: none available → Phase 5 (end-to-end fix rate) SKIPPED, as specified.

## Results

### SWE-bench Lite — file localization (issue → gold files)

TBD.

### CoIR (cosqa, codetrans-dl, stackoverflow-qa capped at 500 queries)

TBD.

### RepoBench-R (python, cross-file completion)

TBD (bm25 acc1 0.126 / acc5 0.566; dense 0.113/0.558; brain_rrf 0.124/0.563, n=3027 — preliminary).

### Cost / latency

TBD — index time, index size, p50/p95 query latency, tokens of returned context,
recall-per-1k-tokens.

## Comparison to published numbers

Not comparable: no published leaderboard uses this embedder, this corpus snapshot
indexing scheme, or file-localization metrics on these exact subsets. CoIR published
numbers use different embedding models and full corpus runs; we report internal
comparisons only.

## Failure analysis — 10 concrete lost cases

TBD.

## Top-5 ranked improvements

TBD.

## Reproduction

```bash
cd /home/ubuntu/repos/project-brain-public
source eval/external/harness/env.sh          # sets DATABASE_URL to brain_db_eval, embed shim env
python eval/external/harness/embed_server.py # 127.0.0.1:18099, bge-small-en-v1.5
python eval/external/harness/run_swebench.py --subset stratified60 --index-once --out results/swebench.jsonl
python eval/external/harness/run_coir.py --dataset CoIR-Retrieval/cosqa --out results/coir_cosqa.jsonl
python eval/external/harness/run_repobench.py --out results/repobench.jsonl
python eval/external/harness/score.py "results/*.jsonl"
```

## Environment versions

TBD — pip freeze snapshot + git SHAs recorded in results/env.txt
