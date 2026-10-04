# Harness Effectiveness Benchmark

Does Project Brain actually help development? This benchmark measures it
against human-curated ground truth — not vibes.

## What it measures

| Task type | Endpoint | Question answered | Key metric |
|---|---|---|---|
| `context` | `/context` | Does the context pack contain the files an engineer needs for the task? | must-have hit rate |
| `impact` | `/impact` | Does impact analysis find the files a change really touches? | must-have recall |
| `review` | `/diff-review` | Does review catch planted bugs (mutation testing) and stay quiet on clean diffs? | detection rate / false-alarm rate |

Retrieval quality per se is covered by `eval/run_golden_eval.py` (golden set,
Hit@K). This benchmark sits one level higher: **task usefulness**.

Supporting metrics: `noise_ratio` (how much irrelevant material comes back
with the useful files), `llm_review_status` (a review that never ran must
never look clean — see `brain/analyzers/diff_analyzer.py`).

## Running

The API must be up. Review tasks need a clean worktree; they create and
delete temporary branches `bench/<task-id>`.

```bash
python eval/harness_benchmark/run.py
python eval/harness_benchmark/run.py --only review-001 --output /tmp/bench.json
python eval/harness_benchmark/run.py --skip-review   # no branches, no LLM cost
```

API key is read from `PROJECT_BRAIN_API_KEY` or `<repo>/.env`.

## Adding tasks

Drop a JSON file into `tasks/`. Fields:

- `id`, `type` (`context` | `impact` | `review`), `base_commit` (the tree
  the ground truth was verified on — recorded as provenance; the runner
  applies the patch to the current HEAD and records `base_used`, so keep
  the tree stable when comparing runs)
- `context`: `task_description`, `must_have_files`, `nice_to_have_files`
- `impact`: `change_request`, `must_have_files`, `nice_to_have_files`
- `review`: `patch` (unified diff applied to a temp branch), `clean`
  (true = expect silence), `must_detect_keywords` (for planted bugs —
  keywords that must appear in the review text)

Ground truth should come from real work: PR descriptions, questions you
actually asked, bugs you actually planted. A task whose ground truth you
did not verify is a guess, not a measurement — mark its provenance honestly.

## Interpreting results

- `must_have_hit_rate` / `must_have_recall` < 1.0 → the harness missed
  something an engineer needed. Look at which files were missed and why.
- `noise_ratio` high → the signal drowns; consider filtering or ranking.
- `review` detection < 1.0 → the reviewer is blind to that bug class.
- `review` false alarms on clean diffs → the reviewer cries wolf; engineers
  will ignore it.

The strongest claim this benchmark supports is comparative: run the same
tasks with and without the harness (or before/after a change) and report
the delta.

## A note on LLM nondeterminism

`review` tasks depend on an LLM judgment call, so a single run can vary:
the same planted bug may be flagged in one run and missed in the next.
Treat a single review-task result as a sample, not a verdict — run review
tasks 3x and report the mean detection rate for stable numbers. `context`
and `impact` tasks are deterministic given a fixed index.
