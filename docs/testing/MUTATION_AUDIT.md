# Test quality and bounded mutation audit

Audited from `main` commit `3c4449e37909541496a5d995b6035a6129c82c10`.
Verification used Python 3.12.14, local fixtures and mock providers. No model
downloads, remote inference, paid calls or NVIDIA credential dependency.

## What the tests actually proved

| Existing example | What it verified | Action |
| --- | --- | --- |
| UTF-8 truncation loop | Determinism and an upper bound; returning empty text could pass | Added exact-fit, useful prefix, Unicode and suffix examples; empty-output mutants now fail |
| Remote HMAC drill | Correct default-key signature and one tampered output digest | Added wrong scheme/key/challenge, altered execution fields, missing credentials and malformed signature cases |
| Nonce replay examples | Immediate replay rejection | Added independent expiry, TTL boundary, wall-clock rollback, clock origin and repeated RNG output |
| Section accounting example | One write, with `sections <= cap` | Added exact byte accounting, global/section interaction and repeated writes/joins |
| “Symlink artifact escape” / “cross-repo evidence leakage” | Placeholder classification of `/etc/passwd` / `unknown` | Renamed accurately; neither establishes containment or repository isolation |
| Seven “attack” examples | Duplicate `TODO`, `unknown` or `placeholder` checks | Removed duplicates; those values remain covered |

`test_property_and_mutation.py` contains three example regressions, not property
generation or mutation testing. Its description now states this. Other legacy
scenario names still exceed their assertions; the suite header explains its limits.

## Bugs corrected

1. A forged `hmac-sha256:` prefix passed `verify_integrity()` and could make a
   report claim `VERIFIED`. Signed integrity now requires an explicit key and
   expected challenge, followed by cryptographic verification. Ordinary self-hash
   checking and the existing signing/verification helper signatures remain usable.
   The claim API accepts and passes through those explicit credentials.
2. Non-ASCII signatures raised `TypeError` in `compare_digest`. Malformed signatures
   now fail closed before comparison.
3. Wall-clock rollback extended nonce lifetime. Expiry now uses monotonic time;
   the existing inclusive TTL boundary is preserved. Linux uses `CLOCK_BOOTTIME`
   to include suspend time; unavailable/unsupported clocks use monotonic fallback.
4. A nonce issued at clock value zero was mistaken for an unknown nonce. Absence
   now checks `None` explicitly.
5. Repeated section writes reset the allocation. Writes and ordered joins now
   use the remaining section allowance; joins retain whole admitted items.
6. Failed re-verification left a claim's earlier `VERIFIED` status, confidence and
   executive-summary permission intact. Every outcome now updates those fields.

The new bug regressions produced **8 failures / 4 passes before fixes**, then all
passed. Provider adapters, model selection and benchmark baselines are unchanged.
The stale-claim regressions separately failed all four scenarios on the original
base commit and passed after correction. Review also added successful signed-claim,
suspend/fallback, manifest-mismatch and one-sided missing-evidence checks.

## Mutation evidence

The dependency-free runner changes comparison boundaries, Boolean decisions,
arithmetic, min/max limits, rounding, clock reads, prefix checks, empty text
returns and state updates in isolated temporary copies. It never mutates the
working checkout. The manifests enumerate every generated site and replacement.
Normal campaigns reject mismatched source hashes before any tests run. Cross-version
replay requires `--replay` and records the actual hashes and mismatched source files.
Published commands normalize the interpreter name rather than retaining host paths.

| Source and tests | Usable mutants | Test failures | Survivors | Timeouts | Sites removed by fixes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original source, original tests | 53 | 17 | 34 | 2 | 0 |
| Same original source, strengthened tests | 53 | 49 | 4 | 0 | 0 |
| Fixed source, replay of original manifest | 53 | 41 | 3 | 0 | 9 |
| Fixed source, fresh manifest including claim verification/new guards | 71 | 68 | 3 | 0 | 0 |

