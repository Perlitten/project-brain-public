# Project Brain — external retrieval benchmark

Independent black-box evaluation of Project Brain's code-context retrieval against
public benchmarks and cheap baselines. Protocol frozen before any scoring:
`PROTOCOL.md` + `PROTOCOL_AMENDMENTS.md` (A1–A5). Raw per-instance rows:
`results/*.jsonl`. Harness: `harness/`.

## TL;DR

- **Project Brain is not top-tier for code-context retrieval today.** Its best
  surface (the MCP locator, `brain_mcp`, R@10 .505) ties the trivial dense
  baseline (R@10 .515, CIs overlap) and the fused RRF pipeline (.458) only beats
  BM25 (.411) — while the flagship pipelines `brain_v5`/`brain_v6` score
  **below** plain BM25 (.259/.303 vs .411 on SWE-bench Lite, n=297).
- **Where it wins:** context economy — `brain_fused` returns 47k tokens per
  top-10 (vs 85–97k for dense/bm25), the best recall-per-1k-tokens of any arm
  (.0153, 1.8× dense). If your bottleneck is context window, Brain's chunk
  aggregation is genuinely efficient.
- **Where it loses:** snippet-level retrieval is a blowout — BM25 beats every
  Brain arm ~2–3× on CoIR codetrans-dl (.310 vs .112 nDCG@10) and
  stackoverflow-qa (.606 vs .266), because Brain's lexical/hints channels are
  path- and symbol-based and produce nothing on anonymous snippet pools.
- **The extra machinery hurts more than it helps on file localization:**
  removing channels strictly helps (brain_nolex .448 > v5 .259); the vector
  channel alone (dense) beats the full fusion; hints/graph/cache + path-feature
  reranking are net-negative on issue→file tasks.
- Verdict: **competitive cost-wise (token efficiency), mid-pack accuracy** —
  ahead of BM25 at file level, behind a plain single-embedding dense index,
  far behind BM25 on snippet pools. Not top-tier.

- **v2 update (see "v2 retrieval" below):** a tuned retrieval stack
  (linear fusion, reranker off, BM25 fallback, identifier query) was
  frozen on a disjoint dev150 split and run once on Lite. Result:
  **dead tie with dense (.5152 = .5152)** — not a win — and it regresses
  the token-economy edge by 41% (cap was 10%). `RETRIEVAL_V2_ENABLED`
  therefore ships **default-off**.

## Setup (all frozen in PROTOCOL.md before runs)

- Branch `devin/1791322327-external-retrieval-eval`; everything under `eval/external/`
- Dedicated eval DB `brain_db_eval` (prod untouched); Neo4j 5.26 + Redis up
- Embedder: `BAAI/bge-small-en-v1.5` (384-d, fastembed ONNX, local ~200 txt/s).
  Prod default jina-code ran at 0.57 chunks/s here → switched before any
  test-set scoring (A2). **Same embedder for every system that uses vectors.**
- Seed 20261006; bootstrap percentile CIs, B=10 000. No tuning on test folds;
  dev fold {django-11019, sympy-13647, requests-2317} excluded from all scored
  subsets.
- SWE-bench indexing caveat (A5): one snapshot index per repo at the earliest
  sampled `base_commit`; rows carry `snapshot_commit` + `gold_at_snapshot`.
  (See "Known biases".)

## Phase 1 — SWE-bench Lite file localization (issue → gold-patch files)

n = **297/300** (all of Lite minus the 3 dev-fold instances; 0 errors).

| arm | R@1 | R@5 | R@10 [95% CI] | MRR@10 | nDCG@10 | symR@10 | tok@10 | recall/1k-tok | p50/p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| **dense (bge-small)** | .239 | .458 | **.515** [.458,.572] | .324 | .370 | .552 | 85k | .0086 | — |
| **brain_mcp** (MCP locator) | .209 | .377 | **.505** [.448,.562] | .291 | .341 | .536 | 65k | .0112 | 228/615 |
| brain_fused (RRF fusion) | .182 | .370 | .458 [.401,.515] | .264 | .310 | .492 | **47k** | **.0153** | — |
| brain_nolex (no lexical) | .148 | .327 | .448 [.391,.505] | .225 | .277 | .484 | 86k | .0074 | 493/1336 |
| **bm25** (rank_bm25) | .192 | .330 | .411 [.353,.468] | .251 | .288 | .429 | 89k | .0062 | 2868/7172 |
| brain_v6 | .040 | .205 | .303 [.253,.357] | .111 | .156 | .325 | 50k | .0083 | 1734/10684 |
| brain_v5 | .037 | .162 | .259 [.209,.310] | .089 | .128 | .274 | 43k | .0080 | 5883/20105 |
| grep (ripgrep keywords) | .007 | .071 | .155 [.115,.199] | .040 | .067 | .171 | 397k | .0006 | 176/517 |
| brain_novector (no vector) | .010 | .071 | .118 [.081,.155] | .035 | .055 | .115 | 34k | .0044 | 1554/10356 |
| lex_only_channel | .007 | .020 | .030 [.013,.051] | .013 | .017 | .036 | 18k | .0022 | — |

