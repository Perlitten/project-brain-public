# Behavioral regression hardening

This change improves authentication, OAuth and API authorization tests and
replaces vacuous drift checks. Production Python source, benchmark inputs,
release tags and archives are unchanged. It does not establish the separate
70% whole-package mutation goal.

## Concrete detection improvements

- Real SQLite credential round trips verify principal kind/organization,
  minimum lifetime, successful-use persistence, revocation and scope direction.
  Three key-blind fake-session cases are removed.
- OAuth storage cases verify client isolation, exact access/refresh expiry,
  identity continuity across rotation, paired revocation and first revocation
  timestamps. The PostgreSQL overlap case uses public row-lock contention
  instead of patching a private OAuth helper.
- Team cases retain ORM objects across reused sessions to catch stale authority
  and deny a live credential whose grants were removed despite its valid role.
- The previous two API scope gates checked **zero routes** under deferred router
  inclusion. Their replacements use OpenAPI and real declared dependencies in
  handler-free HTTP probes: 203 protected operations, 89 write operations and
  four intentionally read-only POSTs. New published operations default to
  protected. Separate team, bridge and webhook policies are explicitly listed
  and retain dedicated tests. No private Brain closure is inspected.
- Copied aggregate route declarations are deduplicated while preserving the
  more complete inherited dependency policy; ambiguous policies fail explicitly.
  Read grants are discovered through the public principal scope protocol, then
  the assertions use a real persisted credential and the original scope method.
  A new-domain POST negative control catches the previous masking by a read
  dependency even when the domain has no GET endpoint. Both controls fail
  against the earlier gate and pass after the fix; see
  [paired control evidence](evidence/project-route-review-controls.json).
- Two drift self-scans that could accept readiness fallback or empty lists are
  replaced by a controlled repository requiring two distinct findings, correct
  evidence and stable identity when source lines move. A version-floor
  placeholder is removed; existing release identity contracts remain.

## Measured mutation evidence

These are independent local pairs, not an aggregated project-wide score:

| Module | Before killed / usable | After killed / usable | Score before → after |
| --- | ---: | ---: | ---: |
| `brain/auth/principals.py` | 47 / 58 | 57 / 58 | 81.03% → 98.28% |
| `brain/auth/oauth_store.py` | 40 / 93 | 80 / 93 | 43.01% → 86.02% |
| `apps/api/auth.py` | 29 / 55 | 43 / 55 | 52.73% → 78.18% |

All initial survivors in these scopes are classified, with no lost kills or
timeouts. The **64 additional type-valid detections** concern observable
behavior. The API module stops below 80% under the requested no-vanity exception:
its twelve remaining non-equivalent changes only alter diagnostic capitalization
or XX decoration. They stay in the denominator; exact spelling assertions were
not added solely to reach the target.

A separate type-valid `/index` wiring fault removes scope enforcement while
retaining key authentication. Both old route gates pass; both replacements fail.
This supplemental configuration mutation is not added to the frozen
function-body mutation score. No production vulnerability is claimed fixed.

[Raw evidence, individual decisions, scope limitations, runtimes and reproduction commands](https://github.com/Perlitten/project-brain-public/blob/d587200046b9ba3009c73f768e988e6b284de5c4/docs/testing/PROJECT_MUTATION_CAMPAIGN.md)
are maintained in the separate campaign PR #108. The seven measured local runs
took 43.10 minutes, excluding the still-running hosted full-package campaign.

## Validation and remaining work

This standalone patch passes **2,545 tests, 11 skipped** with mock providers,
owned PostgreSQL/Redis/Neo4j fixtures and the team PostgreSQL race/restore tests
opted in. Ruff passes; the unchanged mypy gate reports **0 errors / 434 files**.
Net application case growth is three. The same production source passes the
unchanged 31-task offline retrieval gate: all task records match except timing.
The combined campaign branch additionally runs nine tooling integrity cases.

The skips cover seven sandbox cases, two unavailable editor CLIs, one unavailable
Neo4j integration and one API-key-dependent contract. No models were downloaded,
no remote inference was requested, and verification did not depend on
`NVIDIA_API_KEY`. Mock/disabled/local-token-hash verification does not certify
learned-model performance.

Full-package aggregation, remaining module survivor review and the overall
70% / per-security-module 80% acceptance remain pending in #108. Its raw artifact
downloads currently require the saved cloud network allowlist to be published.
No merge is permitted before review and CI pass; no release or tag change is
part of this PR.
