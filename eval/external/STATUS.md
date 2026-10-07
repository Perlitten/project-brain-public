# STATUS — external retrieval eval

## v1 (COMPLETE — merged as PR #15)

All phases done. Final artifacts in this directory.

### Results
- SWE-bench Lite n=297 (all minus dev fold, 0 errors): results/swebench_all.jsonl + score_swebench_all.json
- CoIR: cosqa 500, codetrans-dl 195, soqa 500-capped: results/coir_*.jsonl + score_coir_*.json
- RepoBench-R n=3027: results/repobench_*.jsonl + score_repobench.json
- REPORT.md = final report (TL;DR, tables, failure analysis, improvements)

### Verdict (see REPORT.md)
Not top-tier: best Brain arm (mcp .505) ties dense (.515 R@10), flagship v5/v6 lose to BM25;
BM25 dominates snippet pools 2-3x. Brain's edge = token economy (47k@10, eff .0153).

### Phase 5
Skipped — no LLM API key in env.

## v2 retrieval (IN PROGRESS — branch devin/v2-retrieval)

Goal: beat dense baseline on file localization, keep economy edge, no test-set overfitting.

### Done
- STEP 1: dev150 split frozen (A6, splits/dev150_ids.txt; SWE-bench test − Lite − Verified, n=150 stratified).
- STEP 2 code: fixes (a)–(e) as separate commits behind RETRIEVAL_V2_* flags (all default-off):
  - (a) dense-primary fusion: RETRIEVAL_V2_FUSION = wrrf|linear|rrf — commit 42ea657
  - (b) rerank gate: RETRIEVAL_V2_RERANK_ENABLED — commit 0040996
  - (c) empty-channel content-BM25 fallback: RETRIEVAL_V2_BM25_FALLBACK — commit e5041fd
  - (d) identifier/path/frame/error query extraction: RETRIEVAL_V2_ID_QUERY — commit fe8a379
  - (e) vector chunk→file aggregation: RETRIEVAL_V2_VEC_AGG = max|sum|topk — commit 6673cce
  - harness: --v2 flag, 10 brain_v7_* ablation arms, --ids-file — commit 0df2bb5
  - paired significance tool: harness/paired.py (bootstrap CI on per-instance diff + sign test)

### In flight (STEP 3 test — single run, frozen config)
- dev150 ablation DONE (3 rounds, 23 cells, n=150): linear fusion won; frozen config
  = RETRIEVAL_V2_FUSION=linear + RERANK_ENABLED=False, rest defaults (A7, commit 8824b31).
- Lite n=297 --v7 run, 3 disjoint repo shards → results/test_lite_v7_{a,b,c}.jsonl
- CoIR --v7 ×3 datasets → results/coir_v7_{dl,cosqa,soqa}.jsonl
- RepoBench 2 splits w/ built-in brain_v7 arm → results/repobench_v7_{cff,cfr}.jsonl
All resumable via done_ids; same commands as dev runs but --v7 / --subset all.

### Next
1. Merge brain_v7 arms into frozen rows (per-instance arms dict update), score.py,
   paired.py vs dense AND vs brain_fused; eff regression vs .0153 must be ≤10%.
2. Set RETRIEVAL_V2_ENABLED default per outcome (on only if beats dense, CI excl 0).
3. REPORT.md 'v2 retrieval' section + 5-line verdict; open PR.

### Services needed (VM)
embed_server.py on :18099 (source env.sh FIRST — else it defaults to jina-768),
redis brain-redis :6379, neo4j 5.26 at /home/ubuntu/eval_external/neo4j (bin/neo4j start, :7687),
brain_db_eval postgres :5433 (NOT prod brain_db).

## Resume (if ever needed)
source eval/external/harness/env.sh; runners resume via done_ids on the output file.
