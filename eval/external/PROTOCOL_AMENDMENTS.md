# Protocol amendments

All amendments are made **before the corresponding runs** and state their reason.
Amendments after a run that touch its instances would invalidate that run's numbers.

## A1 — 2026-10-06 21:45 UTC (before any run)

**Query formulation**: `ContextPackBuilder._classify_task` is deterministic by
default (`RETRIEVAL_USE_LLM_CLASSIFICATION=false` in shipped .env): it derives
`task_type` by keyword heuristics and `keywords` = dedup'd `extract_keywords(issue)`
with no cap. Therefore:

- `brain-v5`/`brain-v6` use the **prod** query understanding: `task_type` and
  `keywords` come from `ContextPackBuilder()._classify_task(issue_text)` — not a
  frozen surrogate. This is identical to what `/context` packs compute.
- `brain-mockllm` is redundant (the prod formulation is already LLM-free) → arm
  dropped; the gap is already disclosed under "Known gaps".

**nolex mechanics** (§1): implemented by patching `pipeline.expand_keywords`
and `pipeline.derive_path_hints` to return `[]` for the arm's run — removes
lexical/symbol/hints channels and keyword-path rerank bonuses in one stroke.

**grep baseline** (§1): one invocation `rg -i -c --fixed-strings -e kw1 ... -e kwN`
(keywords = prod `_classify_task` keywords, capped at 40 for runtime); file score =
summed match count across keywords; ties by path. Keywords >200 chars excluded.

**bm25 tokenizer** (§1): `re.findall(r"\w+", text.lower())` on chunk content and
on issue text; file score = max chunk BM25 score; top-30 retained.

**file_limit for v5**: `file_limit=30` keeps the v5 path (`RETRIEVAL_V6_ENABLED`
is False in shipped defaults; the 30-item `selected_paths` is the "deep" pack
budget). `brain-v6` repeats the run with the flag toggled to True in-process.

## A2 — embedder switch (2026-10-06, before any test-set scoring)

`jinaai/jina-embeddings-v2-base-code` (768-dim, ~137M params) measured
0.57 chunks/s on this 8-core box → ~7 h for django's first index alone:
infeasible within the budget. Switched the openai-compatible shim to
`BAAI/bge-small-en-v1.5` (384-dim) BEFORE any stratified-60/300 test data
was produced (only dev-fold smoke ran, now wiped). `EMBEDDING_DIMENSION=384`,
eval DB and embedding cache recreated. All systems (Brain arms AND the
dense/BM25 baselines) share this same embedder, so comparisons remain
paired; absolute recall is expected lower than a code-specialized embedder
would give — a disclosed caveat, same class as A1.

## A3 — CoIR corpus loading (before any CoIR test queries run)

CoIR corpora are snippet collections, not repositories. Indexing 20k
synthetic files through FileIndexer costs ~2.5 h/subset of pure
AST/symbol overhead with zero retrieval benefit (snippets are
function-level; there is no repo structure to extract). Instead the
corpus is bulk-loaded: one File + one FileChunk + one Embedding row per
snippet via `build_embedding_record` — the same record builder the
indexer uses. Text channels (lexical ILIKE, vector, hints, rerank
features derived from content) behave identically to a real index;
symbol/graph/memory channels contribute nothing, which matches what a
snippet-level corpus contains anyway. grep baseline runs on
materialized files under /home/ubuntu/eval_external/coir_corpus/.

## A4 — RepoBench-R scoring

RepoBench-R (tianyang/repobench-r, files python_cff.gz/python_cfr.gz are
gzipped pickles: dict test->{easy,hard}->8000 items) gives each instance
its own candidate pool (`context` list) with `golden_snippet_index`.
Per-instance repos are not indexed (8000 distinct repos, infeasible);
arms = bm25, dense (same embedder), and brain-rerank (Brain's
deterministic feature scorer on candidate snippets). Query = `code` +
`import_statement`. Metric: Acc@k (gold snippet in top-k).

## A5 — SWE-bench single-snapshot index per repo (budget-driven)

Per-instance incremental re-indexing re-embedded 30-60% of files per
base_commit (measure: sympy instance 1 full index = 68 min; instance 2
re-index ~as long → 60 instances infeasible). Amended: each repo is
indexed ONCE at its earliest base_commit in the sampled set; all
instance queries for that repo run against that snapshot (and grep runs
on the same checkout). Bias: a gold file may not exist at the snapshot
commit — counted per row as `gold_at_snapshot` and reported; content
drift between commits is a small, disclosed approximation used by
comparable retrieval papers. `snapshot_commit`/`gold_at_snapshot` are
recorded in every row.

## A6 — dev tuning split (frozen 2026-10-07 before any tuning)

