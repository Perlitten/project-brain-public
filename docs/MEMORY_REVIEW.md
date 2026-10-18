# Governed memory: proposals, review and the personal tier

> Experimental, team state only. Solo Brains (a team of one) are unchanged:
> the owner writes decisions, rules, learnings and skills directly, and
> consolidation keeps its solo evidence gates.

In a team, shared memory is what every agent of every member receives in
context packs, search, rule checks and the MCP `brain://decisions` and
`brain://rules` resources. Governed memory keeps that set to knowledge that an
independent reviewer approved:

```
member or agent ──propose──▶ pending proposal ──approve (Reviewer, not the author)──▶ approved decision / rule / learning
                                   │
                                   ├──reject──▶ closed, nothing written
                                   └──withdraw (author)──▶ closed, nothing written
```

Code: `brain/memory/proposals.py` (service), `apps/api/routers/memory_review.py`
(HTTP), `apps/mcp_server/team_server.py` (MCP tools), `apps/web/app/review/`
(dashboard). Tests: `tests/test_memory_proposals.py`.

## Who may do what

| Action | Team state | Solo state |
|---|---|---|
| Write a decision or rule directly | Team administrators only | The owner, as before |
| Write a shared learning directly | Team administrators only; a member's learning is personal by default, and `"visibility": "shared"` proposes it | The owner, as before |
| Create an active skill | Team administrators only; a member's `POST /skills` waits as `pending` | The owner, as before |
| Propose a decision or rule | Contributor or Reviewer on the repository's project; a system-global item needs Contributor or Reviewer on some project | Allowed (the owner may approve it), but never needed |
| List a review queue, read a proposal | Reviewer on the repository's project; team administrators for system-global items and every scope | The owner |
| Approve or reject | Reviewer on the project who is **not the author**; system-global items: a team administrator who is not the author | The owner |
| Withdraw | The author, while pending | The owner |
| See your own proposals | Every member (`mine=true`) | The owner |

Readers cannot propose. Every check runs before any proposal row is read, so a
member who may not review a scope learns nothing about its queue (403), and a
proposal outside their reach answers 404.

## Writing in team state

`POST /decisions` and `POST /rules` (and the MCP `record_decision` /
`record_rule` tools, which call them) keep working for every member who may
propose. For a member who is not a team administrator they no longer write the
canonical row: they create a pending proposal and answer **HTTP 202**:

```json
{"status": "proposed", "proposal_id": 12, "version": 1, "state": "pending", "proposal": {...}}
```

Team administrators keep writing directly (HTTP 200). `POST /decisions/proposals`
and `POST /rules/proposals` always propose, for anyone allowed to.

**Edits are proposals too.** A proposal with the same title (decision) or rule id
(rule) and scope as an approved item is an edit of it. The proposal records the
item's version at the time (`base_version`); pass `expected_version` to fail
fast with 409 if the item already moved on. Approval writes the edit with that
version as a compare-and-swap, so an item that changed after the proposal was
made refuses the stale proposal with 409 and the proposal stays pending; the
author withdraws it and proposes again from the current version.

## Drafts never reach agents

Proposals live in their own table (`memory_proposals`). No retrieval path reads
it: context packs, search, `GET /decisions`, `GET /rules`, rule checks in
`review_diff`, the MCP `brain://decisions|rules` resources and learning
retrieval only see approved rows. The only way to read a draft is to ask for
proposals explicitly (`GET /memory/proposals`), which the rules above limit to
the author and the reviewers of that scope.

## Reviewing

| Endpoint | Purpose |
|---|---|
| `GET /memory/proposals?repo_path=…` | Pending proposals of one repository (Reviewer) |
| `GET /memory/proposals?scope=system_global` | Pending system-global proposals (team administrator) |
| `GET /memory/proposals` | Every scope (team administrator) |
| `GET /memory/proposals?mine=true&state=all` | Your own proposals in every state |
| `GET /memory/proposals/{id}` | One proposal (author or reviewer of its scope) |
| `POST /memory/proposals/{id}/approve` | `{"expected_version": n, "reason": "…"}` |
| `POST /memory/proposals/{id}/reject` | `{"expected_version": n, "reason": "…"}` |
| `POST /memory/proposals/{id}/withdraw` | `{"expected_version": n}` (author) |

Every decision carries the proposal's `version` as a compare-and-swap. A
proposal that someone else already decided, or that changed since the reviewer
read it, answers **409**. Self-approval answers **403**, whatever the reviewer's
role.

**Provenance.** A new decision, rule or learning names the proposal's author
as its author and the reviewer as its approver (`approved_by_principal_id`).
An approved edit keeps the item's recorded author (an unattributed legacy row
stays unattributed), records the proposal's author as the editor
(`updated_by_principal_id`) and the reviewer as the approver. A direct write
(a team administrator, or solo state) records no approver, and a later direct
edit clears it: the approver always names who reviewed the current version.
An approved skill records its approver the same way. The store's supersede history keeps
the previous version of an edited item. The proposal row is never deleted and records who approved or
rejected it, when, why, and which item and version it produced (`result_key`,
`result_version`).

