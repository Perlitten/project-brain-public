# STATUS — external retrieval eval (COMPLETE)

All phases done. Final artifacts in this directory.

## Results
- SWE-bench Lite n=297 (all minus dev fold, 0 errors): results/swebench_all.jsonl + score_swebench_all.json
- CoIR: cosqa 500, codetrans-dl 195, soqa 500-capped: results/coir_*.jsonl + score_coir_*.json
- RepoBench-R n=3027: results/repobench_*.jsonl + score_repobench.json
- REPORT.md = final report (TL;DR, tables, failure analysis, improvements)

## Verdict (see REPORT.md)
Not top-tier: best Brain arm (mcp .505) ties dense (.515 R@10), flagship v5/v6 lose to BM25;
BM25 dominates snippet pools 2-3x. Brain's edge = token economy (47k@10, eff .0153).

## Phase 5
Skipped — no LLM API key in env.

## Resume (if ever needed)
source eval/external/harness/env.sh; embed_server.py on :18099; brain_db_eval postgres:5433;
runners resume via done_ids on the output file.
