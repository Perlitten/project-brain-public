# ADR 010: `master` is the canonical Project Brain lineage

## Status

Accepted — 2026-07-23

## Context

GitHub still advertised `main` as the default branch while the deployed and
actively developed Project Brain lineage was `master`. The branches had diverged
by 17 commits each. This made new pull requests and the default `review_diff`
base capable of targeting an obsolete repository history.

Before cleanup, the complete repository refs were captured in
`project-brain-pre-lineage-cleanup-2026-07-23.bundle` and verified with
`git bundle verify`.

- Bundle SHA-256:
  `34231F41117FA0F203EBFCB0A93422FC7864F888487A16E932562F62610E4E8E`
- Archived `main` head:
  `f3c8bb5059cdf5da4626366d1b36fc918d30bf02`
- Canonical `master` baseline:
  `958a7c50bd536235453cfae6baf4c52cb15f79ab`

## Classification of the retired lineage

The old line was inspected feature by feature rather than cherry-picked:

| Old slice | Decision |
| --- | --- |
| Free-form vector agent memory | Retired. Typed repository-scoped decisions, rules, context manifests and the append-only harness ledger provide explicit authority and avoid turning agent claims into shared instructions. |
| In-process scheduler, file watcher and generic git webhook | Superseded. Production orchestration is n8n → authenticated job API → Redis worker. A process-local watcher would duplicate work and make reindex cost nondeterministic. |
| Persistent inventory “audit waves” | Not transplanted. Git history, `IndexingRun`, reports, embedding verification and evidence-bound proactive insights already cover the useful signals. Any future snapshot feature must be repository-scoped and bind every result to an exact commit/artifact digest. |
| Prometheus middleware and in-memory rate limiter | Not transplanted from the old branch. Current API-key/dashboard-session boundaries, nginx, worker health and deployment checks are the active security/operations design. Metrics or distributed rate limiting require a fresh bounded design, not resurrection of incompatible middleware. |
| Dashboard actions, mobile layout and status feedback | Superseded by authenticated CSRF-protected dashboard actions, job polling, responsive templates and the current observatory views. |
| April prototype UI, CORS, N+1 and credential branches | Historical prototypes already merged into the retired line or replaced by the current implementation. |

No unique production feature from the retired line remains merge-ready. The
bundle is the recovery boundary; remote branches are no longer the archive.

## Decision

1. `master` is the only canonical and default GitHub branch.
2. CI triggers only for `master`.
3. Omitted diff-review bases resolve from `refs/remotes/origin/HEAD`; they are
   never hard-coded to `main`.
4. The obsolete `main` lineage and its closed-PR branches may be deleted after
   bundle verification.
5. Historical code may be consulted as evidence, but reintroduction requires a
   new task contract and current-architecture implementation.

## Consequences

- New PRs target the deployed lineage by default.
- Agents review the correct repository base across `main`- and
  `master`-based projects.
- GitHub branch clutter is removed without losing recoverability.
- Old implementations cannot silently regain normative authority.