- Pool: `princeton-nlp/SWE-bench` test (2294) minus ALL `SWE-bench_Lite` (300) and
  `SWE-bench_Verified` (500) ids → 1587 candidates.
- Sample: stratified by repo (`max(2, round(150*n/N))` per repo, seed 20261006,
  same procedure as stratified60), n=150. Frozen id list: `splits/dev150_ids.txt`.
- Distribution: django 51, sympy 24, sklearn 17, mpl 13, sphinx 12, pytest 8,
  xarray 8, astropy 7, pylint 3, requests 3, seaborn 2, flask 2.
- Indexes: reuse the same per-repo snapshot indexes as the Lite runs (A5).
  9/150 rows have ≥1 gold file absent at snapshot (`gold_at_snapshot` flagged,
  unbiased across index-based arms; grep arm excluded from tuned comparisons
  anyway).
- Rule: ALL v2 tuning decisions (fusion weights, reranker gate, fallback,
  query-formulation, aggregation mode) are made on dev150 only. The Lite n=297
  test set is touched exactly once, after the config is frozen.
- Dev arms run: bm25, dense, brain_v5 (as shipped baseline), plus each candidate
  v2 variant; cheap-loser arms (grep, lex_only, novector) are skipped on dev to
  halve runtime — they are frozen losers on test.

## A7 — v2 tuning results on dev150 and frozen v2 config (pre-test freeze)

Tuned ONLY on dev150 (A6), three ablation rounds, n=150 each, arm-per-instance within one
row so paired tests are valid (results: results/score_dev150_v2.json, score_dev150_r23.json;
per-instance: results/dev150_all_arms.jsonl; paired stats: results/dev150_paired.json).
Test sets (Lite/Verified) untouched; the Lite run below is the first and only v2 test run.

Dev150 R@10 (95% CI):

| variant | R@10 | vs dense (paired diff [CI]) |
|---|---|---|
| dense (frozen baseline) | .5663 [.515,.616] | — |
| brain_v7_lin (linear, rerank on, all fixes) | .5654 [.494,.635] | −.0009 [−.035,+.034] |
| **brain_v7_lin_norank (frozen pick)** | .5680 [.498,.637] | +.0017 [−.018,+.021] |
| brain_v7_lin_chan0 (vec+bm25 only) | .5728 [.504,.643] | +.0065 [−.024,+.039] |
| brain_v7_lin_nobm25 | .5677 [.498,.637] | +.0013 [−.033,+.036] |
| brain_v7_lin_noidq | .5665 [.497,.635] | — |
| brain_v7_lin_hilex (lex/sym/hint w=0.6) | .5588 [.488,.629] | — |
| brain_v7_lin_topk | .5489 [.480,.618] | — |
| brain_v7_lin_sum | .4670 [.396,.538] | — |
| brain_v7_full (wrrf, all fixes) | .2259 [.175,.282] | −.3396 [−.405,−.274] |
| brain_v7_norank (wrrf, no rerank) | .3859 [.327,.445] | — |
| brain_fused (v5 full) | .4047 [.355,.455] | — |
| brain_v5 | .2356 [.193,.282] | — |

Paired decisions on dev150:
- (a) fusion: linear over wrrf/rrf. lin_norank beats v5 +.332 [.249,.415] and brain_fused
  +.163 [.082,.243], both CI excl. 0. wrrf collapses (−.34 vs dense): rank-level fusion still
  dilutes the vector channel; score-level fusion does not.
- (b) reranker OFF: removing it gains +.159 [.075,.241] excl-0 under wrrf and is neutral
  under linear (+.0025). It never helped once.
- (c) bm25 fallback ON: dev-neutral (empty channels almost never occur on real repos);
  it exists for the CoIR snippet case. nobm25 vs lin: +.0022 [0,+.007].
- (d) idq ON: dev-neutral (+.0011 [0,+.003]).
- (e) vec agg = max: sum is −.098 [−.163,−.036] excl-0; topk is −.017 mean.
- Channel weights kept at defaults: hilex −.0067 and chan0 +.0074 vs lin are both noise.

FROZEN v2 config for the single test run (set as flag defaults in brain/config/settings.py):
RETRIEVAL_V2_FUSION=linear, RETRIEVAL_V2_RERANK_ENABLED=False,
RETRIEVAL_V2_BM25_FALLBACK=True, RETRIEVAL_V2_ID_QUERY=True, RETRIEVAL_V2_VEC_AGG=max,
linear weights w_vec=1.0 w_lex=0.35 w_sym=0.35 w_hint=0.45 w_graph=0.3 w_bm25=0.6.
RETRIEVAL_V2_ENABLED stays default-False until the test run decides per the protocol rule.
