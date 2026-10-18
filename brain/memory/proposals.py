"""Governed memory: proposal -> independent review -> approved knowledge (team state).

In a team, decisions, rules and promoted learnings written by anyone but a
team administrator land here as ``MemoryProposal`` rows instead of in the
canonical tables. Proposals are drafts: no retrieval path (context packs,
search, ``/decisions``, ``/rules``, MCP ``brain://`` resources, learning
retrieval) reads this table, so a draft never reaches an agent as approved
knowledge. A reviewer who holds Reviewer on the project (team administrators
for system-global items) and is not the author approves or rejects it with a
compare-and-swap on the proposal's ``version``.

Approval writes the canonical row through ``DecisionStore`` / ``RuleStore``
in the same transaction as the proposal's state change, with
``expected_version`` set to the target version the proposal was based on: an
approved item that changed after the proposal was made refuses the stale
proposal with ``MemoryVersionConflict`` instead of overwriting it. The store
keeps the supersede history and the author; the proposal row keeps who
approved or rejected it, when and why. Proposal rows are never deleted.

Consolidation (``brain/memory/consolidation.py``) files ``learning`` proposals
too, in team state, for a cluster whose evidence comes from too few principals:
that proposal carries the distilled statement (``payload.origin ==
"memory_consolidation"``) instead of pointing at a personal learning, and
approval writes the learning, retires what it corrects and marks the episode
promoted.

Every submission, approval and rejection is recorded in the team activity feed
(``memory.proposed`` / ``memory.approved`` / ``memory.rejected``, never the
draft content) in the same transaction, so reviewers see what waits for them;
every item an approval writes stamps ``approved_by_principal_id`` next to its
author.

Solo state (a team of one) never creates proposals: the owner writes approved
knowledge directly, exactly as before. Authorization of the caller is the
transport's job (``apps/api/routers/memory_review.py``); this module enforces
the invariants that must hold whatever the transport: independent approval,
a live author, compare-and-swap and one decision per proposal.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional, cast

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain.config.settings import settings
from brain.database.models import Decision, Learning, MemoryEpisode, MemoryProposal, Repository, Rule
from brain.database.repository_utils import find_repository_id
from brain.database.session import async_session_factory
from brain.memory.decision_store import DecisionStore, find_canonical_decision
from brain.memory.learning_store import LearningStore
from brain.memory.provenance import SelfApprovalError, check_expected_version
from brain.memory.repo_scope import normalize_repo_scope
from brain.memory.rule_store import RuleStore, _find_rule, slugify
from brain.team.models import AgentRun, TeamMember

logger = logging.getLogger(__name__)

ProposalKind = Literal["decision", "rule", "learning"]
PERSONAL_STATUS = "personal"
REVOKED_AUTHOR_REASON = "Author access revoked"
# ``payload.origin`` of a learning proposal filed by consolidation (no personal learning behind it).
CONSOLIDATION_ORIGIN = "memory_consolidation"
# The independence check's author for a proposal the system filed (consolidation of
# unattributed evidence): no team-state principal has id 0, so every named reviewer
# is independent of it; the evidence authors are excluded separately.
SYSTEM_AUTHOR_ID = 0

DECISION_FIELDS = ("description", "status", "date", "reason", "consequences",
                   "affected_features", "affected_modules", "affected_files")
RULE_FIELDS = ("description", "type", "severity", "status", "applies_to", "rule_id")


class ProposalNotFound(LookupError):
    """No proposal (or no personal learning of the caller) with that id."""


class ProposalStateError(Exception):
    """The proposal can no longer change: it was already decided, withdrawn, or its author lost access."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def proposal_dict(row: MemoryProposal) -> dict[str, Any]:
    """The transport shape of a proposal."""
    return {
        "id": row.id,
        "kind": row.kind,
        "state": row.state,
        "version": row.version,
        "repo_path": row.requested_repo_path or row.repo_path,
        "scope": row.repo_path or "system_global",
        "target_key": row.target_key,
        "base_version": row.base_version,
        "title": row.title,
        "payload": row.payload or {},
        "author_principal_id": row.author_principal_id,
        "reviewed_by_principal_id": row.reviewed_by_principal_id,
        "decision_reason": row.decision_reason,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "result_key": row.result_key,
        "result_version": row.result_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


_REVIEW_VERBS = {"memory.proposed": "submitted for review", "memory.approved": "approved",
                 "memory.rejected": "rejected"}


async def record_review_event(session: AsyncSession, row: MemoryProposal, kind: str,
                              actor_principal_id: Optional[int]) -> None:
    """Append a ``memory.*`` row to the team activity feed in the caller's transaction.

    The event names the proposal, never its draft content. A repository-scoped
    proposal is visible to that repository's readers (its reviewers among them);
    a system-global one to team administrators. Best effort: a savepoint keeps
    a failed record from undoing the review itself."""
    if settings.BRAIN_STORAGE_MODE != "full":
        return  # team tables exist only under full storage
    from brain.team.activity import record_event

    try:
        async with session.begin_nested():
            path = row.requested_repo_path or row.repo_path
            repository_id = await find_repository_id(session, path) if path else None
            await record_event(
                session, kind=kind, audience="repository" if repository_id is not None else "admin",
                summary=f"{row.kind.capitalize()} proposal #{row.id} {_REVIEW_VERBS[kind]}",
                actor_principal_id=actor_principal_id, actor_label=None if actor_principal_id else "system",
                subject_principal_id=row.author_principal_id, repository_id=repository_id,
                details={"proposal_id": row.id, "proposal_kind": row.kind, "state": row.state,
                         "scope": row.repo_path or "system_global"},
                dedupe_key=f"{kind}:{row.id}",
            )
    except Exception as exc:  # noqa: BLE001 - the review stands; the feed row is best effort
        logger.warning("Could not record %s for proposal %s: %s: %s", kind, row.id, type(exc).__name__, exc)


# Bounds the in-app notification write plus the per-member Telegram push after a submission.
TEAM_NOTIFY_TIMEOUT_S = 10.0


async def notify_reviewers(proposal_id: int, kind: str, repo_path: Optional[str]) -> None:
    """Tell reviewers once a proposal is committed; never fails or stalls the submission.

    * Opt-in push (``MEMORY_REVIEW_TELEGRAM_NOTIFY``) to the owner chat, as before.
    * Team state (full storage): an in-app notification for every member who may
      approve it, plus a Telegram notice to those who linked their own chat
      (``brain.team.notifications``). Solo state writes nothing.

    Messages name the proposal, never its draft content. Best effort and bounded."""
    if settings.MEMORY_REVIEW_TELEGRAM_NOTIFY:
        try:
            from brain.alerts.telegram import deliver_memory_proposal_notice

            await asyncio.wait_for(
                deliver_memory_proposal_notice(proposal_id=proposal_id, kind=kind, repo_path=repo_path),
                timeout=min(5.0, float(settings.TELEGRAM_ALERT_TIMEOUT_S)))
        except Exception as exc:  # noqa: BLE001 - the proposal stands; the push is best effort
            logger.warning("Review notification for proposal %s failed: %s", proposal_id, type(exc).__name__)
    await notify_team_reviewers(proposal_id)


async def notify_team_reviewers(proposal_id: int) -> None:
    """In-app (and linked-chat) review notifications for the members who may approve; team state only."""
    if settings.BRAIN_STORAGE_MODE != "full":
        return
    try:
        from brain.team.notifications import notify_review_needed

        await asyncio.wait_for(
            notify_review_needed(async_session_factory, proposal_id,
                                 telegram_timeout_s=min(5.0, float(settings.TELEGRAM_ALERT_TIMEOUT_S))),
            timeout=TEAM_NOTIFY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the proposal stands; notifications are best effort
        logger.warning("Team review notifications for proposal %s failed: %s", proposal_id, type(exc).__name__)


async def _target(session: AsyncSession, kind: str, title: str, repo_path: Optional[str],
                  payload: dict[str, Any]) -> tuple[Optional[str], int]:
    """(canonical key, current version) of the item a decision/rule proposal would change; (None, 0) if new."""
    if kind == "decision":
        decision = await find_canonical_decision(session, title, repo_path)
        return (str(decision.id), decision.version or 1) if decision is not None else (None, 0)
    rule, _ = await _find_rule(session, payload.get("rule_id") or slugify(title), repo_path)
    return (rule.id, rule.version or 1) if rule is not None else (None, 0)


async def create_proposal(kind: Literal["decision", "rule"], *, title: str, repo_path: Optional[str],
                          payload: dict[str, Any], author_principal_id: Optional[int],
                          expected_version: Optional[int] = None) -> MemoryProposal:
    """Record a pending proposal for a decision or rule.

    The proposal remembers the version of the item it changes (``base_version``,
    0 for a new item). A caller that sends ``expected_version`` is checked now,
    so a proposal against an already stale read fails fast with
    ``MemoryVersionConflict``; approval re-checks the same version."""
    title = title.strip()
    if not title:
        raise ValueError(f"{kind.capitalize()} title must not be empty")
    scope = normalize_repo_scope(repo_path)
    fields = DECISION_FIELDS if kind == "decision" else RULE_FIELDS
    clean = {field: payload.get(field) for field in fields if payload.get(field) is not None}
    async with async_session_factory() as session:
        target_key, current = await _target(session, kind, title, scope, clean)
        check_expected_version(kind, target_key or title, expected_version, current)
        row = MemoryProposal(kind=kind, state="pending", version=1, repo_path=scope,
                             requested_repo_path=repo_path.strip() if scope is not None and repo_path else None,
                             target_key=target_key, base_version=current, title=title, payload=clean,
                             author_principal_id=author_principal_id)
        session.add(row)
        await session.flush()
        await record_review_event(session, row, "memory.proposed", author_principal_id)
        await session.commit()
        await session.refresh(row)
    await notify_reviewers(row.id, row.kind, row.requested_repo_path or row.repo_path)
    return row


async def registered_repo_spelling(scope: Optional[str]) -> Optional[str]:
    """The registry spelling of a normalized scope (learnings store scopes normalized, which case-folds
    Windows paths); the scope itself when no indexed repository matches."""
    if scope is None:
        return None
    async with async_session_factory() as session:
        for path in (await session.execute(select(Repository.path))).scalars():
            if normalize_repo_scope(path) == normalize_repo_scope(scope):
                return path
    return scope


async def propose_learning(learning_id: int, author_principal_id: Optional[int],
                           requested_repo_path: Optional[str] = None) -> MemoryProposal:
    """Propose promoting the caller's personal learning into shared memory.

    Only the author's own ``personal`` learning can be proposed, and only once
    while a proposal for it is pending."""
    async with async_session_factory() as session:
        learning = await session.get(Learning, learning_id)
        if (learning is None or learning.status != PERSONAL_STATUS
                or author_principal_id is None or learning.author_principal_id != author_principal_id):
            raise ProposalNotFound("Personal learning not found")
        pending = (await session.execute(select(MemoryProposal.id).where(
            MemoryProposal.kind == "learning", MemoryProposal.target_key == str(learning_id),
            MemoryProposal.state == "pending"))).scalar_one_or_none()
        if pending is not None:
            raise ProposalStateError(f"Learning {learning_id} already has pending proposal {pending}")
        row = MemoryProposal(kind="learning", state="pending", version=1,
                             repo_path=normalize_repo_scope(learning.repo_scope),
                             requested_repo_path=requested_repo_path or learning.repo_scope,
                             target_key=str(learning_id), base_version=0,
                             title=learning.statement[:255],
                             payload={"statement": learning.statement, "category": learning.category,
                                      "confidence": learning.confidence},
                             author_principal_id=author_principal_id)
        session.add(row)
        await session.flush()
        await record_review_event(session, row, "memory.proposed", author_principal_id)
        await session.commit()
        await session.refresh(row)
    await notify_reviewers(row.id, row.kind, row.requested_repo_path or row.repo_path)
    return row


async def add_consolidated_learning_proposal(
        session: AsyncSession, *, statement: str, category: Optional[str], confidence: float,
        evidence: list[dict[str, Any]], repo_path: Optional[str], requested_repo_path: Optional[str],
        author_principal_id: Optional[int], episode_id: int, supersedes: list[int], severity: str,
        suggested_target: str, evidence_tier: str, approval_reason: Optional[str] = None) -> MemoryProposal:
    """File a consolidation learning proposal inside the caller's (episode) transaction.

    Team state only: the cluster's evidence comes from too few principals to
    reach shared memory unreviewed, or gate G4 wants a human approval
    (``approval_reason`` set, ``payload.gate == "G4"``). The proposal is
    authored by the lead principal behind the evidence (None, i.e. the system,
    when no evidence is attributed) and carries everything approval needs to
    write the learning; ``target_key`` stays NULL (no personal learning)."""
    payload: dict[str, Any] = {
        "origin": CONSOLIDATION_ORIGIN, "statement": statement, "category": category, "confidence": confidence,
        "evidence": list(evidence), "episode_id": episode_id, "supersedes": [int(item) for item in supersedes],
        "severity": severity, "suggested_target": suggested_target, "evidence_tier": evidence_tier,
    }
    if approval_reason:
        payload.update(gate="G4", approval_reason=approval_reason)
    row = MemoryProposal(kind="learning", state="pending", version=1, repo_path=normalize_repo_scope(repo_path),
                         requested_repo_path=requested_repo_path, target_key=None, base_version=0,
                         title=statement[:255], payload=payload, author_principal_id=author_principal_id)
    session.add(row)
    await session.flush()
    await record_review_event(session, row, "memory.proposed", author_principal_id)
    return row


async def promote_personal_learning_directly(learning_id: int, author_principal_id: Optional[int]) -> int:
    """Solo state: the owner's personal learning becomes shared knowledge at once (no reviewer exists)."""
    async with async_session_factory() as session:
        learning = await session.get(Learning, learning_id)
        if learning is None or learning.status != PERSONAL_STATUS or (
                learning.author_principal_id is not None and learning.author_principal_id != author_principal_id):
            raise ProposalNotFound("Personal learning not found")
        learning.status = "active"
        learning.updated_by_principal_id = author_principal_id
        await session.commit()
        return learning.id


async def personal_learnings(author_principal_id: Optional[int]) -> list[Learning]:
    """The personal-tier learnings of one principal, newest first (nobody else's)."""
    if author_principal_id is None:
        return []
    async with async_session_factory() as session:
        return list((await session.execute(select(Learning).where(
            Learning.status == PERSONAL_STATUS, Learning.author_principal_id == author_principal_id,
        ).order_by(Learning.id.desc()))).scalars().all())


async def count_pending_proposals(*, scopes: Optional[list[Optional[str]]] = None,
                                  exclude_author: Optional[int] = None,
                                  exclude_evidence_author: Optional[int] = None) -> int:
    """How many pending proposals wait in ``scopes`` (None = every scope), leaving out ``exclude_author``'s own
    (nobody reviews their own proposal). Same scope semantics as ``list_proposals``."""
    filters = [MemoryProposal.state == "pending"]
    if exclude_author is not None:
        filters.append(or_(MemoryProposal.author_principal_id.is_(None),
                           MemoryProposal.author_principal_id != exclude_author))
    if scopes is not None:
        paths = [scope for scope in scopes if scope is not None]
        clauses = []
        if paths:
            clauses.append(MemoryProposal.repo_path.in_(paths))
        if None in scopes:
            clauses.append(MemoryProposal.repo_path.is_(None))
        if not clauses:
            return 0
        filters.append(or_(*clauses))
    async with async_session_factory() as session:
        if exclude_evidence_author is None:
            stmt = select(func.count()).select_from(MemoryProposal).where(*filters)
            return int((await session.execute(stmt)).scalar_one())
        rows = (await session.execute(select(MemoryProposal).where(*filters))).scalars().all()
        return sum(exclude_evidence_author not in _evidence_principals(row) for row in rows)


async def get_proposal(proposal_id: int) -> MemoryProposal:
    async with async_session_factory() as session:
        row = await session.get(MemoryProposal, proposal_id)
        if row is None:
            raise ProposalNotFound(f"Proposal {proposal_id} not found")
        return row


async def list_proposals(*, state: Optional[str] = "pending", kind: Optional[str] = None,
                         scopes: Optional[list[Optional[str]]] = None, author_principal_id: Optional[int] = None,
                         limit: int = 100) -> list[MemoryProposal]:
    """Proposals, newest first. ``scopes`` restricts to those normalized scopes (None = system-global);
    leaving it out lists every scope, which only a caller authorized for every scope may ask for."""
    stmt = select(MemoryProposal)
    if state:
        stmt = stmt.where(MemoryProposal.state == state)
    if kind:
        stmt = stmt.where(MemoryProposal.kind == kind)
    if author_principal_id is not None:
        stmt = stmt.where(MemoryProposal.author_principal_id == author_principal_id)
    if scopes is not None:
        paths = [scope for scope in scopes if scope is not None]
        clauses = []
        if paths:
            clauses.append(MemoryProposal.repo_path.in_(paths))
        if None in scopes:
            clauses.append(MemoryProposal.repo_path.is_(None))
        if not clauses:
            return []
        stmt = stmt.where(or_(*clauses))
    stmt = stmt.order_by(MemoryProposal.id.desc()).limit(limit)
    async with async_session_factory() as session:
        return list((await session.execute(stmt)).scalars().all())


async def _author_is_live(session: AsyncSession, author_principal_id: Optional[int]) -> bool:
    """A team author, or a run's human initiator, must still be a live verified member."""
    if author_principal_id is None:
        return False
    member = (await session.execute(select(TeamMember).where(
        TeamMember.principal_id == author_principal_id))).scalar_one_or_none()
    if member is not None:
        return bool(member.verified_at and not member.revoked_at)
    run = (await session.execute(select(AgentRun).where(
        AgentRun.principal_id == author_principal_id))).scalar_one_or_none()
    if run is None or run.revoked_at is not None:
        return False
    initiator = (await session.execute(select(TeamMember).where(
        TeamMember.principal_id == run.initiator_id))).scalar_one_or_none()
    return bool(initiator and initiator.verified_at and not initiator.revoked_at)


async def _claim(session: AsyncSession, proposal_id: int, expected_version: int, new_state: str,
                 reviewer_principal_id: Optional[int], reason: Optional[str]) -> MemoryProposal:
    """Compare-and-swap the proposal out of ``pending``; a stale version or a decided proposal is a conflict."""
    row = await session.get(MemoryProposal, proposal_id)
    if row is None:
        raise ProposalNotFound(f"Proposal {proposal_id} not found")
    if row.state != "pending":
        raise ProposalStateError(f"Proposal {proposal_id} is already {row.state}")
    changed = await session.execute(
        update(MemoryProposal)
        .where(MemoryProposal.id == proposal_id, MemoryProposal.version == expected_version,
               MemoryProposal.state == "pending")
        .values(state=new_state, version=expected_version + 1, reviewed_by_principal_id=reviewer_principal_id,
                decision_reason=reason, decided_at=_now())
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[Any], changed).rowcount != 1:
        raise ProposalStateError(
            f"Proposal {proposal_id} is at version {row.version}, not {expected_version}; re-read it and retry")
    await session.refresh(row)
    return row


def _is_consolidated(row: MemoryProposal) -> bool:
    return row.kind == "learning" and (row.payload or {}).get("origin") == CONSOLIDATION_ORIGIN


def system_filed(row: MemoryProposal) -> bool:
    """A consolidation proposal without an author: filed by the system for unattributed evidence."""
    return row.author_principal_id is None and _is_consolidated(row)


def _evidence_principals(row: MemoryProposal) -> set[int]:
    """Principals behind a consolidation proposal's L1 evidence."""
    found: set[int] = set()
    for item in (row.payload or {}).get("evidence") or []:
        try:
            if isinstance(item, dict) and item.get("author_principal_id") is not None:
                found.add(int(item["author_principal_id"]))
        except (TypeError, ValueError):
            continue
    return found


def _check_independent(row: MemoryProposal, reviewer_principal_id: Optional[int], team_state: str) -> None:
    if team_state != "team":
        return
    if reviewer_principal_id is None or reviewer_principal_id == row.author_principal_id:
        raise SelfApprovalError("Independent, attributed approval is required: the author cannot review "
                                "their own proposal")
    # Consolidated memory: whoever supplied the evidence is not independent of the claim either.
    if _is_consolidated(row) and reviewer_principal_id in _evidence_principals(row):
        raise SelfApprovalError("Independent, attributed approval is required: this proposal rests on your own "
                                "evidence")


async def approve_proposal(proposal_id: int, *, expected_version: int, reviewer_principal_id: Optional[int],
                           reason: Optional[str] = None, team_state: str = "team") -> MemoryProposal:
    """Approve a pending proposal and write the approved knowledge in the same transaction.

    Raises ``ProposalNotFound``, ``ProposalStateError`` (stale ``expected_version``,
    already decided, or an author who lost team access), ``SelfApprovalError`` and
    ``MemoryVersionConflict`` (the approved item changed since the proposal)."""
    async with async_session_factory() as session:
        row = await session.get(MemoryProposal, proposal_id)
        if row is None:
            raise ProposalNotFound(f"Proposal {proposal_id} not found")
        _check_independent(row, reviewer_principal_id, team_state)
        # A consolidation proposal without an author was filed by the system (unattributed G4 evidence).
        if team_state == "team" and not system_filed(row) and not await _author_is_live(session,
                                                                                         row.author_principal_id):
            raise ProposalStateError(f"Proposal {proposal_id} cannot be approved: its author no longer has "
                                     "team access")
        row = await _claim(session, proposal_id, expected_version, "approved", reviewer_principal_id, reason)
        payload = dict(row.payload or {})
        if row.kind == "decision":
            result = await DecisionStore.record_decision(
                title=row.title, repo_path=row.repo_path, caller_role="admin",
                author_principal_id=row.author_principal_id, expected_version=row.base_version,
                session=session, **{field: payload.get(field) for field in DECISION_FIELDS
                                    if field in payload},
            )
            row.result_key, row.result_version = str(result.id), result.version
            if result.changed:
                decision = await session.get(Decision, result.id)
                if decision is not None:
                    decision.approved_by_principal_id = reviewer_principal_id
        elif row.kind == "rule":
            result = await RuleStore.record_rule(
                name=row.title, repo_path=row.repo_path, author_principal_id=row.author_principal_id,
                expected_version=row.base_version, session=session,
                **{field: payload.get(field) for field in RULE_FIELDS if field in payload},
            )
            row.result_key, row.result_version = str(result.id), result.version
            if result.changed:
                rule = await session.get(Rule, result.id)
                if rule is not None:
                    rule.approved_by_principal_id = reviewer_principal_id
        elif payload.get("origin") == CONSOLIDATION_ORIGIN:
            learning_id = await _approve_consolidated_learning(session, row, payload, reviewer_principal_id)
            row.result_key, row.result_version = str(learning_id), 1
        else:
            learning = await session.get(Learning, int(row.target_key or 0))
            if learning is None or learning.status != PERSONAL_STATUS:
                raise ProposalStateError(f"Learning {row.target_key} is no longer a personal learning")
            learning.status = "active"
            learning.updated_by_principal_id = reviewer_principal_id
            learning.approved_by_principal_id = reviewer_principal_id
            row.result_key, row.result_version = str(learning.id), 1
        await record_review_event(session, row, "memory.approved", reviewer_principal_id)
        await session.commit()
        await session.refresh(row)
        return row


async def _approve_consolidated_learning(session: AsyncSession, row: MemoryProposal, payload: dict[str, Any],
                                         reviewer_principal_id: Optional[int]) -> int:
    """Write the learning a consolidation proposal carries, retire what it corrects, promote its episode."""
    learning_id = await LearningStore.add_learning(
        statement=str(payload.get("statement") or row.title), category=payload.get("category"),
        confidence=float(payload.get("confidence") or 0.5), evidence=list(payload.get("evidence") or []),
        repo_scope=row.repo_path, promoted_from=f"proposal:{row.id}", session=session,
        author_principal_id=row.author_principal_id, updated_by_principal_id=reviewer_principal_id,
        approved_by_principal_id=reviewer_principal_id,
    )
    for old_id in payload.get("supersedes") or []:
        old = await session.get(Learning, int(old_id))
        if old is None or old.status != "active":
            continue  # already retired or replaced while the proposal waited
        await LearningStore.supersede(int(old_id), learning_id, session=session,
                                      updated_by_principal_id=reviewer_principal_id)
    episode = await _proposal_episode(session, payload)
    if episode is not None:
        episode.status = "promoted"
        episode.promoted_to_learning_id = learning_id
        episode.updated_by_principal_id = reviewer_principal_id
        episode.gate_reasons = [*(episode.gate_reasons or []), f"approved via proposal #{row.id}"]
    return learning_id


async def _proposal_episode(session: AsyncSession, payload: dict[str, Any]) -> Optional[MemoryEpisode]:
    """The ``proposed`` L2 episode a consolidation proposal came from, if it is still waiting."""
    try:
        episode_id = int(payload.get("episode_id") or 0)
    except (TypeError, ValueError):
        return None
    episode = await session.get(MemoryEpisode, episode_id) if episode_id else None
    return episode if episode is not None and episode.status == "proposed" else None


async def _close_proposal_episodes(session: AsyncSession, rows: list[MemoryProposal], note: str) -> None:
    """A rejected or withdrawn consolidation proposal rejects its episode (fresh evidence may re-open it)."""
    for row in rows:
        payload = row.payload or {}
        if row.kind != "learning" or payload.get("origin") != CONSOLIDATION_ORIGIN:
            continue
        episode = await _proposal_episode(session, payload)
        if episode is not None:
            episode.status = "rejected"
            episode.gate_reasons = [*(episode.gate_reasons or []), f"{note} (proposal #{row.id})"]


async def reject_proposal(proposal_id: int, *, expected_version: int, reviewer_principal_id: Optional[int],
                          reason: Optional[str] = None, team_state: str = "team") -> MemoryProposal:
    """Reject a pending proposal; nothing reaches approved memory. Same independence rule as approval."""
    async with async_session_factory() as session:
        row = await session.get(MemoryProposal, proposal_id)
        if row is None:
            raise ProposalNotFound(f"Proposal {proposal_id} not found")
        _check_independent(row, reviewer_principal_id, team_state)
        row = await _claim(session, proposal_id, expected_version, "rejected", reviewer_principal_id, reason)
        await _close_proposal_episodes(session, [row], "rejected in review")
        await record_review_event(session, row, "memory.rejected", reviewer_principal_id)
        await session.commit()
        await session.refresh(row)
        return row


async def withdraw_proposal(proposal_id: int, *, expected_version: int,
                            author_principal_id: Optional[int]) -> MemoryProposal:
    """The author takes back their own pending proposal."""
    async with async_session_factory() as session:
        row = await session.get(MemoryProposal, proposal_id)
        if row is None or author_principal_id is None or row.author_principal_id != author_principal_id:
            raise ProposalNotFound(f"Proposal {proposal_id} not found")
        row = await _claim(session, proposal_id, expected_version, "withdrawn", author_principal_id,
                           "Withdrawn by the author")
        await _close_proposal_episodes(session, [row], "withdrawn by the author")
        await session.commit()
        await session.refresh(row)
        return row


async def withdraw_author_proposals(session: AsyncSession, principal_id: int,
                                    reason: str = REVOKED_AUTHOR_REASON) -> int:
    """Withdraw every pending proposal of ``principal_id`` in the caller's transaction (member revocation).

    Knowledge that was already approved stays: it was reviewed by someone else.
    Returns how many proposals were withdrawn."""
    pending = list((await session.execute(select(MemoryProposal).where(
        MemoryProposal.author_principal_id == principal_id, MemoryProposal.state == "pending"))).scalars().all())
    await _close_proposal_episodes(session, pending, reason)
    result = await session.execute(
        update(MemoryProposal)
        .where(MemoryProposal.author_principal_id == principal_id, MemoryProposal.state == "pending")
        .values(state="withdrawn", version=MemoryProposal.version + 1, decision_reason=reason, decided_at=_now())
        .execution_options(synchronize_session=False)
    )
    return int(cast(CursorResult[Any], result).rowcount or 0)
