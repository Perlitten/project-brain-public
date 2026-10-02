# Benefit verification — measured results

Controlled comparison harness: `eval/token_economy_benchmark.py` (schema-v2,
paired arms — every task runs through BOTH strategies against the same
indexed repository and the same read-window budgets).

Arms:

- `brain_first` — `/search` (`response_mode=locator`) then the targeted reads
  the locators point at.
- `local_rg_read` — local `rg` locator + the same targeted-read budget.

Tokens are **estimated transport tokens**: `ceil(UTF-8 bytes / 4)` —
model-independent, no provider key required. No provider calls occur in
either arm (`provider_usage.mode: mock`; `actual_provider_tokens: null`).
`tool_calls` counts locator + read calls.

**Metric scope (honesty label on every report):** literal-assertion coverage
is a *retrieval proxy* — verbatim substring checks inside locator-window
reads — not observed coding-task completion. Assertions that are prose or
stale (not verbatim substrings of the expected files at the evaluated
revision) are unscored and reported, never counted.

## Scorer corrections since the first run (2026-10-02)

The first published run reported "0% task completion in both arms". Audited
root causes (all quantified by `eval/audit_corpus.py`):

1. **Unscorable assertions.** Assertions containing natural-language prose
   could never substring-match; scorer now classifies assertions as literal
   only when they occur **verbatim** in an expected file at the evaluated
   revision. v2 corpus: 7/291 assertions are non-verbatim → unscored.
2. **Ground truth vs. ranges.** 126/284 literal assertions sit *outside* the
   expected ranges — unreachable under the targeted-read contract for both
   arms. v2's per-task `failure_reasons` now separates
   `in_expected_range_not_read` (a real read-window miss) from
   `outside_expected_ranges` (a corpus-contract defect).
3. **Stale symbols.** 31 expected symbols absent from the pinned tree
   (`symbols_absent` in the audit).
4. **Provenance.** v2 tasks are `kind: curated`, `production_real: false`
   (AST enrichment does not create production provenance); the schema now
   rejects production claims from curated-derived tasks.
5. **Revisions pinned.** Both corpora pin `repository.pinned_commit`;
   reports carry `pin_check.aligned` and the alignment mode
   (`head_equals_pin` / `expected_files_unchanged` / `expected_files_differ`).

## Run 2026-10-02 — unfamiliar repository (Flask 3.1.3), live API, mock providers

Repository: `pallets/flask` pinned at `22d924701a6ae2e4cd01e9a15bbaf3946094af65`
(tag 3.1.3), indexed commit = pin (`head_equals_pin`). Corpus:
`eval/token_economy_tasks_flask.json` — 20 author-curated tasks
(16 evaluable + **4 untouched holdout**: f017–f020), all 32 literals
verified in-range (`eval/results/corpus-audit-flask-3.1.3.json`).
3 paired repeats. Command:

```bash
BRAIN_API_KEY=change-me-in-production PYTHONPATH=eval \
python eval/token_economy_benchmark.py \
  --tasks eval/token_economy_tasks_flask.json \
  --output eval/results/flask-repo-paired-x3.json \
  --api-url http://127.0.0.1:8010 \
  --repo-path /tmp/eval-repos/flask --local-root /tmp/eval-repos/flask \
  --repeats 3 --allow-non-production-baseline
```

Raw artifact: `eval/results/flask-repo-paired-x3.json`.

| Metric | brain_first | local_rg_read | Direction |
|---|---|---|---|
| hit@3 (any expected file in first 3 locators) | 12.5% | 12.5% | tie |
| mandatory file recall | 56% (9/16) | 6% (1/16) | Brain +9x |
| mandatory range recall | 43.8% (7/16) | 6.2% (1/16) | Brain +7x |
| literal assertion coverage | 37.5% (12/32) | 12.5% (4/32) | Brain +3x |
| evidence_complete (files+symbols+ranges+all in-scope literals) | 25% (4/16) | 0% | Brain |
| median estimated transport tokens | 4,093 | 2,243 | Brain +1.8x cost |
| p95 latency | 57.8 ms | 9.6 ms | Brain +6x slower |
| mean tool calls | ~1.9 | ~1.8 | ~same |

On a clean, in-range-validated unfamiliar corpus, Brain's locator arm wins
every recall metric at ~1.8x the estimated transport cost. Still a
retrieval-proxy measurement — not agent task completion, not uplift.

## Run 2026-10-02 — Brain repository, live API, mock providers (rescored)

Repository: this checkout, index at `cd022eb`, corpus pinned at `2f2e08e`
(`expected_files_unchanged` — eval-only diffs between pin and HEAD).
3 paired repeats. Raw: `eval/results/brain-repo-paired-x3.json`.

| Metric | brain_first | local_rg_read | Direction |
|---|---|---|---|
| hit@3 | 12% | 0% | Brain |
| mandatory range recall | 2.6% | 0.8% | Brain |
| literal assertion coverage | 1.4% (4/284) | 26.8% (76/284) | rg +19x |
| evidence_complete | 0% | 0% | — |
| median estimated transport tokens | 4,286 | 2,356 | Brain +1.8x |
| p95 latency | 97 ms | 25 ms | Brain +4x |

Miss attribution (`failure_reasons` in the raw artifact): of 284 literals,
126 sit outside expected ranges (unreachable by contract, both arms); of
the 158 in-range, Brain reads cover 4, rg's line-context reads cover 41.
The v2 corpus mixes valid range-level ground truth with literals placed
elsewhere in the file — its retrieval-proxy scores under-measure locator
recall and must not be tuned to improve scores; it stays pinned and its
defects stay visible.

## What the measurement says — honestly

- **Locator recall is a real, repeatable benefit on unfamiliar code:**
  +7–9x range/file recall and +3x literal coverage over `rg` on the audited
  Flask corpus.
- **Coverage ≠ completion.** Literal-assertion coverage is a substring
  proxy; agent task completion was not measured (no agent, no provider).
- **Cost is real:** ~1.8x estimated transport tokens, 4–6x latency.
- **No uplift claim.** Two corpora, mock providers, retrieval proxy only.

## Still required before a benefit claim (blocked/untested)

- **Agent-level paired runs** (same coding agent + provider completing
  tasks via the MCP tool surface vs. local tools only). Needs provider
  credentials via the secret mechanism — **blocked: no provider key**.
- **Real-provider rerun** (`DEFAULT_LLM_PROVIDER` ≠ mock) — the mock
  summarizer produces truncation digests; the assertion-coverage gap is
  partially an artifact of this.
- **Holdout result.** f017–f020 are intentionally untouched; report with
  `--include-holdout` once the agent-level harness exists. Do not tune
  labels or ranges against holdout scores.
- Production-provenance corpus: v2 and Flask corpora are both
  author-curated (`production_real: false`) and therefore
  acceptance-ineligible for production benefit claims — enforced by
  `token_economy_schema.py` (`_production_claim_invalid`).

## Corpus audit artifacts

```bash
python eval/audit_corpus.py --tasks eval/token_economy_tasks_flask.json \
  --root /tmp/eval-repos/flask --json eval/results/corpus-audit-flask-3.1.3.json
python eval/audit_corpus.py --tasks eval/token_economy_tasks_v2.json \
  --root . --json eval/results/corpus-audit-brain-2f2e08e.json
```

- `eval/results/corpus-audit-flask-3.1.3.json` — 0 files missing, 0 stale
  ranges, 0 non-verbatim assertions, 32/32 literals in expected ranges.
- `eval/results/corpus-audit-brain-2f2e08e.json` — 31 stale symbols,
  7 non-verbatim assertions, 126 literals outside expected ranges.
