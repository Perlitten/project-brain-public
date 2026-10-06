# External Retrieval Benchmark — Frozen Protocol

Commit: frozen before any benchmark run. Any change after this commit is an
amendment and must be listed in `PROTOCOL_AMENDMENTS.md` with timestamp and
reason; headline numbers always state the protocol revision they were
computed under.

Question under test: **is Project Brain top-tier at code-context retrieval for
coding agents?** Answer = retrieval quality on external benchmarks vs. cheap
baselines, plus cost/latency efficiency.

## 0. Honesty rules (binding)

1. No Project Brain setting may be tuned on any test set. All retrieval
   settings are the shipped defaults (`.env` committed defaults) with only the
   substitutions enumerated in §4 (embedder endpoint, batch sizes, DB targets).
   If any tuning happens, it happens on the dev fold only (§3.4) and is
   disclosed next to the headline numbers.
2. Every run is reported, including failed/degraded ones. `STATUS.md` logs all
   runs; `results/` keeps per-instance JSONL for all of them.
3. Every headline metric ships with a 95% bootstrap CI (percentile method,
   B = 10 000 resamples over instances, seed 20261006).
4. Comparable systems get the same embedder and the same reranker budget: none
   of the systems in this benchmark uses an LLM reranker.
5. Published leaderboard numbers are quoted only when dataset, split, metric,
   and candidate unit are identical; otherwise labelled *not comparable*.

## 1. Systems under test

| id | description |
|---|---|
| `brain-v5` | `HybridRetrievalPipeline.run`, production defaults, `retrieval_mode="fast"`, `file_limit=30` → `selected_paths` (v5 frozen path; `RETRIEVAL_V6_ENABLED=false` as shipped) |
| `brain-v6` | same, with `RETRIEVAL_V6_ENABLED=true` (shipped feature flag, second config arm) |
| `brain-mcp` | `RetrievalService.retrieve(intent="locator")` — the `search_code` MCP/API surface; file ranking = files channel order, then chunk-derived paths by vector order (secondary arm) |
| `bm25` | rank_bm25 over the **same** `file_chunks.content` rows in the same index DB; file score = max chunk score, ties by path |
| `dense` | Brain's own vector channel (`vector_search_chunks`, k=30, same embedder); file score = max chunk similarity (equivalently: pipeline's `vector` channel order) |
| `grep` | "plain agent grep": keywords = `brain.search.code_search.extract_keywords(issue)`; per keyword `rg -il --fixed-strings` on the worktree; file score = Σ matches per file, ties by path |
| `brain-fused` | pipeline output before rerank: `fused_ranking[:k]` (reranker-off ablation, exact, free) |
| `brain-novector` | full pipeline with `vector_search_chunks` patched to return empty (channel-off ablation) |
| `brain-nolex` | full pipeline with the lexical+symbol+hints channel block patched to empty (approximates dense+rest) |
| `brain-nograph` | full pipeline without Neo4j (if Neo4j runs, this = Neo4j stopped; if it never runs, `brain-v5`/`brain-v6` are effectively this and the arm is marked N/A) |
| `brain-mockllm` | prod context-pack query formulation: `ContextPackBuilder._classify_task(issue)` under `DEFAULT_LLM_PROVIDER=mock`, then same pipeline. Shows what prod query understanding adds vs. frozen keywords |

Ablation methodology note: channel-off arms reuse the same index and run the
real pipeline with the channel call patched to empty; `brain-fused` is the
pre-rerank order of the same run. Any additional arm not listed here requires a
protocol amendment.

## 2. Retrieval entry point (Phase 0 inventory)

- Flagship path: `brain/retrieval/pipeline.py::HybridRetrievalPipeline.run`
  (drives `/context` packs). Channels: lexical ILIKE on `File.path/summary`,
  symbol ILIKE, path hints, pgvector dense, Neo4j graph (only DEPENDENCY /
  CROSS_SURFACE routes), memory cache, card channel (off by default).
  RRF (k=60) → deterministic rerank → `selected_paths`.
- Query formulation (frozen): `task_type="bugfix"`,
  `keywords=extract_keywords(issue_text)`, `file_limit=30`,
  `retrieval_mode="fast"`. `brain-mockllm` replaces these two args with the
  mock classifier output.
- Late-interaction (ColBERT/LFM) arms: all `LATE_INTERACTION_*` flags are off
  in shipped `.env` → not benchmarked, disclosed as a gap.

## 3. Datasets and splits

### 3.1 SWE-bench file localization (main result)

- `princeton-nlp/SWE-bench_Lite` test split, all 300 instances.
- `princeton-nlp/SWE-bench_Verified` test split (500) — only if time remains
  after Lite; decided at the Phase-1 checkpoint, logged in STATUS.md.
- Per instance: checkout repo at `base_commit`, run incremental `brain index`
  on the worktree (unchanged files skipped by content hash), query with the
  full `problem_statement`, score retrieved files against **all files touched
  by `patch`** (gold patch file set).
- Instance order within a repo: ascending committer date of `base_commit`
  (minimizes incremental re-embedding, no test information used).
- **Subsetting**: first run a stratified subset of 60 — proportional per repo
  (min 2/repo), inside each repo selected uniformly at random (seed 20261006)
  then evaluated in commit-date order. Extension to full Lite is decided by
  measured index/query throughput and logged in STATUS.md.
- Repos cloned once to `$EVAL_DIR/repos/<repo>` and reused across commits.

### 3.2 CoIR (Phase 2)

Priority order (fit as many as time allows, in this order):
1. `CoIR-Retrieval/cosqa` (text→code)
2. `CoIR-Retrieval/stackoverflow-qa` (text→code)
3. `CoIR-Retrieval/codesearchnet` (text→code; use the benchmark's packaged
   corpus, not a re-crawl)
