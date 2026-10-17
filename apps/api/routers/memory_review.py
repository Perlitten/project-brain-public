"""Governed memory over HTTP: proposals, the review queue and the personal tier.

Transport for ``brain/memory/proposals.py``. In team state a decision or rule
written by anyone but a team administrator becomes a pending proposal
(``POST /decisions`` and ``POST /rules`` answer HTTP 202 with the proposal);
an independent reviewer approves or rejects it here. Solo state never routes
writes through proposals: the owner writes approved knowledge directly.

Who may do what (team state):

* propose: ``knowledge.propose`` on the item's repository (Contributor or
  Reviewer); a system-global item needs ``knowledge.propose`` on some project.
* list the queue / read a proposal: Reviewer on the repository's project
  (``knowledge.approve`` in the caller's authority) or a team administrator for
  system-global and unattached scopes, checked before any row is read. Every
  member may list and read their own proposals (``mine=true``).
* approve / reject: Reviewer on the project, never the author; system-global
  and unattached scopes: a team administrator who is not the author.
* withdraw: the author.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.api import team_access
from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.schemas import DecisionCreate, RuleCreate
from apps.api.team_access import repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.memory import proposals
from brain.memory.provenance import MemoryVersionConflict, SelfApprovalError
from brain.memory.repo_scope import normalize_repo_scope
from brain.team.authorization import Authority, Permission

router = APIRouter()
READ = [Depends(require_api_key), Depends(require_scope("core:read"))]
WRITE = [Depends(require_api_key), Depends(require_scope("core:write"))]


class ProposalDecisionRequest(BaseModel):
    # Compare-and-swap on the proposal: the version last read; 409 when someone decided first.
    expected_version: int = Field(..., ge=1)
    reason: Optional[str] = Field(None, max_length=2000)


async def governed_write(caller: ResolvedPrincipal) -> bool:
    """True when a decision/rule write by ``caller`` must become a proposal: team state, not a team administrator."""
    return await team_access.current_team_state() == "team" and not await team_access.is_team_administrator(caller)


async def proposal_authority(caller: ResolvedPrincipal, repo_path: Optional[str]) -> Authority:
    """``knowledge.propose`` for the item's scope; system-global needs it on at least one project."""
    if normalize_repo_scope(repo_path) is None:
        return await team_access.proposer_authority(caller)
    return await repository_authority(caller, Permission.PROPOSE, repo_path=repo_path)


