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

### Safety review follow-up

The second review's six findings are corrected: mandatory image preflight
ignores optional tools, dependency markers are evaluated in the sandbox,
isolated-mode rewriting applies only to Python, accepted executable aliases
resolve to the same image tool that was probed, and the disabled autonomy CLI
returns `BLOCKED` with a nonzero exit without executing or creating state.
Runtime log exclusions now preserve source directories named `logs`.

The [real Docker context export](evidence/public-hardening/docker-context-review2.json)
retains `apps/web/app/logs/page.tsx`, excludes ten private marker classes and
has the same source content digest before and after filtering. No image or
dependency download was needed. JSON redaction also preserves unrelated fields
and valid syntax, and a real legacy SDK confirmation timeout dispatches no
write. Focused verification passed **82 tests**; full Python verification passed
**2,217 tests with 11 optional skips**. Ruff, mypy, public launch and current
documentation checks passed. These results apply to the safety branch; they do
not certify the unfinished recovery, distribution or dashboard work.

The next review's six findings are also corrected: lab snippets keep valid JSON
without a second plaintext regex; common camelCase and AWS credential fields
are redacted; metadata and requirement parsing happen only in the image, with
Python 3.10-compatible image parser fallback; pytest preflight includes declared
dev/test extras; workspace modules are not mistaken for image tools; and a
successful retry clears only the prior infrastructure uncertainty. Existing
lab redaction markers remain compatible. Full verification passed **2,226 tests
with 11 optional skips**, and Ruff, mypy and public launch checks passed.
The [actual optional-tool container probe](evidence/public-hardening/docker-optional-tool-review3.json)
passes required isolated compilation with an accepted Python alias while a
missing optional Ruff tool fails without invalidating the required result.

The following review identified five additional edge cases. Common secret-key
fields are now redacted; Docker retains the image's PATH; a retry replaces
attempt-scoped network uncertainty and recognizes `enforced:docker`; optional
test credit requires successful individual optional results; and malformed
candidate metadata is a candidate rejection rather than a retryable image
failure. Focused regressions passed **108 tests**, and the full safety suite
passed **2,234 tests with 11 optional skips**. Ruff, mypy and public launch checks
passed. Fresh remote validation remains required before merging this branch.
