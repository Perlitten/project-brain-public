# Governed memory: proposals, review and the personal tier

> Experimental, team state only. Solo Brains (a team of one) are unchanged:
> the owner writes decisions, rules and learnings directly.

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
as its author. An approved edit keeps the item's recorded author (an
unattributed legacy row stays unattributed) and records the proposal's author
as the editor (`updated_by_principal_id`). The store's supersede history keeps
the previous version of an edited item. The proposal row is never deleted and records who approved or
rejected it, when, why, and which item and version it produced (`result_key`,
`result_version`).

**Surfaces.** The same endpoints back the MCP tools `team_propose_decision`,
`team_propose_rule`, `team_list_proposals`, `team_approve_proposal` and
`team_reject_proposal` (HTTP-backed bridge and native `/mcp`), and the
dashboard **Memory → Review** page, which lists the queues you review and your
own proposals. The page does not offer Approve on your own proposals; Brain
refuses it anyway.

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
- Personal learnings never feed shared retrieval, context packs or another
  member's listings.
- `POST /learnings/{id}/promote` shares one. In team state it creates a pending
  learning proposal (HTTP 202) that a Reviewer of its scope approves, which
  makes the learning active shared knowledge. The author needs Contributor or
  Reviewer on the scope; a learning can be promoted once (409 on repeat), and
  only by its author (404 for anyone else). In solo state the owner's learning
  becomes shared at once.

## Not governed (known limitations)

- Shared `POST /learnings` and the consolidation pipeline (L1 → L3) write
  learnings directly; consolidation has its own evidence gates
  ([lesson protocol](MEMORY_LESSON_PROTOCOL.md)).
- The local stdio MCP server and the `brain` CLI are operator surfaces on the
  server and write directly.
- A revoked member's personal learnings stay stored and invisible to everyone.
