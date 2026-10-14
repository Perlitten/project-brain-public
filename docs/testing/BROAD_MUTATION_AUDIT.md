# Authentication, retrieval and concurrency test audit

This follow-up starts at `15d549f899418ad9a369f5d0281eadce07498236`, the merge of
[PR #98](https://github.com/Perlitten/project-brain-public/pull/98).
That PR closed both pending-nonce cleanup assertion gaps: **70/71 usable mutants
detected**, one equivalent boundary, no timeouts. Its full offline run passed
2,409 tests. The broader changes are in
[PR #100](https://github.com/Perlitten/project-brain-public/pull/100).

## Confirmed defects and changes

| Behavior before the fix | Correction and regression evidence |
| --- | --- |
| Two consumers can both read an issued nonce before either deletes it, causing a `KeyError` or inconsistent consumption. Generation cleanup can also iterate a table another consumer changes. | Serialize issuance, eviction and consumption per manager. Controlled thread interleavings assert exactly one success, rejection of replay and safe generation during consumption. |
| Expired unused challenges and consumed replay markers accumulate indefinitely. | Evict both tables during issuance; retain markers through the original inclusive TTL. Tests check actual retained records at expiry and at the exact boundary. There is no new hard capacity limit on challenges issued within one TTL. |
| Concurrent successful and failed verification can publish `FAILED`, confidence zero and executive-summary permission true together. | Serialize writers per claim and test that the completed outcome fields agree. Independent claims remain independent; private locks preserve deep copy, pickle, value equality and JSON/schema behavior. |
| Two reservations each admit six bytes into an eight-byte section. Join admission can race with reservation, snapshots can observe half-written counters, and concurrent exclusions can exceed their cap. | Lock complete budget operations, including join admission/reservation and snapshots. Tests force preemption inside these operations and assert byte accounting, whole-item admission and bounded metadata. Builders remain request-owned. |
| A caller can mutate an exclusion dictionary obtained from a snapshot. | Copy exclusion records; mutate a returned snapshot and verify subsequent state is unchanged. |
| The internal key resolver treats empty input as legacy admin when the configured key is empty. Unicode legacy keys raise `TypeError`; direct minting with TTL zero creates a non-expiring key. | Reject empty input before storage, compare UTF-8 bytes and validate positive specified lifetimes. Existing HTTP production guards already rejected keyless requests; this was an internal resolver edge case, not a demonstrated HTTP bypass. |
| Nonfinite deadlines can disable request bounds. Malformed/nonfinite channel scores can throw or contaminate ranking; null lexical scores throw. Boolean source lines are accepted as integers. | Reject nonfinite deadlines before I/O, isolate invalid score rows with explicit degradation, preserve valid rows and missing-score rank fallback, and reject boolean line ranges. Both ranking modes are tested. Valid provider behavior and ranking formulas remain intact. |

Running the current bug regressions against pristine base source gives **34
failed, one passed**; the same 35 cases all pass after the fixes. These are
bug reproductions, not surviving mutations relabeled as vulnerabilities.
Exact selections and test fingerprints are retained in
[evidence](evidence/broad-regressions-before.json) and its
[matching after run](evidence/broad-regressions-after.json).

## What the tests actually establish

- Authentication uses a temporary SQLite store and local ASGI requests with
  distinct readers, writers and empty-scope credentials. Tests exercise actual
  hashed lookup, revocation, disabled principals, exact expiry, principal reuse,
  legacy compatibility and stored keys when no legacy key is configured.
- The previous unknown-key case replaced the query with a constant empty result;
  it could not detect a broken lookup predicate. That redundant assertion was
  removed. A hash self-comparison was replaced with an independent SHA-256
  oracle and a distinct-input assertion. Positive credential expiry is checked
  against an actual future interval, rather than merely being non-null.
- Retrieval tests assert output paths, ranges, degradation reasons, channel
  bounds and elapsed time. They cover missing scope, fallback identity,
  deadline boundaries before new I/O, late cleanup exceptions and cancellation
  isolation between simultaneous requests. An outer timeout turns deadline
  mutations into prompt assertion failures rather than hanging the campaign.
- Thread tests use events to pause real operations at vulnerable transitions.
  They observe completed state and return values; they do not merely assert
  that a lock method was called. Removing each critical lock is mutation-tested.

## Mutation evidence

The campaign targets seven modules: attestation verification, nonce lifecycle,
claim verification/outcome flags, UTF-8/budget operations, API authentication,
credential resolution/minting and retrieval orchestration/normalization.
`scripts/mutation_audit.py --profile broad` adds async-function discovery and
lock-removal mutations. Supporting model/lock code is regression-tested, not
directly mutated. This is a bounded campaign, not a repository-wide score.

| Source / tests | Usable | Killed | Survived | Timed out | Equivalent (within survivors) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original source / original tests | 175 | 128 | 45 | 2 | 1 |
| Same original source / strengthened tests | 175 | 174 | 1 | 0 | 1 |
| Fixed source / fresh manifest | 201 | 200 | 1 | 0 | 1 |

[Machine-readable counts, test fingerprints and failure witnesses](evidence/broad-audit-results.json)
accompany the raw manifests and individual outcomes.

Raw manifests and per-mutant outcomes are retained in `evidence/`. String
subtraction is type-invalid, so before ID 48 / fixed ID 56 is excluded from the
usable denominator while its raw failed-test outcome remains visible. Timeouts
are never counted as kills. The single equivalent is before ID 41 / fixed ID 49:
changing `max_bytes <= 0` to `< 0` still returns empty text at zero for the
UTF-8-encodable input contract.

Original timeouts were 15 seconds; final campaigns allow 30 seconds and stop on
the first assertion failure. The two original timeout mutants are also rerun
with the original tests and a 30-second cutoff. Both still time out at 30 seconds. Their strengthened tests now fail promptly
inside an outer request timeout; the cutoff change is not credited as detection.

Original-source comparisons freeze the same 176 mutation candidates and source
bytes. The fresh fixed-source campaign has 202 candidates, including expiry
cleanup and critical-section removal; its denominator is reported separately.
No non-equivalent survivor, timeout, collection error or tool error remains in
the final campaigns. Before candidates 75/83/90/91 demonstrate legacy-key,
principal-selection, expiry-boundary and key-lookup gaps; 127 detects unreaped
late exceptions. Fresh critical-section removals fail forced-interleaving tests.
These claims apply to the enumerated operators and input contracts only.

## Verification and limits

- Full local offline Python suite: **2,483 passed, 11 skipped**, one existing MCP
  deprecation warning. Seven sandbox cases, two absent editor CLIs, one unavailable
  Neo4j integration and one API-key-dependent contract remain skipped.
- `ruff check .` passes. Full `mypy --explicit-package-bases brain apps scripts`
  passes for 425 files, zero errors; its zero-error gate is unchanged.
- Initial remote CI is successful: Linux full Python/static/package/migration
  checks, Linux/Windows Lite, privacy/launch checks and web tests/typecheck/build.
  CodeQL succeeds for Python and JavaScript/TypeScript; its PR check reports
  **no new alerts in changed code**. The repository-wide alert API returns HTTP
  403 for the integration, so this does not certify the full historical alert inventory.
  Final-head check status is reported on PR #100 after the evidence commit.

The paired acceptance run uses 20 unchanged goldens and 11 context/impact tasks
against the existing frozen corpus/index, offline token-hash embeddings and
disabled chat. External socket connections are blocked. Corpus checkout:
`02af8a216bb0f05214ac838091eb944aae22133d`. No indexing, model download, remote
inference, paid call, benchmark baseline change or NVIDIA credential is needed.
These captures verify non-regression on this index, not learned semantic or
coding-outcome quality. The recomputed gate passes with no failures: golden HIT@3 remains 70%, MRR
0.6211; context required-file recall remains 0.75 and impact recall 1.0.
[Captured paths and gate results](evidence/broad-acceptance-results.json) retain
individual misses. The corpus/index already existed; rebuilding it is not part
of this test audit.

Remaining limits: no cross-process/distributed nonce protocol certification;
no stress/load or actual OS-suspend certification; no concurrent mutation of
arbitrary public model fields; no database transaction-isolation campaign;
no new certification of issuer provisioning or evidence-authority matching.
The bounded AST operators cannot represent every possible defect. Full-suite
skips and pre-existing misses in the frozen retrieval tasks remain explicit.

## Reproduce

Use the locked development environment; no mutation framework or model install
is needed. From this PR checkout, run the fresh campaign:

```bash
python scripts/mutation_audit.py \
  --manifest docs/testing/evidence/broad-fixed-manifest.json \
  --output /tmp/brain-broad-fixed --timeout 30 \
  --extra-test tests/test_auth_boundary_contracts.py \
  --extra-test tests/test_auth_failure_regressions.py \
  --extra-test tests/test_retrieval_boundary_contracts.py \
  --extra-test tests/test_retrieval_edge_regressions.py \
  --extra-test tests/test_concurrency_regressions.py
```

For the comparable pair, export base commit `15d549f` to a temporary directory.
Run the current mutation runner with `--root` pointing to that export,
`--manifest docs/testing/evidence/broad-before-manifest.json` and a separate
output directory. Original tests need no extra test arguments. For strengthened
tests, copy `test_auth_boundary_contracts.py`, `test_retrieval_boundary_contracts.py`
and `test_retrieval_deadlines.py` from this PR into that export's `tests/`, and
pass the first two as `--extra-test`. Do not copy bug-regression tests into the
unfixed runtime: they intentionally fail there without any mutation.

Recompute acceptance metrics from the captured paths, without a running service:

```bash
python -m eval.product_upgrade_gate \
  docs/testing/evidence/broad-capture-before.json \
  docs/testing/evidence/broad-capture-after.json
```

Release recommendation: hold a new release until PR #100 has been reviewed and
merged with successful final checks. This work does not publish or retag a release.
