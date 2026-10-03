# Brain Golden-Set Retrieval Evaluation

## Overview
This directory contains the golden-set evaluation harness for measuring retrieval quality of the Brain project's `/search` endpoint. The evaluation uses 20 curated questions covering core Brain subsystems and reports Hit@K metrics.

## Token-economy baseline

The current default is `token_economy_tasks_web.json`: a separately frozen
revision for the Next.js migration. It updates six UI cases, rebases source
ranges and uses source method identifiers. The 50 tasks remain curated and
ineligible for production benefit claims. Its source pin and byte digest are
recorded; loading a modified corpus without an explicit re-freeze fails.

`token_economy_tasks_v2.json` and all saved measurements remain unchanged at
their historical source pin. Pre-migration golden fixtures and the old API
benchmark are retained under `historical/pre-web/`; the schema-v1 replay
points at those fixtures. Compare results only within one corpus digest and
source pin. Replaying historical tasks requires their pinned source tree.
`holdout_set.json` is unchanged by this migration.

`token_economy_benchmark.py` compares complete **Brain-first** and local
`rg → targeted range-read` task loops. It charges both the locator and the
follow-up code reads, and records expected file/symbol/range recall plus an
observable completion assertion. Transport tokens remain model-independent
`ceil(UTF-8 bytes / 4)`. API keys are read only from `BRAIN_API_KEY` or
`--api-key-file` and are never persisted.

Acceptance requires a schema-v2 manifest of 50 real production tasks. Every
task must declare production provenance, mandatory files, symbols, ranges and
completion assertions. The checked-in schema-v1 corpus is historical curated
baseline data and the command refuses to treat it as acceptance evidence. It
can only be replayed explicitly with `--allow-non-production-baseline`; the
result is marked ineligible.

```bash
python eval/token_economy_benchmark.py \
  --api-url https://brain.example \
  --api-key-file .secrets/brain-api-key \
  --repo-path /app \
  --output reports/token-economy/baseline.json
```

The required split remains 15 unknown-location, 10 known-symbol, 10
cross-surface, 5 architecture/memory, 5 RU-to-EN and 5 stale/failure cases.
`token_economy_schema.py` rejects a partial, duplicate or recategorized corpus
before any result is written, and refuses a non-production corpus at acceptance
mode.

Measured results and the honest read of them live in
`docs/release/benefit-verification.md`. An unfamiliar-repository leg needs
its own schema-v2 50-task manifest (fixed category split) authored against
that repo's code — the harness is already parameterized for it.

## LFM paired evaluation and runtime parity

The active `golden_set.json` contains 20 curated queries with the migration's
current web targets and explicit language/class/provenance metadata. `lfm_eval_extension.json`
contains 30 separate curated fixtures, bringing the paired set to 50, including
15 RU or RU-to-EN queries. These are translations/synthetic evaluation probes,
not production-real traffic; every item has required provenance and
`production_real=false`. `paired_dataset.schema.json` defines the item
contract.

`paired_retrieval_eval.py` compares saved final rankings from:

1. the authoritative Ranking v6 baseline;
2. the same candidate set with LFM reranking;
3. an optional FastPLAID recall channel plus final rerank.

It reports Hit@3, Recall@10/50, MRR@10 and NDCG@10; paired per-query deltas;
language/query-class slices; and machine-readable LFM/FastPLAID gate verdicts.
It is fully offline and never calls the API or a model:

```bash
python eval/paired_retrieval_eval.py \
  --runs reports/lfm/paired-rankings.json \
  --production-gate \
  --json reports/lfm/paired-eval.json
```

### Frozen holdout and adversarial items

`holdout_set.json` is a **frozen** holdout: items are scored but must never be
edited to fit a tuning run. Its sha256 is recorded in `frozen_datasets.json`;
`load_dataset` verifies the digest and refuses to score a drifted file, so a
silent holdout edit cannot contaminate a comparison. An intentional dataset
change records the new digest explicitly:

```bash
python eval/paired_retrieval_eval.py --runs <any> --write-freeze eval/holdout_set.json
```

Dataset items may carry `split: "tune" | "holdout"` (default `tune`) and
`trap_files` — plausible-but-wrong paths that must not outrank the expected
files. `query_class: "adversarial"` requires `trap_files`; the report adds a
`trap_contamination` block per variant (`in_top_10_rate`, `above_expected_rate`)
and a `split` metric slice. `--split tune|holdout` restricts scoring to one
split; `--holdout-dataset` adds the frozen file to the loaded set.

