# Team mode: current status

Snapshot: 2026-10-17, `main` at `2e66c66` (after PR #122). Update this file in
the same PR as any team-mode change; it is the one place that answers "what
stage is team mode at". The contract is the [ADR](../adr/team-collaboration.md);
the per-requirement evidence is the [foundations matrix](team-foundations.md).

## In one paragraph

The core of **M1** (identity, membership and fail-closed authorization) is on
`main`: a solo Brain is a team of one, and adding the first verified member
switches repository-scoped API, MCP and dashboard requests to per-project
authorization. M1 is not complete while the gaps below remain, including web
logs, downloads and notifications that still use the server key. Execution
(M2) and everything after it has not started. Team mode is **not deployed** and
has **not been tried with real people**; green CI is the only evidence so far,
and the ADR says plainly that green CI is not team readiness.

## Stages

| Stage | Scope | Status |
|---|---|---|
| M0 | Contract, permission/revocation semantics, coverage matrix | Done |
| M1 | Verified identity, membership, centralized authorization, provisioning, invitations, restore fence | Core implemented; known authorization gaps remain; not deployed |
| M2 | Bounded real executors, tool/secret/egress/budget capabilities, revocation checkpoints | Not started; execution is denied |
| M3 | Subtasks, coordinator, overlap detection, integration queue | Not started |
| M4 | Governed versioned memory, independent review | Not started |
| M5 | Invitation/board/conflict UX, cloud packaging, upgrade/restore diagnostics | Not started (only the admin Team page exists) |
| M6 | Pilot with 3–5 people on two projects | Not started |

## Public label

Team mode may be announced only as an **experimental preview: `main` only, not
in v0.9.2**, never as generally available. The operator path (init, attach a
repository, invite, dashboard sign-in, MCP, revoke) and the user-facing known
limitations are in the [team mode walkthrough](../TEAM.md); keep its
limitations in step with the known gaps below. The security reporting scope
is in [SECURITY.md](../../SECURITY.md#team-mode-experimental-preview).

## Shipped in M1

| What | PR |
|---|---|
| Gated project authority, durable task/run foundations | [#99](https://github.com/Perlitten/project-brain-public/pull/99) |
| `brain team` CLI and `team:admin` API; OAuth clients bound to members; principal-attributed memory writes | [#111](https://github.com/Perlitten/project-brain-public/pull/111) |
| Unified model: no team switch, `team_state()` = `solo` / `team`, one MCP server | [#112](https://github.com/Perlitten/project-brain-public/pull/112) |
| Repository listing, diff-review results and context packs scoped by membership | [#113](https://github.com/Perlitten/project-brain-public/pull/113), [#118](https://github.com/Perlitten/project-brain-public/pull/118) |
| Registry and global routers authorized per repository; administrative routers admin-only | [#114](https://github.com/Perlitten/project-brain-public/pull/114), [#116](https://github.com/Perlitten/project-brain-public/pull/116) |
| Queued jobs record their initiator and are re-authorized before execution | [#115](https://github.com/Perlitten/project-brain-public/pull/115), [#117](https://github.com/Perlitten/project-brain-public/pull/117) |
| Single-use invitations (invited → verified → active → revoked), API and CLI | [#119](https://github.com/Perlitten/project-brain-public/pull/119) |
| Remaining personal routes scoped in team state | [#120](https://github.com/Perlitten/project-brain-public/pull/120) |
| Dashboard members sign in with their own key; admin Team page (add, role, revoke) | [#121](https://github.com/Perlitten/project-brain-public/pull/121) |
| A team snapshot restored with `scripts/restore_database.py` stays fenced until `brain team reconcile --attest`; plain `psql` does not add the fence ([runbook](../runbooks/backup-restore.md#restoring-a-team-brain)) | [#122](https://github.com/Perlitten/project-brain-public/pull/122) |

Tests: 12 `tests/test_team_*.py` files, about 90 test functions, plus the
PostgreSQL-only restore and concurrency cases that CI runs.

## Known gaps (checked against `main` on 2026-10-17)

- **Not deployed.** Production runs a release from before team mode.
- **No SSO/OIDC.** "Verified" means possession of an invitation token for an
  identity the operator chose; the email is never checked
  ([TEAM_INVITATIONS.md](../operations/TEAM_INVITATIONS.md)).
- **No invitation UI.** Invitations exist only in the API and CLI; the
  dashboard Team page adds members directly and shows their key once.
- **`brain team member add --mint-key` keys cannot use the dashboard.** The CLI
  grants only `team:*` scopes (`brain/team/provisioning.py::member_scopes`),
  while dashboard keys also need `DASHBOARD_SCOPES`
  (`apps/web/lib/team-scopes.ts`).
- **MCP resources `brain://rules` and `brain://decisions` fail for
  non-admin members.** They request the unfiltered lists
  (`apps/mcp_server/protocol.py`), which require a team administrator in team
  state.
- **Invitation acceptance throttle is per process**
  (`apps/api/routers/team_invitations.py`); correct only with one API process.
- **Web app logs, downloads and notifications** are still reached with the
  Next.js server key and are not scoped per member.
- **`brain/workspace` and `brain/team` are not merged**: repository trust and
  human membership are still two separate models.
- **Not exercised end to end**: production upgrade/restore, external identities
  and secrets, real multi-person use.

## Next steps, in order

1. Close the small M1 gaps above (CLI key scopes, MCP resources, invitation UI).
2. Deploy to production in solo state and confirm nothing changed for the owner.
3. Add one real member on one project and walk the invite → sign-in → revoke path.
4. Then complete M2 through M5; run the M6 pilot only after those prerequisite
   stages can exercise coordinated runs, overlaps, revocations and crashes.