4. `CoIR-Retrieval/codetrans-dl` (code→code)

Corpus items are indexed through the same `FileIndexer` write path where the
unit is a file (each corpus doc becomes one pseudo-file so chunking/embedding
are identical to Brain usage); queries = the packaged query set.
Metrics: nDCG@10 (headline), plus MRR@10 and Recall@100 where the harness
supports the pool. If a subset cannot be ingested in time it is reported as
*not run* with the reason — never silently dropped.

### 3.3 RepoBench-R (Phase 2)

- `tianyang/repobench-r`, python, the packaged eval split.
- Query = provided snippet (`next_line`-style context field as packaged);
  candidates = the packaged candidate files/cross-file pool per instance.
- Metric: benchmark-defined accuracy@k (Acc@3 plus top-1/5/10 for readability —
  exact field mapping recorded in the run README once the format is confirmed).

### 3.4 Dev fold (sanity/debug only)

3 SWE-bench Lite instances (django__django-11019, sympy__sympy-13647,
psf__requests-2317 — fixed list, chosen before data inspection) are a dev fold:
used for harness smoke tests and debugging. They are excluded from all
reported subsets and headline numbers. No tuning on test sets; anything tuned
on the dev fold is disclosed.

## 4. Configuration (the only allowed deviations from shipped defaults)

- `DATABASE_URL` → `postgresql://postgres:postgres_password@localhost:5433/brain_db_eval`
  (dedicated scratch DB; prod `brain_db` untouched; DB created by
  `scripts/check_migrations.py` + `create_all`, same as `isolated_bench_db`).
- Embedder: prod default `nvidia/nv-embedcode-7b-v1` needs a paid API key —
  none available → local open model substitute: **`jinaai/jina-embeddings-v2-base-code`**
  (161M, 768-dim, 8192 ctx, Apache-2.0; fastembed/ONNX on 8 CPU cores), served
  behind an OpenAI-compatible shim `eval/external/harness/embed_server.py` on
  `127.0.0.1:18099`; Brain sees `DEFAULT_EMBEDDING_PROVIDER=openai_compatible`,
  `EMBEDDING_BASE_URL=http://127.0.0.1:18099/v1`, `EMBEDDING_MODEL=jina-code`,
  `EMBEDDING_DIMENSION=768`, `INDEX_EMBEDDING_BATCH_SIZE=64`.
  The shim adds a persistent disk cache keyed by sha256(text) → identical
  contents are embedded once ever (performance cache only; identical vectors
  served to Brain and to baselines). Passage/query `input_type` is ignored —
  jina-v2 uses no prefixes — disclosed.
- `DEFAULT_LLM_PROVIDER=mock` (no LLM key in env) → file summaries are mock
  stubs during indexing (lexical channel degraded vs. prod). Disclosed on every
  affected result. Phase 5 skipped for the same reason — stated explicitly in
  REPORT.md.
- Neo4j: attempted as a local install (timeboxed); if unavailable, the graph
  channel silently contributes nothing (it already fails open per keyword) and
  every Brain arm is labelled `no-graph`.
- Rerank cache: filesystem `.cache/rerank` under the eval workdir (prod
  behaviour); latency measurements use cold-cache runs only.
- Seed for subset selection and bootstrap: 20261006.
- `ENVIRONMENT` stays unset (dev default); `ALLOW_SCRATCH_REPO_ROOTS` default.
- No changes to Brain source code, ranking constants, prompts, or exclusion
  lists. Monkeypatching for ablation arms happens inside `eval/external/` only.

## 5. Metrics

Per instance i with gold file set G_i and ranked list R_i:

- Recall@k = |R_i[:k] ∩ G_i| / |G_i|, k ∈ {1,5,10,30} (30 only where the system
  emits ≥30 items)
- MRR@10 = 1/rank of first gold hit (0 if none in top 10)
- nDCG@10 with binary gains
- Symbol recall: gold hunk line ranges → symbols whose [start,end] overlap on
  the same file, in the index at base_commit; SymbolRecall@k = fraction of gold
  symbols inside retrieved files' top-k.
- Chunk recall@k over `file_chunks` (secondary diagnostic).
- CoIR: nDCG@10 headline (their canonical metric), MRR@10, Recall@100.
- RepoBench-R: Acc@k as packaged.
- Phase 4: index wall time/repo, index DB size (pg_total_relation_size),
  p50/p95 cold-query latency, tiktoken(cl100k) tokens of top-10 file contents,
  recall-per-1k-tokens curve.

Aggregation: macro-average over instances; 95% CI = percentile bootstrap
(B=10 000, seed 20261006) over the instance set.

## 6. Checkpointing

After each phase: commit results JSONL + `STATUS.md` (done/next/exact resume
command) and push the branch. STATUS.md also lists every run attempt including
bad ones.

## 7. Known gaps, declared up front

- No real LLM: file summaries are stubs (weakens lexical channel and
  `brain-mockllm` query understanding vs. prod), Phase 5 skipped.
- Local CPU embedder substitutes the prod NVIDIA model — dense channel quality
  differs from prod in both directions; identical for all dense systems here.
- `.json` files are never indexed by Brain (should_ignore_file) — CoIR corpora
  must map docs to non-.json pseudo-paths; SWE-bench gold files are .py so this
  mainly bounds CoIR comparability.
- Index reuse across commits is incremental by file content hash — this mirrors
  prod re-index semantics but means per-instance state reflects prior commits'
  unembedded files being skipped. Embedding cache means re-embeds are cache hits.
