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
