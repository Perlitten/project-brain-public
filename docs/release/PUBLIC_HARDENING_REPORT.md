# Public hardening implementation evidence

Date: 2026-10-14. This is a corrections ledger for the supplied independent
assessment, not certification of unchanged v0.9.2 assets. Publication remains
**NO-GO** while confirmed core/recovery/distribution blockers are unresolved.

## Execution and privacy corrections

| Assessment finding | Correction / verification | Status |
|---|---|---|
| B01 fail-open laboratory | Missing/invalid/Docker-unavailable/off-without-opt-in rejected before spawn; trusted host execution requires two explicit settings; compile-only uses isolated Python | Implemented, regression tests pass |
| B02 unshare host filesystem escape | Unshare removed as a containment backend | Implemented, selection regression passes |
| B03 private Docker context | Final nested deny rules exclude env credentials, package-manager config, Lite data, virtualenv/deployment state | Real Docker scratch COPY/export excludes five marker classes; sources/templates retained |
| B04 invented autonomy | Normal API excludes autonomy; direct compatibility service blocks; operator persists blocked state without fake model usage/commit/deploy | Tests assert no completion, telemetry or manufactured evidence |
| B05 cancelled autonomy | Cancelled compatibility runs remain terminal and execute no oracle/workspace operation | Regression passes; unfinished executor excluded |
| B06 missing goal persistence | No durable GoalRun execution/recovery surface is exposed; direct submission reports unavailable | Feature excluded until an actual durable executor exists |
| B10 sibling toolchain mounts | Toolchain comes from the trusted image; only workspace bind exists | Real container cannot see outside owned sentinel; no broad host paths mounted |
| B11 native/provider redaction | Shared native/provider/header/field/URL/private-key policy used by lab and trajectory | Synthetic native/provider credentials absent; benign output retained |
| B13 confirmation failure | Enabled/capable legacy confirmation failure cancels; modern invalid responses cancel; modern client callback failure dispatches nothing | Real SDK legacy/auto tests pass |
| C04 loopback prefix | URL parsing, actual loopback IP/hostname, valid port, no userinfo | Ten endpoint table cases pass |

Full Python verification: **2,192 passed, 11 optional skips**. Ruff and mypy
passed on the source changes. Process-supervision fixtures explicitly opt into
trusted host execution; independent safety tests retain the normal fail-closed
default. Obsolete tests accepting scaffold completion were replaced with
stronger assertions against invented success. No quality gate was relaxed.

[Actual Docker probe](evidence/public-hardening/docker-safety.json) used the
already cached `python:3.11-slim` solely as a containment fixture. The default
validation image is `python:3.12-slim`; it must be provisioned by the operator.
This probe is not acceptance of the application image or its dependencies.
No model/image/dependency download, provider inference, paid call, production
data access, release publication or deployment was performed.

Remaining assessment findings are tracked through separate core-memory, Lite
queue, recovery, integration, distribution and dashboard corrections. Remote
exact-head CI, CodeQL and independent review must pass before merge.

## Repository memory and Lite recovery

| Finding | Correction / real regression | Status |
|---|---|---|
| B07 rule overwrite | Scoped deterministic IDs for new rules; existing bare IDs preserved only in their original repository. Both API/library writes and YAML sync fence lookup by scope | Real SQLite and disposable PostgreSQL schema tests pass; other repository and global values survive edits/deletion |
| C02 POSIX case collision | Case-sensitive POSIX equality in decision uniqueness, normative/learning/skill filters and context-cache keys; Windows drive/UNC identities retain case-insensitive equality | Case-distinct decisions remain separate; Windows identity and cache regressions pass |
| C03 global decisions omitted | Authorized `system_global` and legacy NULL represent global scope consistently in runtime and legacy context selection; relevance still excludes unrelated policy | Relevant global policy reaches two repositories; private policy stays scoped |
| B09 exhausted Lite leases | Recovery/claim atomically fail exhausted jobs and clear owner tokens; retries cannot exceed max_attempts | Real SQLite expiry, eight-way ownership contention and actual worker-kill/restart tests pass |
| B12 Lite backpressure | Typed exception contains depth/limit; rejection rolls back without adding a job | Actual SQLite enqueue returns HTTP 429 and Retry-After via the production exception handler |

Full default-profile suite after source corrections: **2,210 passed, 11 optional
skips**. Eighteen storage/queue cases (including two subsequently added YAML
regressions) also pass. Ruff, zero-error mypy and migration reapplication pass.
PostgreSQL fixtures create/drop only UUID-named disposable schemas; Lite
fixtures use temporary directories. The crash probe kills only its own child
and waits for a real one-second lease expiry, rather than injecting expiry.
Queue recovery regressions also run in Windows/Linux Lite CI.

These fixes prevent future corruption. They cannot reconstruct repository
memory already overwritten by the old implementation; recover such records
from a trusted backup or their original evidence. No existing IDs are retagged,
no production data is migrated here and no historical release asset is changed.

### Safety review corrections

The first independent review found four further boundary cases. Docker tools
now resolve only inside the image; preflight checks required tools and declared
project dependencies using isolated image Python without importing repository
code. Incompatible images report `infrastructure_error`, execute no candidate
commands and provide no experiment test evidence. Basic/other Authorization
fields and incomplete PEM blocks are redacted; full output is redacted before
snippet truncation. Six new cases pass, and the full suite passed **2,198 tests
with 11 optional skips** on these corrections. Ruff and mypy pass.

The [updated real container probe](evidence/public-hardening/docker-safety-review.json)
confirms outside reads and writes fail. The cached bare Python image's standard
pytest profile is rejected as infrastructure unavailable, while compile/code
containment succeeds. Provision a trusted image with pytest and the candidate's
declared dependencies for standard testing; a bare Python image is suitable
only for compile-only profiles. No automatic download or host mounts supply
missing tools.