Raw manifests contain 54 / 72 mutations. One string-subtraction mutation is
type-invalid and excluded from each behavioral comparison, with its raw outcome
retained. Timeouts and removed sites are **not** counted as test failures.
The strengthened runs put contract tests first and stop at the first failure;
the two original timeout cases then fail promptly on truncation assertions.
Fresh-manifest totals are separate from the comparable original-source pair.
That pair mutates three modules; claim verification and the suspend-aware clock
selector are added to the final fresh campaign, not retroactively scored.

Concrete examples: original mutants 1/3 accepted wrong signature schemes or
challenges; 22 changed rounding up to rounding down; 30 discarded exact-fit text;
38 excluded an ordered join at its exact limit. All survived the original suite
and failed the strengthened tests. A further repaired-code survivor allowed a
missing key with a supplied challenge; the added case now detects that mutant.
The expanded claim scope exposed acceptance when IDs were absent but attestations
were supplied. Independent missing-ID / missing-attestation cases now detect it.

### Surviving mutations and remaining gaps

- Original 27 / repaired 41: `<= 0` becomes `< 0`. At zero both paths return
  empty text, even with arbitrary suffixes. This is equivalent.
- Original 10/17 / repaired 18/26: omit deleting an issued entry after consumption
  or expiry. Accept/reject behavior is preserved, including replay and repeated
  RNG output, but retained entries change memory use. Resource lifecycle is an
  explicit remaining coverage gap; these are not unconditional equivalents.
- The old HMAC `return True` survivor was unsafe behavior, not a missing assertion
  requiring acceptance of signed evidence without credentials. The fix removed it.

## Scope, verification and reproduction

Mutation scope is **attestation integrity/HMAC verification, `RemoteNonceManager`,
claim verification/outcome flags, UTF-8/token helpers, `ContextBudget`, and
`bounded_join`**. `BudgetedPayloadBuilder`
is exercised by existing tests but is not mutated. Hash serialization, signing,
authentication routes, filesystem containment, retrieval deadlines, ranking,
provider behavior and concurrency were not mutation-audited. The explicit AST
operators are bounded coverage, not a claim about all possible bugs or the whole
repository. A valid self-hash does not establish a trusted issuer. Evidence-authority
matching and trust provisioning are outside this patch. Suspend is simulated through
clock readings; real OS suspend and suspend behavior without `CLOCK_BOOTTIME` are
not certified by these tests.

Latest full local Python regression: **2,404 passed, 11 skipped, one MCP deprecation
warning**. Skips comprise seven sandbox cases, missing Claude/Cursor CLIs,
unavailable Neo4j integration and an API-key-dependent contract case. Ruff and
the full mypy ratchet passed with zero errors; public-launch and documentation
consistency checks passed. These results do not certify live providers or skipped
integrations. See [machine-readable evidence](evidence/mutation-results.json) for
commands, source hashes, per-mutant outcomes, failure witnesses and exact skips.
Two missing-evidence cases were added after full-suite collection; the final
focused run, including them, passed all 60 cases. The CI privacy failure from host
paths in the first published evidence was corrected without changing its denylist.

With the project's locked development environment installed, reproduce the final
campaign without installing a mutation framework:

```bash
python scripts/mutation_audit.py \
  --manifest docs/testing/evidence/mutation-repaired-manifest.json \
  --output /tmp/brain-mutation-results \
  --extra-test tests/test_mutation_contracts.py \
  --extra-test tests/test_mutation_bug_regressions.py
```

For the comparable original-source run, use a checkout/copy of the base commit,
copy `test_mutation_contracts.py` into its tests directory and pass that copy as
`--root` with `mutation-before-manifest.json`. Omit `--extra-test` for the original
test selection. The original run used all tests before stopping on failure;
reproduction with the optimized runner may classify a hang as an earlier failure.