**Surfaces.** The same endpoints back the MCP tools `team_propose_decision`,
`team_propose_rule`, `team_list_proposals`, `team_approve_proposal` and
`team_reject_proposal` (HTTP-backed bridge and native `/mcp`), and the
dashboard **Memory → Review** page, which lists the queues you review and your
own proposals. The page does not offer Approve on your own proposals; Brain
refuses it anyway.

**Notifications.** Every submission, approval and rejection appends a
`memory.proposed`, `memory.approved` or `memory.rejected` event to the team
activity feed (`GET /team/activity?kind=memory`, the dashboard **Activity**
page). The event names the proposal (`details.proposal_id`, `proposal_kind`,
`scope`) and never carries its draft text. A repository-scoped proposal's
events are visible to that repository's readers, its reviewers among them; a
system-global proposal's events to team administrators. `GET /team/me` reports
`pending_review_count`: the pending proposals the caller could decide on,
leaving out their own (solo state: every pending proposal; a team
administrator: every scope; a member: the repositories of projects where they
hold `knowledge.approve`).

## Revoked authors

- Revoking a member's team membership (`brain team member revoke`, the Team
  page or the API) **withdraws all of their pending proposals** in the same
  transaction, with the reason "Author access revoked". Nothing they proposed
  reaches approved memory after they leave.
- Approval also re-checks the author: a proposal whose author is no longer a
  live team member cannot be approved (409), even if it was left pending some
  other way.
- Knowledge that was approved before the revocation stays approved, with the
  author or editor still on record.
- Removing only one **project role** leaves the member's proposals pending; a
  Reviewer may still approve or reject them, or the author withdraws them.

## Personal tier

A member's own notes are private learnings:

- `POST /learnings` with `"visibility": "personal"` stores a learning that only
  its author sees (`GET /learnings?visibility=personal`). It needs a named
  credential and, in team state, read access to the scope. Readers may keep
  personal notes too.
- In team state a member who is not a team administrator gets a personal
  learning when `visibility` is omitted. `"visibility": "shared"` stores it
  personal **and** files its promotion proposal in one call (HTTP 202 with
  `proposal_id`); it needs Contributor or Reviewer on the scope (a learning
  without a repository: on some project, and a team administrator approves
  it). Team administrators and solo owners still write shared learnings
  directly (HTTP 200).
- Personal learnings never feed shared retrieval, context packs or another
  member's listings.
- `POST /learnings/{id}/promote` shares one. In team state it creates a pending
  learning proposal (HTTP 202) that a Reviewer of its scope approves, which
  makes the learning active shared knowledge. The author needs Contributor or
  Reviewer on the scope; a learning can be promoted once (409 on repeat), and
  only by its author (404 for anyone else). In solo state the owner's learning
  becomes shared at once.

## Skills

In team state a member's `POST /skills` stores the skill as `pending` and
answers **HTTP 202** (`"status": "pending_review"`): it is not matched or
injected into context until a team administrator approves it with
`POST /skills/{id}/approve` (the author may not approve their own). Team
administrators and solo owners create active skills directly.

## Consolidation (L1 → L3 → L4)

The consolidation pipeline ([lesson protocol](MEMORY_LESSON_PROTOCOL.md))
keeps its evidence gates G1–G4; in team state it no longer writes shared
memory on one person's word:

- **G2 counts distinct principals.** Auto-promotion in team state needs the
  evidence of at least two distinct principals
  (`MEMORY_CONSOLIDATION_MIN_DISTINCT_PRINCIPALS_TEAM`, default 2; values
  below 2 are raised to 2). Evidence from nobody (no attributed principal) is
  still rejected.
- **Otherwise it proposes.** A cluster that meets the event and evidence-level
  gates but not the principal floor writes no learning: its episode waits as
  `proposed` on a learning proposal authored by the principal behind most of
  the evidence, filed in the same transaction. Approving it (a Reviewer of the
  scope who is not that author) writes the active learning with author and
  approver and marks the episode `promoted`; rejecting it marks the episode
  `rejected`. Run reports list such clusters under `proposed`. High-severity
  candidates still go to the episode approval queue (G4) first.
- **Rule and decision projection proposes.** In team state the rule or
  decision a promoted learning implies is filed as a proposal (authored by the
  lead evidence principal) instead of written canonically.
- **Skill distillation is gated the same way.** A distilled skill is active
  only when the learnings behind it come from at least two distinct
  principals; otherwise it is `pending` for a team administrator.
  `MEMORY_CONSOLIDATION_REQUIRE_APPROVAL=true` keeps every distilled skill
  pending, as before. Solo state keeps its rules unchanged.

## Not governed (known limitations)

- The local stdio MCP server and the `brain` CLI are operator surfaces on the
  server and write directly.
- Consolidation episodes routed to G4 approval (`POST /episodes/{id}/approve`)
  are decided by a team administrator, outside the proposal queue.
- A revoked member's personal learnings stay stored and invisible to everyone.
