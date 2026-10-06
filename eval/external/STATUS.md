# External eval — status

Branch: `devin/1791322327-external-retrieval-eval`

## Done
- Phase 0 complete: entry points inventoried, embedder decided (jina-code via local
  ONNX shim — prod NVIDIA key unavailable; see PROTOCOL.md §4).
- `PROTOCOL.md` + `PROTOCOL_AMENDMENTS.md` frozen and committed BEFORE any data run.
- Harness written: `harness/{common,embed_server,env.sh,run_swebench,score}.py`.
- Env up: eval Postgres `brain_db_eval` (localhost:5433), Neo4j 5.26 at
  `/home/ubuntu/eval_external/neo4j` (bolt 7687, neo4j/neo4j_password),
  embed shim on 127.0.0.1:18099 (`nohup .venv/bin/python eval/external/harness/embed_server.py &`),
  all 12 SWE-bench repos cloned to `/home/ubuntu/eval_external/repos/`.
- Smoke run passed (dev-fold psf__requests-2317).

## In progress
- Phase 1 stratified-60 running as 3 shards (started ~21:53 UTC 2026-10-06):
  - `run_swebench.py --subset stratified60 --repos django/django` → `results/swebench60_django.jsonl`
  - same for `sympy/sympy` → `swebench60_sympy.jsonl`
  - same for the other 10 repos → `swebench60_rest.jsonl`
  Logs: `/home/ubuntu/eval_external/log_{django,sympy,rest}.log`.
  NOTE: this run wrote `graph_available: false` in rows — flag was hardcoded before
  the neo4j_up() probe was added; Neo4j IS up and the graph channel is populated
  (verified ~1500 nodes). Treat as `true` when scoring.

## Next
1. Score: `.venv/bin/python eval/external/harness/score.py '/home/ubuntu/eval_external/results/swebench60_*.jsonl' --label stratified60`
2. Extend to all 300: same commands with `--subset all` (resume skips done rows —
   move the 60-row files' content into the `all` output files first OR just rerun
   with fresh out files and merge at scoring; resume reads ids from --out file).
3. Phase 2: CoIR cosqa/stackoverflow-qa (corpus+queries+default qrels configs work),
   codesearchnet (config `python-*`), codetrans-dl; RepoBench-R python_cff.gz/
   python_cfr.gz are gzipped *pickles* (dict test→{easy,hard}→list of 8000 items;
   item keys: repo_name, file_path, code, import_statement, context(list),
   golden_snippet_index). Query = code + import_statement; rank context candidates.
   Datasets already in HF cache (`/home/ubuntu/.cache/huggingface`).
4. Phase 4 cost/latency: fields index_ms/retrieval_ms/chunks_total already in rows;
   tokens via tiktoken on packs.
5. Phase 5: SKIP — no LLM key in env (disclose in report).
6. REPORT.md + PR.

## Resume
```bash
cd /home/ubuntu/repos/project-brain-public
set -a && source eval/external/harness/env.sh && set +a
# ensure services up:
pg_isready -h localhost -p 5433 || docker compose up -d postgres
curl -s http://127.0.0.1:18099/health || nohup .venv/bin/python eval/external/harness/embed_server.py >/home/ubuntu/eval_external/embed.log 2>&1 &
/home/ubuntu/eval_external/neo4j/bin/neo4j status || /home/ubuntu/eval_external/neo4j/bin/neo4j start
# rerun any shard command above; it resumes from --out JSONL
```