Reads:

- `brain_mcp` is Brain's best arm and is statistically tied with dense at
  R@10 (CIs overlap), but loses on R@1/R@5 (dense .239/.458 vs .209/.377) —
  dense reaches gold earlier in the list.
- `brain_fused` = Brain RRF over channels. It beats BM25 (+4.7pp R@10,
  CI [0.40,0.52] vs [0.35,0.47] — overlapping, not decisive) and has the best
  token efficiency, but never catches plain dense.
- `brain_v5`/`brain_v6` (the product's full pipelines incl. hints, graph,
  cache, deterministic rerank) are the worst non-trivial arms: **worse than
  BM25 by 15–17pp**. Ablations say why: removing the lexical channel doesn't
  rescue them (nolex .448 still > v5), removing the vector channel is fatal
  (.118) — i.e. **the vector channel carries nearly all the value and the
  product pipeline actively damages its output**.
- stratified-60 sub-sample gave the same ordering (mcp .667 > dense .583 >
  fused .533 > bm25 .467 > v5 .333); the full-297 run is the headline.
- Latency: MCP locator p50 228ms; v6 p50 1.7s; v5 p50 5.9s / p95 20s —
  measured under eval-environment embed contention (pgvector search itself is
  ~4ms warm; see "cost"). grep is 176ms but useless (.155).

## Phase 2a — CoIR (anonymous snippet corpora)

nDCG@10 [95% CI], query counts: cosqa 500/500, codetrans-dl 195/195,
stackoverflow-qa 500 (capped from 1994, disclosed in protocol).

| dataset | bm25 | dense | brain_fused | brain_v5 | brain_v6 |
|---|---|---|---|---|---|
| cosqa | **.113** [.092,.135] | .095 [.074,.117] | .095 | .095 | .095 |
| codetrans-dl | **.310** [.282,.339] | .112 [.084,.141] | .112 | .121 | .110 |
| so-qa (500) | **.606** [.566,.646] | .266 [.230,.304] | .266 | .266 | .266 |

- Every Brain arm collapses to the dense score (identical rows) — on anonymous
  `s_NNNNNN.py` files the lexical, hints, graph and cache channels emit
  nothing, so "fusion" is vector-only.
- BM25 over chunk text wins by 2–3× on dl/soqa. These corpora are
  function/answer snippets where literal token overlap is strong; Brain's
  channel design is aimed at repo-shaped corpora and does not transfer.
- Not comparable to published CoIR numbers (different embedder/corpus caps;
  internal baselines only).

## Phase 2b — RepoBench-R python (cross-file snippet ranking)

n = 3027 (cff 1527 + cfr 1500). Query = context code + imports; candidates =
repo snippets. Acc@k:

| arm | Acc@1 | Acc@3 | Acc@5 |
|---|---|---|---|
| bm25 | .126 | .345 | .566 |
| dense | .113 | .341 | .558 |
| brain_rrf (bm25+dense, Brain weights) | .124 | .357 | .563 |

- Hard task for everyone (best Acc@1 ≈ .12). Brain's RRF fusion weights
  (lex 1.25, vec 1.0, k=60) add nothing over bm25 (.124 vs .126 — inside noise).
- The Brain-specific channels can't run here either (no index, no symbols).

## Phase 3 — ablations verdict

