# Project Brain — demo: what it actually found

This page is not a feature list. It is the evidence log: real bugs Project Brain
found in its own codebase, measured on `eval/harness_benchmark/`.

## Bug 1: diff-review silently approved garbage

**What happened.** The `/diff-review` endpoint asked the LLM to judge a diff.
When the LLM returned unparseable JSON, the code caught the parse error and
defaulted to `approved`. A strict-mode bypass planted as a test sailed through
review with a green stamp.

**What Brain did.** The harness benchmark's mutation-testing review tasks
(`review_001_strict_bypass`) caught it: a deliberately planted bug was approved.

**Fix** (commit `b789f6c`): robust JSON parsing (fence/prose stripping,
invalid-escape repair) + an honest `llm_review_status` field — `needs_review`,
never `approved`, when the model output cannot be parsed. 11 regression tests
in `tests/test_diff_review_llm_fallback.py`, verified end-to-end: the LLM review
caught the planted bug after the fix.

## Bug 2: duplicate `files` rows

**What happened.** Re-indexing the same repository created duplicate rows in the
`files` table — the upsert had no uniqueness guard.

**Fix** (commit `0b2257f`): `UNIQUE(repository_id, path)` constraint + migration
+ hardened upsert. Zero duplicates after.

## Bug 3: impact-analysis noise

**What happened.** `/impact` returned SQL fragments and prose noise mixed into
the affected-files list, drowning the signal.

**Fix** (commit `0b2257f`): SQL/prose noise filter on the analyzer output.

## Measured usefulness

`eval/harness_benchmark/` — 9 tasks against human-curated ground truth:

| Area | Result |
|---|---|
| Context hit-rate (does `/context` return the files an engineer needs?) | 3/3 |
| Impact recall (does `/impact` find the files a change really touches?) | 2/2 |
| Diff-review mutation testing (catch planted bugs, stay quiet on clean diffs) | 2/4* |
| Memory recall (plant a learning, ask, check it surfaces) | new |
| **Overall** | **0.833** |

\* The 2 review misses were transient NVIDIA API errors, not wrong judgments —
documented in the benchmark methodology. The benchmark now runs weekly in CI
(`.github/workflows/benchmark.yml`) with a regression gate.

## Try it yourself

```bash
./scripts/bootstrap.sh
.venv/bin/brain index --repo /path/to/your/repo
.venv/bin/uvicorn apps.api.main:app --port 8000
python eval/harness_benchmark/run.py --skip-review
```

Then plant a bug in a branch and ask for a review:

```bash
curl -H "X-API-Key: $PROJECT_BRAIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"base": "main", "head": "your-branch", "repo_path": "/path/to/your/repo"}' \
  http://127.0.0.1:8000/diff-review
```