def _proposal_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, proposals.ProposalNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (proposals.ProposalStateError, MemoryVersionConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SelfApprovalError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


async def submit_proposal(kind: Literal["decision", "rule"], *, title: str, repo_path: Optional[str],
                          payload: dict[str, Any], caller: ResolvedPrincipal,
                          expected_version: Optional[int]) -> JSONResponse:
    """Record a pending proposal for an already authorized caller and answer HTTP 202."""
    try:
        row = await proposals.create_proposal(kind, title=title, repo_path=repo_path, payload=payload,
                                              author_principal_id=caller.author_id,
                                              expected_version=expected_version)
    except (MemoryVersionConflict, ValueError) as exc:
        raise _proposal_errors(exc) from exc
    return JSONResponse(status_code=202, content={
        "status": "proposed", "proposal_id": row.id, "version": row.version, "state": row.state,
        "kind": row.kind, "target_key": row.target_key, "base_version": row.base_version,
        "detail": "Pending review: proposals are not approved knowledge until an independent reviewer approves them.",
    })


def decision_payload(body: DecisionCreate) -> dict[str, Any]:
    return body.model_dump(exclude={"title", "repo_path", "expected_version"}, exclude_none=True)


def rule_payload(body: RuleCreate) -> dict[str, Any]:
    return body.model_dump(exclude={"name", "repo_path", "expected_version"}, exclude_none=True)


@router.post("/decisions/proposals", dependencies=WRITE, status_code=202)
async def propose_decision(body: DecisionCreate, caller: ResolvedPrincipal = Depends(require_principal)):
    """Always propose (also for team administrators and in solo state, where the owner may approve it)."""
    await proposal_authority(caller, body.repo_path)
    return await submit_proposal("decision", title=body.title, repo_path=body.repo_path,
                                 payload=decision_payload(body), caller=caller,
                                 expected_version=body.expected_version)


@router.post("/rules/proposals", dependencies=WRITE, status_code=202)
async def propose_rule(body: RuleCreate, caller: ResolvedPrincipal = Depends(require_principal)):
    await proposal_authority(caller, body.repo_path)
    return await submit_proposal("rule", title=body.name, repo_path=body.repo_path, payload=rule_payload(body),
                                 caller=caller, expected_version=body.expected_version)


async def _reviewer_authority(caller: ResolvedPrincipal, repo_path: Optional[str]) -> Authority:
    """Authority to see a scope's review queue: ``knowledge.approve`` there (team administrators: everywhere)."""
    # The path as spelled: repository lookup matches the registry, normalization would case-fold Windows paths.
    authority = await repository_authority(caller, Permission.READ, repo_path=repo_path or None)
    if Permission.APPROVE not in authority.allowed:
        raise HTTPException(status_code=403, detail="Review queue requires the Reviewer role on this project")
    return authority


@router.get("/memory/proposals", dependencies=READ)
async def list_memory_proposals(
    repo_path: Optional[str] = Query(None, max_length=1024),
    scope: Optional[Literal["system_global"]] = Query(
        None, description="system_global lists only system-global proposals (team administrators)"),
    state: Optional[Literal["pending", "approved", "rejected", "withdrawn", "all"]] = "pending",
    kind: Optional[Literal["decision", "rule", "learning"]] = None,
    mine: bool = False,
    limit: int = Query(100, ge=1, le=500),
    caller: ResolvedPrincipal = Depends(require_principal),
):
    """The review queue, authorized before any proposal is read.

    ``mine=true`` lists the caller's own proposals in every scope. Otherwise one
    repository's queue needs the Reviewer role on its project; the system-global
    queue and the unscoped every-repository queue need a team administrator."""
    wanted_state = None if state == "all" else state
    if mine:
        author = caller.author_id
        if author is None and await team_access.current_team_state() == "team":
            raise HTTPException(status_code=403, detail="A named credential is required to list your proposals")
        rows = await proposals.list_proposals(state=wanted_state, kind=kind, author_principal_id=author,
                                              limit=limit) if author is not None else []
        return {"proposals": [proposals.proposal_dict(row) for row in rows]}
    if scope == "system_global":
        await _reviewer_authority(caller, None)
        scopes: Optional[list[Optional[str]]] = [None]
    elif repo_path:
        await _reviewer_authority(caller, repo_path)
        scopes = [normalize_repo_scope(repo_path)]
    else:
        await _reviewer_authority(caller, None)
        scopes = None
    rows = await proposals.list_proposals(state=wanted_state, kind=kind, scopes=scopes, limit=limit)
    return {"proposals": [proposals.proposal_dict(row) for row in rows]}


@router.get("/memory/proposals/{proposal_id}", dependencies=READ)
async def get_memory_proposal(proposal_id: int, caller: ResolvedPrincipal = Depends(require_principal)):
    """One proposal for its author or a reviewer of its scope; anyone else gets 404, not its existence."""
    try:
        row = await proposals.get_proposal(proposal_id)
    except proposals.ProposalNotFound as exc:
        raise _proposal_errors(exc) from exc
    if caller.author_id is None or row.author_principal_id != caller.author_id:
        try:
            await _reviewer_authority(caller, row.requested_repo_path or row.repo_path)
        except HTTPException as exc:
            if await team_access.current_team_state() == "solo":
                raise
            raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found") from exc
    return proposals.proposal_dict(row)


async def _approval_authority(caller: ResolvedPrincipal, row) -> Authority:
    """Reviewer on the project and not the author; system-global or unattached: a team administrator, not the author."""
    return await repository_authority(caller, Permission.APPROVE, repo_path=row.requested_repo_path or row.repo_path,
                                      author_id=row.author_principal_id)


async def _decide(proposal_id: int, body: ProposalDecisionRequest, caller: ResolvedPrincipal, approve: bool):
    try:
        row = await proposals.get_proposal(proposal_id)
    except proposals.ProposalNotFound as exc:
        raise _proposal_errors(exc) from exc
    authority = await _approval_authority(caller, row)
    reviewer = authority.initiator_id if authority.team_state == "team" else caller.author_id
    decide = proposals.approve_proposal if approve else proposals.reject_proposal
    try:
        row = await decide(proposal_id, expected_version=body.expected_version, reviewer_principal_id=reviewer,
                           reason=body.reason, team_state=authority.team_state)
    except (proposals.ProposalNotFound, proposals.ProposalStateError, MemoryVersionConflict,
            SelfApprovalError, ValueError) as exc:
        raise _proposal_errors(exc) from exc
    return proposals.proposal_dict(row)


@router.post("/memory/proposals/{proposal_id}/approve", dependencies=WRITE)
async def approve_memory_proposal(proposal_id: int, body: ProposalDecisionRequest,
                                  caller: ResolvedPrincipal = Depends(require_principal)):
    """Approve and publish: the decision/rule store writes the canonical row (supersede history kept)."""
    return await _decide(proposal_id, body, caller, approve=True)


@router.post("/memory/proposals/{proposal_id}/reject", dependencies=WRITE)
async def reject_memory_proposal(proposal_id: int, body: ProposalDecisionRequest,
                                 caller: ResolvedPrincipal = Depends(require_principal)):
    return await _decide(proposal_id, body, caller, approve=False)


@router.post("/memory/proposals/{proposal_id}/withdraw", dependencies=WRITE)
async def withdraw_memory_proposal(proposal_id: int, body: ProposalDecisionRequest,
                                   caller: ResolvedPrincipal = Depends(require_principal)):
    """The author takes back a pending proposal."""
    try:
        row = await proposals.withdraw_proposal(proposal_id, expected_version=body.expected_version,
                                                author_principal_id=caller.author_id)
    except (proposals.ProposalNotFound, proposals.ProposalStateError) as exc:
        raise _proposal_errors(exc) from exc
    return proposals.proposal_dict(row)


@router.post("/learnings/{learning_id}/promote", dependencies=WRITE)
async def promote_personal_learning(learning_id: int, caller: ResolvedPrincipal = Depends(require_principal)):
    """Offer one of your personal learnings to shared memory.

    Team state: a pending learning proposal (HTTP 202) that a reviewer of its scope
    approves; solo state: the owner's learning becomes shared knowledge at once."""
    try:
        if await team_access.current_team_state() == "solo":
            promoted = await proposals.promote_personal_learning_directly(learning_id, caller.author_id)
            return {"status": "promoted", "learning_id": promoted}
        owned = [row for row in await proposals.personal_learnings(caller.author_id) if row.id == learning_id]
        if not owned:
            raise proposals.ProposalNotFound("Personal learning not found")
        spelling = await proposals.registered_repo_spelling(owned[0].repo_scope)
        await proposal_authority(caller, spelling)
        row = await proposals.propose_learning(learning_id, caller.author_id, requested_repo_path=spelling)
    except (proposals.ProposalNotFound, proposals.ProposalStateError) as exc:
        raise _proposal_errors(exc) from exc
    return JSONResponse(status_code=202, content={
        "status": "proposed", "proposal_id": row.id, "version": row.version, "state": row.state,
        "kind": row.kind, "target_key": row.target_key,
    })