| signal | evidence |
|---|---|
| Vector channel | carries nearly everything (novector .118 R@10; dense .515) |
| Lexical channel | weak on real repos (lex_only .030) and dead on snippets |
| Fusion (RRF) | ≈bm25 on repobench; <dense on swebench (dilutes vector #1 hits) |
| Rerank + hints + graph + cache | net-negative: v5 .259 < nolex .448 < fused .458 < dense .515 |
| MCP locator | the one product surface that matches dense — best Brain arm |
| Context packing | strongest selling point: 47k tok@10 at .458 recall (eff .0153) |

## Phase 4 — cost / latency

Indexing (snapshot, incremental-capable; embed-bound at ~200 texts/s):
sympy 4080s/1369 files, sklearn 1051s/1089, sphinx 1034s/1265, pylint 541s/2051,
xarray 373s/242, pytest 370s/424, seaborn 208s/243, flask 119s/222,
django ≈2100s total/4721 (completed in two passes after a killed run).
Rate ≈ 1–3 files/s — index cost is ~99% embedding; pgvector insert + ANN search
is ~4ms warm. Eval DB ≈ 2.7 GB for 12 repos + 3 snippet corpora (~95k files).
Query latency: see table above. Token accounting: cl100k via `git show
<snapshot_commit>:<path>` on top-10 hits (scorer code in `score.py`).

## Phase 5 — end-to-end fix rate

**Skipped as specified** — no LLM API key available in the environment
(checked `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`NVIDIA_API_KEY`/`LLM_API_KEY`:
all unset or placeholder-only).

## Comparison to published numbers

None are metric-and-setting-identical (different embedder, corpora, snapshot
indexing, and file-level gold), so per the protocol we quote **internal
baselines only**; leaderboard numbers are deliberately not cited.

## Failure analysis — concrete lost cases (brain_fused misses @10 that a baseline hits)

| instance | gold file | fused rank | winner (rank) | why it lost |
|---|---|---|---|---|
| django-12908 | db/models/query.py | >30 | dense@1 | vector #1 drowned: 4 channels each push own top-k; RRF w=1.0 can't overcome lexical+hints mass |
| django-11742 | db/models/fields/__init__.py | >30 | dense@3 | same — semantic-only hit |
| sympy-12419 | matrices/expressions/matexpr.py | >30 | dense@2, mcp@2 | semantic gap, no token overlap for lexical/hints |
| mpl-23562 | mplot3d/art3d.py | >30 | dense@4, mcp@2 | dense neighbors clustered correctly; fused promoted test files instead |
| pytest-7490 | _pytest/skipping.py | >30 | dense@1 | vector hit lost in fusion |
| requests-863 | models.py | >30 | dense@1, mcp@2 | tiny repo; fusion still diluted vector #1 |
| django-16041 | forms/formsets.py | >30 | mcp@1, dense@1, bm25@3 | channels disagreed, fused picked template/test files |
| sphinx-8282 | ext/autodoc/__init__.py | 26 | dense@2, mcp@2 | near-miss — just below cutoff after rerank |
| pytest-5221 | _pytest/python.py | 27 | dense@3 | near-miss |
| django-15498 | views/static.py | >30 | bm25@2 | rare pure-lexical win where Brain lexical channel still missed (path-term weighting) |

Dominant cause (8/10): **RRF dilution** — a file that is vector-channel #1
gets only w/(k+1)≈0.016 mass, comparable to each other channel's own #1;
wrong-but-lexically-plausible files (tests, templates, sibling modules)
accumulate more total mass. Secondary (2/10): near-cutoff misses after
deterministic rerank.

## Ranked improvements (expected gain on swebench R@10)

1. **Fix channel aggregation or drop dead channels on retrieval.** Dense alone
   gains +5.7pp over fused, +25pp over v5 — the fusion/rerank stack is where
   the loss is born. Est. +10–25pp if full pipeline just returned vector+rRF
   sans path-bias rerank.
2. **Expose the MCP-locator ranking as the default** — it's the only arm at
   dense level (.505); whatever it does differently (intent classification +
   candidate budgeting) should be the v5 path. Est. +5pp, free.
3. **Content-lexical channel for path-less corpora** — BM25 beats Brain ~3×
   on snippet pools because the lexical channel reads path/symbol fields, not
   text. Est. +0.15–0.30 nDCG on CoIR-type workloads.
4. **Dedup/boost test-file policy** — several losses are tests/templates
   outranking source files; a configurable source-vs-test prior (not a
   hard-coded one) would recover near-misses like sphinx-8282, pytest-5221.
   Est. +2–4pp.
5. **Cache/latency hardening** — v5 p50 5.9s is not competitive for agent
   inner loops; most of it is serial channel waits + embed queue, since
   pgvector is 4ms. Concurrency fix + warm embedding path. Est. latency only.

## v2 retrieval (STEP 1–3, amendments A6–A7)

Question: can Brain's ranking beat the dense baseline on file localization
while keeping the token-economy edge — **without tuning on test data**?

**Method.** dev150 (A6): 150 SWE-bench-test instances disjoint from Lite and
Verified, stratified by repo (django 51, sympy 24, sklearn 17, mpl 13, sphinx
12, pytest 8, xarray 8, astropy 7, pylint 3, requests 3, seaborn 2, flask 2),
frozen before any tuning. Five fixes, each its own commit behind
`RETRIEVAL_V2_*` flags, ablated on dev150 only (23 variant arms × n=150):

- (a) score-normalized **linear fusion** (replaces flat RRF / weighted RRF)
- (b) reranker gate — **off** (it was +.159 R@10 excl-0 to REMOVE it under
  wrrf; neutral under linear)
- (c) empty-channel content-**BM25 fallback** (anonymous-corpus case)
- (d) identifier/path/stack-frame/error **query extraction**
- (e) chunk→file vector aggregation: **max** (sum −.098 excl-0; topk −.017)

Dev150 headline: linear fusion `.565–.573` vs dense `.566` — tie; beats the
v5 fused stack +.163 [.082,.243] excl-0 and every wrrf/rrf variant by ≥.16.
Frozen config (A7): `linear` + rerank off + bm25-fallback + idq + max-agg.

**Test — one run, Lite n=297, same harness as the frozen numbers:**

| arm | R@10 [95% CI] | MRR | tok@10 | recall/1k tok |
|---|---|---|---|---|
| dense (frozen) | .5152 [.458,.572] | .324 | 84 945 | .0086 |
| **brain_v7 (frozen v2)** | **.5152 [.458,.572]** | .322 | **81 930** | **.0090** |
| brain_mcp | .5051 [.448,.562] | .291 | 65 271 | .0112 |
| brain_fused | .4579 [.401,.515] | .264 | 46 634 | .0153 |
| bm25 | .4108 [.353,.468] | .251 | 88 754 | .0062 |
| brain_v6 | .3030 [.253,.357] | .111 | 50 377 | .0083 |
| brain_v5 | .2593 [.209,.310] | .089 | 42 557 | .0080 |

Paired vs dense: diff **0.000** [−.017,+.017] — 3 wins / 3 losses / 291 ties.
Paired vs brain_fused: +.0572 [−.0034,+.1178] (directional, not significant).
Paired vs bm25: +.1044 [.037,.172] excl-0. Paired vs mcp: +.010 (noise).

**Token economy.** v7 returns ~82k tokens top-10 (dense 85k, fused 47k):
eff .0090 vs fused's .0153 = **−41% regression**, over the 10% cap — the
fused arm's edge came precisely from returning about half the tokens.

**CoIR snippet corpora (n=1180, same queries as the frozen run):**
bm25 .5008 > **brain_v7 .4008** > brain_v5 .2373 = brain_fused .2364 = dense
.2364. Paired: v7 +.164 [.142,.186] excl-0 over v5/fused/dense — the
BM25-fallback + identifier query fixes are real and large on anonymous
corpora — but still −.100 [−.129,−.070] excl-0 **below plain bm25**.

**RepoBench-R (n=3000):** bm25 .1263, brain_rrf .1240, brain_v7 .1163,
dense .1133 (Acc@1) — all CIs overlap; v2 ≈ dense, unchanged picture.

**5-line verdict:**
1. On the single frozen test run, brain_v7 ties dense **exactly**
   (.5152 = .5152, paired diff 0.0) — Brain does **not** beat dense.
2. vs its own stack: +.057 over fused (CI crosses 0) and +.104 over bm25
   (excl-0) — the repairs help, just not enough to win.
3. The token-economy edge regresses 41% vs fused's recall/1k-tok —
   violating the ≤10% cap.
4. On snippet corpora v2 is a genuine fix (+.164 excl-0 over v5) but still
   loses to plain bm25 (−.100 excl-0).
5. `RETRIEVAL_V2_ENABLED` ships **default-off** per protocol. What the
   evidence actually says: "dense channel + light lexical backup ≈ dense" —
   the multi-channel machinery contributes ~nothing measurable on real
   issues (channels flipped the outcome on only 6 of 297 instances,
   3 each way) and still loses where it was meant to matter most.

## Known biases / limitations (all disclosed in PROTOCOL_AMENDMENTS)

- Snapshot indexing: one index per repo at earliest sampled commit; files
  created after it are unretrievable for their own issues
  (`gold_at_snapshot` recorded per row). Rate is low on Lite.
- Sympy's index contains a 2-commit union (pre-A5 run) — negligible pool
  inflation, disclosed.
- so-qa queries capped at 500/1994; codetrans-dl at 195.
- Query latency measured under eval-VM embed contention; absolute ms are
  environment-bound (relative ordering across arms remains valid).
- Embedder is bge-small-384, not a code-specialized model — absolute numbers
  would shift with a stronger one; the arm-vs-arm ordering is the claim.

## Reproduction

```bash
cd project-brain-public
source eval/external/harness/env.sh
python eval/external/harness/embed_server.py &          # 127.0.0.1:18099
python eval/external/harness/run_swebench.py --subset all --index-once \
    --repos <repo> --out results/swebench_all.jsonl
python eval/external/harness/run_coir.py --dataset CoIR-Retrieval/<ds> --out results/coir_<ds>.jsonl
python eval/external/harness/run_repobench.py --out results/repobench.jsonl
python eval/external/harness/score.py "results/*.jsonl" --json results/score.json
```

Env versions: `results/env.txt` (py3.12.13, fastembed 0.8.1, onnxruntime
1.30.0, rank-bm25 0.2.2, datasets 5.1.0, tiktoken 0.14.0; repo@de925a4).