```bash
python eval/paired_retrieval_eval.py \
  --runs reports/lfm/paired-rankings.json \
  --holdout-dataset eval/holdout_set.json \
  --split holdout \
  --json reports/lfm/holdout-eval.json
```

`--production-gate` accepts only the baked default datasets and a complete
unsplit evaluation; `--holdout-dataset` and `--split tune|holdout` are rejected
in that mode. Holdout reports record the selected split and holdout SHA-256.
The production mode requires a complete
schema-v2 capture bound to one exact index revision, lineage, identity digest,
and document count. Its thresholds are fixed. The output records SHA-256 and
size for the capture, datasets, runner, and evaluator, then rechecks those
inputs after evaluation. Custom thresholds remain available only without the
production flag and cannot be used as release evidence.

`late_interaction_runtime_parity.py` compares PyLate BF16 and remote/GGUF
rankings using top-k overlap, Spearman, Kendall and NDCG delta. Precomputed-file
mode has no heavy dependencies. PyLate and torch are imported only when
`--pylate-model` is explicitly requested; see
`docs/runbooks/lfm-colbert-cutover.md` for the GPU and deterministic replay
commands. `runtime_parity_corpus.schema.json` defines its fixed query/document
corpus.

## Files

### `golden_set.json`
Exactly 20 questions derived from actual Brain codebase paths. Each question has:
- `id`: Unique identifier (q001–q020)
- `question`: Natural-language question an engineer would ask
- `expect_files`: Repo-relative paths (verified to exist) that answer the question
- `why`: Rationale for the question's coverage

Questions span:
1. Retrieval pipeline (chunking, indexing, hybrid search)
2. Embedding providers and input caps
3. `/search` endpoint and decision injection
4. Task ledger and optimistic concurrency
5. Decision and rule stores
6. API auth mechanisms
7. Dashboard features
8. Graph structures
9. Background jobs
10. LLM provider routing

### `run_golden_eval.py`
Evaluation runner that:
- Reads Brain API key from `~/.claude/harness/.brain-api-key` (never logged)
- POSTs each question to `${BRAIN_API_URL}/search` (local API by default)
- Computes Hit@1, Hit@3, Hit@5, MRR per question
- Outputs table + summary to stdout
- Optionally writes full results to JSON (--json)
- Exits non-zero if Hit@3 < threshold (default 70%)

## Usage

```bash
# Activate venv
source .venv/Scripts/activate

# Run evaluation (default: limit=5, threshold=70)
python eval/run_golden_eval.py

# With custom limit and threshold
python eval/run_golden_eval.py --limit 10 --threshold 75

# Write results for trend tracking
python eval/run_golden_eval.py --json reports/golden_eval_results.json

# All options
python eval/run_golden_eval.py \
  --limit 5 \
  --threshold 70 \
  --json reports/golden_eval_results.json
```

## Output

Stdout:
```
Q      | Question                    | Hit@1 | Hit@3 | Hit@5 | MRR     | Status
------ | --------------------------- | ----- | ----- | ----- | ------- | ------
q001   | How are files chunked...    | FAIL  | FAIL  | FAIL  | 0.0000  | FAIL
...
Summary: HIT@1=5.00%, HIT@3=15.00%, HIT@5=15.00%, MRR=0.0917
HIT@3=15.00%
```

JSON (`--json <path>`):
```json
{
  "endpoint": "http://127.0.0.1:8010/search",
  "limit": 5,
  "threshold": 70.0,
  "count": 20,
  "summary": {
    "hit@1_pct": 15.0,
    "hit@3_pct": 15.0,
    "hit@5_pct": 15.0,
    "mrr": 0.0917
  },
  "results": [
    {
      "id": "q001",
      "question": "...",
      "rank": 0,
      "hit_at_1": false,
      "hit_at_3": false,
      "hit_at_5": false,
      "mrr": 0.0,
      "status": "FAIL",
      "expected_files": ["brain/indexers/file_indexer.py", "..."],
      "response_files": ["apps/api/auth.py", "..."],
      "response": { ... full /search response ... }
    }
  ]
}
```

## Interpreting Results

**Hit@K** = percentage of questions where at least one expected file appeared in the top-K results
- Hit@1: file in rank 1
- Hit@3: file in ranks 1–3
- Hit@5: file in ranks 1–5

**MRR** = Mean Reciprocal Rank (average 1/rank of first hit across questions)

**Status** = PASS if Hit@3, else FAIL

## Exit Codes

- `0`: Hit@3 >= threshold
- `1`: Hit@3 < threshold (or network error)

## Linting

```bash
python -m ruff check eval/run_golden_eval.py
```
