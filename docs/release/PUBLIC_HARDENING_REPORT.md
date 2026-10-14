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
