# Architecture refactoring and product upgrade evidence

Assessment date: 2026-10-14. Baseline: immutable **v0.9.2**
(`ceffe3a2d5ad6b4fa330b2d972442bd58c6f7a49`). This records new work on main and
candidate branches; it does not revise published release evidence or archives.

## Result and publication state

Four responsibility-boundary PRs were merged after full CI, CodeQL and installed
CLI/MCP acceptance: [#66](https://github.com/Perlitten/project-brain-public/pull/66),
[#67](https://github.com/Perlitten/project-brain-public/pull/67),
[#68](https://github.com/Perlitten/project-brain-public/pull/68), and
[#69](https://github.com/Perlitten/project-brain-public/pull/69).
The subsequent freshness upgrade [#72](https://github.com/Perlitten/project-brain-public/pull/72)
is merged, including owner hardening of current-source reads and diagnostics.
Embedding-first indexing [#73](https://github.com/Perlitten/project-brain-public/pull/73)
and SQLite Lite [#74](https://github.com/Perlitten/project-brain-public/pull/74)
were subsequently merged by the owner, including additional summary and queue
hardening. Their code/CI/CodeQL checks pass; Vercel preview checks still report
a build rate limit. Those merges were owner actions, not bypasses by this agent.
The retrieval candidate [#75](https://github.com/Perlitten/project-brain-public/pull/75)
is verified locally and documented separately below. Both of its Vercel previews
pass. A new CodeQL regex alert was fixed by stripping question whitespace once
before matching; CodeQL subsequently reports no new alerts.

No release is published by this work. v0.9.0, v0.9.1 and v0.9.2 tags and
historical release archives remain unchanged. No production infrastructure,
user data, golden answers or prior benchmark result files were modified.

## Measured responsibility boundaries

Function lines include declarations and bodies. Cyclomatic scores use
`radon==6.0.1`; cognitive scores use `cognitive-complexity==1.3.0`, which is not
certified Sonar output. The same AST procedure measures both sides. These are
function-level results, not a claim that total program complexity disappeared.

| Function | Lines before → candidate | Cyclomatic before → candidate | Cognitive before → candidate |
|---|---:|---:|---:|
| `proactive.deterministic_insights` | 462 → 32 | 90 → 4 | 85 → 3 |
| `HybridRetrievalPipeline.run` | 808 → 742 | 221 → 195 | 364 → 322 |
| `ContextPackBuilder.build_context_pack` | 627 → 530 | 153 → 131 | 198 → 161 |
| `FileIndexer.index_file` | 346 → 238 | 59 → 39 | 82 → 52 |
| `FileIndexer._post_process_graph` | 303 → 7 | 42 → 1 | 90 → 0 |
| `proactive.generate_proactive_insights` | 141 → 141 | 24 → 24 | 29 → 29 |

The index-file reduction includes the subsequent preparation boundary needed
for optional summaries; other rows describe the behavior-preserving foundation.
The extracted code still contributes complexity in its own smaller functions.

| Module | v0.9.2 lines → foundation lines | Explicit fan-out before → foundation |
|---|---:|---:|
| `brain/insights/proactive.py` | 1624 → 1174 | 17 → 19 |
| `brain/retrieval/pipeline.py` | 1481 → 1372 | 16 → 17 |
| `brain/context/context_pack_builder.py` | 1110 → 1014 | 15 → 16 |
| `brain/indexers/file_indexer.py` | 1727 → 1430 | 25 → 25 |

Explicit import fan-in remains 2, 2, 8 and 3 respectively in this measurement.
`scripts/architecture_metrics.py` counts explicit internal import edges; it does
not infer dynamic calls and undercounts some `from package import module`
aliases. Fan-out increases at several orchestrators because they now depend on
explicit stage modules. The benefit is responsibility ownership, not lower
total import coupling. Product indexing adds preparation/enrichment imports
(1493 module lines and fan-out 27 at the measured candidate).

## Ownership and cohesion

- `insights/candidates.py` owns immutable candidate records;
  `insights/detectors.py` owns seven pure snapshot detectors. The proactive
  orchestrator retains collection, external synthesis, caching and persistence.
  Detector environment/minimum-recall inputs are explicit; import aliases remain.
- `retrieval/fusion.py` owns channel preparation, policy-driven scoring and
  channel views. The pipeline retains query routing, provider deadlines,
  database hydration, reranking and selection. Pure stages do not open sessions.
- `context/rendering.py` owns evidence and complete Markdown rendering through
  immutable value records. The builder retains selection, graph reads, memory
  freshness, byte budgets, artifact persistence and write fencing.
- `indexers/graph_enrichment.py` owns hierarchy/features/imports/semantic graph
  enrichment with a scoped write buffer, progress and failure callbacks. The
  indexer retains source verification, SQL projection and job ownership.
- `indexers/source_summaries.py` supplies bounded deterministic descriptions;
  `summary_enrichment.py` owns opt-in synthesis outside transactions and guarded
  backfill. `PreparedFile` separates provider preparation from projection writes.

This creates direct test seams while preserving the orchestration sequence and
avoiding a new framework. SQLite adapters are a later, explicitly authorized
behavioral addition, not part of the equivalence claim for the four refactors.

## Behavior-preservation evidence

Characterization was captured from the release implementation before extraction:
11 complete insight-record cases; 28 complete fusion records covering legacy,
linear, weighted RRF, RRF, ties, exclusions and weak/strong channels; six complete
Markdown byte fixtures plus malformed-trace cases; eight ordered graph traces
covering failures, progress, buffering and cancellation. Fixtures were not
re-blessed to conceal differences.

Installed release-baseline versus extracted package comparisons also matched:
12 complete pipeline results across four modes and budgets 2/5/10; eight complete
context structures and actual artifact bytes across small/deep budgets,
mock/skip-plan modes and isolated cold/warm reranker cache states. Comparisons
exclude only generated database IDs, output filenames and wall-clock timings.

Full suites passed at each foundation stage: 2003, 2031, 2039 and 2047 tests
respectively, with 11 optional skips each. Ruff and the zero-error mypy ratchet
passed. Clean hash-locked wheel installs ran indexing, reindexing and actual
MCP search/context/artifact checks before each merge. Mocks prove functional
contracts here; they do not prove semantic quality or coding benefit.

## Product behavior and evidence

| Workstream | Implemented behavior | Evidence and limits |
|---|---|---|
| Freshness | Warn-and-serve default, bounded Git delta, current disk slices, scoped memory and throttled reindex; explicit strict mode | Dirty/ahead, moved declarations, deleted source, missing source, secret/symlink exclusion and byte bounds covered; dirty reindex waits for a clean commit |
| Indexing | Lazy/off foreground indexing never calls the chat summarizer; structural symbols, deterministic descriptions, graph and vectors finish first | Real SQL projection test forbids summarizer access, verifies all projections/readiness and replay; embedding-provider costs still apply |
| Lite | SQLite/aiosqlite relational state, float32 cosine, persisted adjacency, TTL cache, durable maintenance queue and SQL lease fences | Zero-service smoke passes on Linux and Windows CI; installed stdio MCP search/context/impact passes; local single-operator storage, no arbitrary Cypher |
| Retrieval | Bounded locator/runtime default, dense-primary linear scores, per-channel maxima, query-aware tests/templates, bounded offline BM25 companion | Aggregate non-regression over all 31 unchanged tasks; individual misses and model limits remain explicit |

An owned offline fixture indexed **887 files / 8643 chunks in 34.599 seconds**,
with complete embedding coverage and verified source revision. This uses local
token-feature hashing, not a learned semantic model or a remote production
provider. It is one structural smoke measurement, not proof of a 10× speedup,
universal 1–3 minute indexing, or a 9/10 product rating.

Small synthetic extraction microbenchmarks: insight detector median 35.99 →
35.30 μs (peak traced allocations 13403 → 12411 bytes); legacy fusion median
182.74 → 168.80 μs (3115 → 3163 bytes). These exclude network/storage latency
and do not establish production performance gains.

## Retrieval comparison and honest acceptance

The frozen corpus is source commit `2a7e66819c60b6eeb19edbb2cc9c2a082184fe57`.
Both arms use the same verified index, graph, provider and unchanged questions.
`local-token-hash-v1` supplies offline lexical features. The comparison includes
20 golden queries, seven context tasks and four impact tasks through the API.

| Aggregate | Before Hit@1 / Hit@3 / MRR | After Hit@1 / Hit@3 / MRR |
|---|---|---|
| 20 golden queries | 45% / 65% / 0.5746 | 50% / 70% / 0.6211 |
| Six positive context tasks | 33.3% / 66.7% / 0.4444 | 33.3% / 66.7% / 0.5056 |
| Two positive impact tasks | 50% / 100% / 0.7500 | 50% / 100% / 0.7500 |

The remaining three harness tasks are adversarial: unsupported-domain context,
vague impact and common-word impact all pass restraint checks. Aggregate context
must-have recall rises from 58.3% to 75%; positive impact recall remains 100%.
Golden Hit@5 falls from 80% to 75%, outside the requested Hit@1/Hit@3/MRR gate.
Six golden queries still miss at rank three; `context-006` still misses its
required file and `context-004` retains only half its required files. Therefore
**aggregate non-regression passes; perfect per-query acceptance does not**.
The captured task records and gate retain these misses rather than declaring
every expected-file assertion successful.

Reviewable new captures are [before](evidence/product-upgrade-before.json),
[after](evidence/product-upgrade-after.json) and [gate](evidence/product-upgrade-gate.json).
They retain all task inputs, ranks and returned paths, excluding source slices
and local runtime credentials. Recheck with:

```bash
python -m eval.product_upgrade_gate \
  docs/release/evidence/product-upgrade-before.json \
  docs/release/evidence/product-upgrade-after.json
```

`python -m eval.product_upgrade_capture --help` documents API recapture; select
the same corpus/index/provider in both arms and configure offline/free inference
first. The input hash and task records prevent silently changing expectations.
Standard function [complexity measurements](evidence/architecture-complexity.json)
retain the same tools and baseline for both sides.

No curated input or historical benchmark outcome was changed. New measurements
are distinct evidence. The inherited legacy golden scorer takes the best rank
from separate files/chunks channels; the new locator has one compact ranking.
That contract difference is included in the comparison and makes direct
production-quality inference inappropriate. The new path is covered separately
by dense-hit/duplicate-channel/explicit-identifier/query-intent invariants.

After incorporating current `main` and owner hardening, the candidate's full
default-profile suite passes **2080 tests, 11 optional skips**. Ruff passes and
the mypy ratchet reports zero errors across 418 source files. Public launch,
documentation and reachable commit privacy guards pass. Wheel and source
archives pass packaging checks; a clean hash-locked wheel install runs actual
stdio MCP search/context/impact for both the new default and explicit legacy
contracts, including a durable legacy artifact byte comparison. SQLite Lite
also runs installed stdio MCP without backing daemons. These are functional
checks with mock/offline providers; no semantic or coding-benefit claim follows.

A free learned-model rerun was attempted, but the cloud destination allowlist
denies Hugging Face (HTTP 403). Domain additions are saved for environment
review. A provider rerun additionally needs a secure usable credential and
confirmation of free credits; no paid calls were made. These measurements
do not reproduce the external SWE-bench numbers, semantic gains or token savings.

## Remaining hotspots and recommendation

`HybridRetrievalPipeline.run` and `ContextPackBuilder.build_context_pack` remain
large despite cleaner stage boundaries. Next candidates are policy-owned
selection, provider/deadline coordination and hydration interfaces, each with
characterization before extraction. Insight snapshot collection and runtime
slice allocation also retain several responsibilities. SQLite adapters need
explicit versioned migrations before future incompatible schema changes.

The GPU application factory largely groups configuration and distinct endpoints;
the web API module groups endpoints, and the web data module groups transforms
and loaders. Size alone did not justify changing these public surfaces during
this set. Their future work should separate cohesive data-loading contracts
only where an independently testable responsibility exists.

Keep the remaining retrieval candidate unmerged until exact-head checks pass
and the documented per-query acceptance gaps are reviewed.
Prepare one compatible release after the coherent set passes all checks and
quality limitations are reviewed. Publish only after a separate user action.
