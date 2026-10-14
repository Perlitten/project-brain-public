# PR #75 review hotfix

Date: 2026-10-14. Base: current `main` at
`9079b353e4f16b2f557f1368bf9b9f0809b15d75`, after PR #75 and the subsequent UX
fixes. Dedicated branch: `hotfix/pr75-review`.

## Confirmed findings and corrections

All nine unresolved findings were reproduced or confirmed in the base source.
The older CodeQL regex thread was already resolved and is not counted here.

| Priority / original comment | Correction | Regression coverage |
|---|---|---|
| P1 [deadline](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699610) | Scope lookup, vector/symbol channels, fallback freshness and optional BM25 share one absolute deadline; completed channels survive expiry; owned tasks are cancelled and their late errors reaped without waiting indefinitely for provider cleanup | `test_retrieval_deadlines.py`: stalled symbols with successful/stalled/failed search, slow scope/freshness, caller cancellation, slow provider cleanup and BM25 timeout |
| P1 [required-file recall](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699615) | Gate rejects aggregate recall loss and loss on any individual required-file task, even when another task improves or the first-hit rank is unchanged | `test_product_upgrade_gate.py`: stable first hit with a lost second file; aggregate gains masking one task's loss |
| P1 [golden scores](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699624) | Gate derives ranks/hits/MRR from captured file/chunk paths using the existing golden scorer; stored favorable or unfavorable scores cannot change acceptance | `test_product_upgrade_gate.py`: removed expected paths with favorable saved scores; corrupted baseline scores |
| P2 [templates](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699619) | Root-relative and Windows template paths are recognized; file-type promotion requires explicit query intent, including unconventional paths | `test_filters.py`: root/nested/Windows templates and explicit file-type intent |
| P2 [language evidence](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699622) | Relevant source extensions support named programming-language anchors; unknown platforms still require evidence and test fixtures do not establish production features | `test_runtime_evidence_support.py`: Python/TypeScript/JavaScript/Kotlin/Rust, unknown platform and fixture counterexamples |
| P2 [capture phases](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699629) | Gate requires before/after in order; CLI and capture function reject unknown phases before credentials/HTTP | `test_product_upgrade_gate.py`: same-phase, swapped and arbitrary labels; invalid phase without a credential |
| P2 [BM25 isolation](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699634) | Ordinary optional-channel failures report degradation while retaining successful primary channels; cancellation propagates | `test_retrieval_deadlines.py`: SQL operational, streaming, limit/timeout errors and cancellation |
| P2 [false test paths](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699640) | Explicit test components and naming conventions replace substring checks in retrieval and new-index classification; `contest`, `specification`, `latest` and `specialist` remain source code | `test_filters.py`: production counterexamples and Python/JS/Java test conventions |
| P2 [Lite traversal](https://github.com/Perlitten/project-brain-public/pull/75#discussion_r4266699648) | SQLite reads adjacency on demand instead of a full snapshot; each seed reads at most 128 edges and visits at most 64 nodes; at most 12 keywords × 5 seeds × 8 returned neighbors; cancellation signals the worker and SQLite progress handler | `test_lite_graph_bounds.py`: 2500-edge hub hydration bound, scope/limit, cancellation and expiry; also included in Windows/Linux Lite CI |

Normal learned-provider routing remains intact: BM25 is still exclusive to the
local token-hash profile, and successful real-provider/vector contracts retain
their existing fusion and explicit legacy compatibility settings. Timeout
fallback freshness is honestly `unknown` when there is no time for verification.
Previously misclassified stored file types require reindexing to correct;
query-time path classification is corrected immediately.

## Offline verification

- Full default-profile Python suite: **2163 passed, 11 optional skips**.
- Ruff passes; mypy reports **zero errors**; public launch and docs guards pass.
- Wheel/sdist archive checks pass. The candidate wheel is installed without
  dependency downloads into an isolated copy of the hash-locked baseline
  environment. Actual installed full-mode MCP search/context/impact passes for
  default and explicit legacy contracts, including durable artifact bytes.
- Installed Lite stdio search/context/impact passes with local token hashing,
  disabled chat and no backing services required.
- No model download, remote model inference, paid provider call or NVIDIA key
  dependency. Mock providers prove functional compatibility; they do not prove
  learned semantic or coding benefit.

New [before](evidence/pr75-hotfix/before-validated.json) and
[after](evidence/pr75-hotfix/after-review3.json) captures execute all 20 unchanged goldens,
seven context tasks and four impact tasks against the same frozen source
(`2a7e66819c60b6eeb19edbb2cc9c2a082184fe57`), verified index and offline provider.
The baseline package's six changed runtime modules byte-match pre-hotfix main;
each arm runs in a fresh process, so the context cache starts empty. Chat is
disabled and API calls use an in-process ASGI transport.

The strengthened [gate](evidence/pr75-hotfix/gate-review3.json) passes:

| Metric | Before → hotfix |
|---|---|
| Golden Hit@1 / Hit@3 / Hit@5 / MRR | 50% / 70% / 75% / 0.6211 → unchanged |
| Positive context Hit@1 / Hit@3 / MRR / required-file recall | 33.3% / 66.7% / 0.5056 / 75% → unchanged |
| Positive impact Hit@1 / Hit@3 / MRR / required-file recall | 50% / 100% / 0.75 / 100% → unchanged |
| Three adversarial restraint tasks | pass → pass |

```bash
python -m eval.product_upgrade_gate \
  docs/release/evidence/pr75-hotfix/before-validated.json \
  docs/release/evidence/pr75-hotfix/after-review3.json
```

Existing golden inputs, task definitions, baseline captures and historical
outcomes were not modified. Six goldens still miss at rank three;
`context-006` still misses its required file and `context-004` has partial
recall. These pre-existing quality limits remain visible and do not count as
new regressions or as resolved by this hotfix.

## Release recommendation

Merge only after exact-head CI, CodeQL and fresh review pass, with no unresolved
blocking findings. Then consider a compatible patch release carrying these
corrections and the existing retrieval limitations. Do not describe offline
token hashing as learned semantic evidence. No tag, published release asset or
production deployment is changed or published by this hotfix.

## Review follow-up and evidence hardening

PR #77 was merged by the owner at `4a8f791ae60a3c0dcb0a684a233269fbf48ef8ff`
before its three P2 review corrections were delivered. This follow-up includes
standalone `test.py`/`tests.py`/`conftest.py` recognition, explicit degradation
when no budget remains to start BM25, and reserved incoming-edge capacity so
outgoing hub symbols cannot hide incoming callers. Each has regression coverage.

The gate now rejects inconsistent stored metrics as well as recomputing ranks,
validates frozen canonical inputs, input hashes, task identities, requested and
effective budgets, and single known retrieval contracts. Hit@5 also gates
acceptance. Unified results cannot borrow legacy compatibility chunk hits.
Captures record elapsed time and actual budgets, without credentials or content.
The wider context slice pool remains valid: each published locator must have a
returned slice. Initial captures and instrumentation-validation captures are
retained separately; historical captures are unchanged.

CPU lexical ranking cooperatively stops on cancellation; lexical term iteration
and bounded graph queries use deterministic ordering. Deadline tests include
32 simultaneous requests and assert every cooperative child completes, alongside
elapsed-time bounds and slow cancellation cleanup. Provider code that suppresses
cancellation cannot be forcibly terminated by asyncio; late errors are consumed
rather than delaying the response indefinitely.

Historical retrieval-default evidence independently shows Hit@5 **80% → 75%**.
The stricter gate flags this historical loss; it is not relabelled as passing.
The matched-contract pre-hotfix/current comparison shows no additional loss.
Token-hash results do not establish learned semantic quality.

PR #78's first review additionally found v2 HTTP-200 failure envelopes and
unused adjacency capacity. Captures now reject endpoint error markers and
deadline failures before saving evidence, while legitimate unsupported-feature
abstention remains valid. Traversal backfills unused incoming capacity from
outgoing edges without exceeding 128 total edge reads or losing the incoming
reservation. Six failure-envelope/abstention cases and two unbalanced-degree
regressions pass. All 31 tasks were freshly captured after these corrections.
