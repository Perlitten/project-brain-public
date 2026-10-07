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

### In flight
- dev150 ablation run: /home/ubuntu/eval_external/dev150_v2.log → results/dev150_v2.jsonl
  ETA ~1.7h (11 pipeline calls per instance; graph timeouts on dependency routes inflate some).
  Command:
  cd eval/external/harness && set -a && source env.sh && set +a &&
  .venv/bin/python run_swebench.py --dataset princeton-nlp/SWE-bench \
      --ids-file ../splits/dev150_ids.txt --index-once --v2 \
      --out /home/ubuntu/eval_external/results/dev150_v2.jsonl
  (resumable — already-done instance_ids are skipped via done_ids)

### Next
1. Score dev150_v2.jsonl (score.py) + paired.py vs brain_v5 and dense; pick per-fix winners.
2. STEP 3: freeze chosen config → ONE Lite n=297 run (same harness, --index-once, no --v2
   but v7 arm only) + CoIR + RepoBench vs frozen numbers; paired CIs + sign test vs dense;
   eff regression ≤ 10%.
3. Set flag defaults per outcome; REPORT.md 'v2 retrieval' section + 5-line verdict; PR.

### Services needed (VM)
embed_server.py on :18099 (source env.sh FIRST — else it defaults to jina-768),
redis brain-redis :6379, neo4j 5.26 at /home/ubuntu/eval_external/neo4j (bin/neo4j start, :7687),
brain_db_eval postgres :5433 (NOT prod brain_db).

## Resume (if ever needed)
source eval/external/harness/env.sh; runners resume via done_ids on the output file.
