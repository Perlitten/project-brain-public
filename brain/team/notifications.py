"""Review-needed notifications for the team members who can act on them.

When a memory proposal is committed in team state, ``notify_review_needed``
writes one in-app notification (``team_notifications``, read on ``/team/me``)
for every member who may approve it, then pushes a short Telegram notice to
the members among them who linked their own chat (``TEAM_MEMBER_TELEGRAM_NOTIFY``).

Recipients mirror the approval rule of ``apps/api/routers/memory_review.py``:

* a proposal on a repository attached to a project goes to the live, verified
  human members of that project whose role carries ``knowledge.approve`` and
  whose project policy allows it (Reviewers on a live project);
* a system-global proposal, or one on a repository attached to no project,
  goes to the live, verified human team administrators;
* the author and, for consolidation proposals, whoever supplied the evidence
  are left out: they cannot approve it.

The answer is about membership only (like ``principal_is_team_administrator``):
a reviewer whose current key lacks the ``team:knowledge.approve`` scope is
still told, and the review queue refuses the decision as before.

Bounded and idempotent: at most ``MAX_RECIPIENTS`` recipients per proposal,
one row per recipient and proposal (``dedupe_key``), each inbox pruned to its
newest ``INBOX_LIMIT`` rows. Solo state writes nothing. No draft content is
stored or sent: the proposal number, its kind and the repository name.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.database.models import MemoryProposal, Principal, Repository
from brain.database.repository_utils import find_repository_id
from brain.team.authorization import (ROLE_PERMISSIONS, Permission, registry_records, registry_trust_ceiling,
                                      team_state)
from brain.team.models import (ProjectMember, ProjectRepository, TeamMember, TeamNotification, TeamProject,
                               TeamTelegramLink)

logger = logging.getLogger(__name__)

REVIEW_KIND = "memory.review_needed"
MAX_RECIPIENTS = 100
INBOX_LIMIT = 500
LIST_LIMIT = 100
LINK_CODE_TTL = timedelta(minutes=10)
LINK_CODE_RESEND = timedelta(seconds=60)
LINK_MAX_ATTEMPTS = 5
TELEGRAM_CONCURRENCY = 10
_CHAT_ID = re.compile(r"^-?[0-9]{1,20}$")


class TelegramLinkError(ValueError):
    """A link request the API answers with 4xx: bad chat id, no pending code, expired or wrong code."""


def review_dedupe_key(proposal_id: int) -> str:
    return f"memory.review:{proposal_id}"


def _repository_name(repo_path: Optional[str]) -> str:
    if not repo_path or not repo_path.strip():
        return "system-global"
    return repo_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or repo_path


def _live_human_member(*extra):
    """Predicates for a live, verified human member of the team (``human_member`` without the credential)."""
    return (TeamMember.team_id == 1, TeamMember.verified_at.is_not(None), TeamMember.revoked_at.is_(None),
            TeamMember.verification_issuer != "", TeamMember.verification_subject != "",
            Principal.kind == "human", Principal.disabled_at.is_(None), *extra)


async def review_recipients(session: AsyncSession, row: MemoryProposal) -> list[int]:
    """Principal ids that may approve ``row``, author and evidence suppliers excluded, at most ``MAX_RECIPIENTS``."""
    from brain.memory.proposals import _evidence_principals

    path = row.requested_repo_path or row.repo_path
    link = None
    indexed_path = None
    if path:
        # The path as spelled, like the review queue's authorization: the registry and index match it.
        repository_id = await find_repository_id(session, path)
        if repository_id is not None:
            indexed_path = (await session.execute(select(Repository.path).where(
                Repository.id == repository_id))).scalar_one_or_none()
            link = (await session.execute(select(ProjectRepository).where(
                ProjectRepository.repository_id == repository_id))).scalar_one_or_none()
        ceiling, _ = registry_trust_ceiling(await registry_records(session), indexed_path, path)
        if Permission.APPROVE not in ceiling:
            return []
    if link is not None:
        project = await session.get(TeamProject, link.project_id)
        if project is None or project.disabled_at is not None:
            return []
        try:
            policy = frozenset(Permission(value) for value in project.permissions or [])
        except ValueError:
            return []  # an invalid policy grants nothing, as ``authorize`` denies it
        if Permission.APPROVE not in policy:
            return []
        roles = sorted(role for role, allowed in ROLE_PERMISSIONS.items() if Permission.APPROVE in allowed)
        stmt = (select(ProjectMember.principal_id)
                .join(TeamMember, TeamMember.principal_id == ProjectMember.principal_id)
                .join(Principal, Principal.id == ProjectMember.principal_id)
                .where(*_live_human_member(ProjectMember.project_id == project.id, ProjectMember.role.in_(roles),
                                           ProjectMember.revoked_at.is_(None))))
    else:
        stmt = (select(TeamMember.principal_id)
                .join(Principal, Principal.id == TeamMember.principal_id)
                .where(*_live_human_member(TeamMember.is_admin.is_(True))))
    excluded = set(_evidence_principals(row))
    if row.author_principal_id is not None:
        excluded.add(row.author_principal_id)
    found = sorted({int(pid) for pid in (await session.execute(stmt)).scalars()} - excluded)
    return found[:MAX_RECIPIENTS]


async def _prune(session: AsyncSession, recipient: int) -> None:
    stale = (await session.execute(
        select(TeamNotification.id).where(TeamNotification.recipient_principal_id == recipient)
        .order_by(TeamNotification.id.desc()).offset(INBOX_LIMIT))).scalars().all()
    if stale:
        await session.execute(delete(TeamNotification).where(TeamNotification.id.in_(stale)))


async def record_review_needed(session: AsyncSession, row: MemoryProposal) -> list[int]:
    """Write the review-needed notification for each recipient not told yet; returns the newly told ids."""
    recipients = await review_recipients(session, row)
    if not recipients:
        return []
    key = review_dedupe_key(row.id)
    told = set((await session.execute(select(TeamNotification.recipient_principal_id).where(
        TeamNotification.dedupe_key == key,
        TeamNotification.recipient_principal_id.in_(recipients)))).scalars())
    fresh = [pid for pid in recipients if pid not in told]
    scope = row.requested_repo_path or row.repo_path
    for pid in fresh:
        session.add(TeamNotification(
            recipient_principal_id=pid, kind=REVIEW_KIND, proposal_id=row.id, dedupe_key=key,
            summary=f"{row.kind.capitalize()} proposal #{row.id} awaits your review in {_repository_name(scope)}"[:255],
            details={"proposal_id": row.id, "proposal_kind": row.kind, "scope": row.repo_path or "system_global"},
        ))
    await session.flush()
    for pid in fresh:
        await _prune(session, pid)
    return fresh


async def _push_telegram(chats: list[str], row: MemoryProposal) -> None:
    from brain.alerts.telegram import deliver_member_review_notice

    gate = asyncio.Semaphore(TELEGRAM_CONCURRENCY)

    async def one(chat_id: str) -> None:
        async with gate:
            await deliver_member_review_notice(chat_id=chat_id, proposal_id=row.id, kind=row.kind,
                                               repo_path=row.requested_repo_path or row.repo_path)

    results = await asyncio.gather(*(one(chat) for chat in chats), return_exceptions=True)
    failed = sum(isinstance(result, BaseException) for result in results)
    if failed:
        logger.warning("Member review notices: %s of %s Telegram deliveries failed", failed, len(chats))


async def notify_review_needed(factory: async_sessionmaker, proposal_id: int, *,
                               telegram_timeout_s: float = 5.0) -> list[int]:
    """Notify everyone who may approve ``proposal_id`` (team state only); returns the newly notified ids.

    Commits the in-app rows first; the optional Telegram push runs after the
    commit, concurrently and bounded by ``telegram_timeout_s``."""
    from brain.alerts.telegram import member_telegram_available

    async with factory() as session:
        if await team_state(session) != "team":
            return []
        row = await session.get(MemoryProposal, proposal_id)
        if row is None or row.state != "pending":
            return []
        fresh = await record_review_needed(session, row)
        chats: list[str] = []
        if fresh and member_telegram_available():
            chats = [chat for chat in (await session.execute(select(TeamTelegramLink.chat_id).where(
                TeamTelegramLink.principal_id.in_(fresh), TeamTelegramLink.chat_id.is_not(None)))).scalars() if chat]
        await session.commit()
    if chats:
        try:
            await asyncio.wait_for(_push_telegram(chats, row), timeout=telegram_timeout_s)
        except Exception as exc:  # noqa: BLE001 - the inbox row stands; the push is best effort
            logger.warning("Member review notices for proposal %s failed: %s", proposal_id, type(exc).__name__)
    return fresh


# The caller's own inbox. Every query below is bound to the recipient: no id from a request reaches
# another member's row.


def notification_dict(row: TeamNotification, proposal_state: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": row.id, "kind": row.kind, "summary": row.summary, "proposal_id": row.proposal_id,
        "proposal_state": proposal_state, "details": row.details or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "read_at": row.read_at.isoformat() if row.read_at else None,
    }


async def list_notifications(session: AsyncSession, recipient: int, *, unread_only: bool = False,
                             limit: int = 50) -> dict[str, Any]:
    filters = [TeamNotification.recipient_principal_id == recipient]
    unread = int((await session.execute(select(func.count()).select_from(TeamNotification).where(
        *filters, TeamNotification.read_at.is_(None)))).scalar_one())
    if unread_only:
        filters.append(TeamNotification.read_at.is_(None))
    rows = (await session.execute(select(TeamNotification).where(*filters)
                                  .order_by(TeamNotification.id.desc())
                                  .limit(max(1, min(limit, LIST_LIMIT))))).scalars().all()
    proposal_ids = sorted({row.proposal_id for row in rows if row.proposal_id is not None})
    states: dict[int, str] = {}
    if proposal_ids:
        states = {pid: state for pid, state in (await session.execute(
            select(MemoryProposal.id, MemoryProposal.state).where(MemoryProposal.id.in_(proposal_ids)))).all()}
    return {"notifications": [notification_dict(row, states.get(row.proposal_id or -1)) for row in rows],
            "unread_count": unread}


async def mark_read(session: AsyncSession, recipient: int, notification_id: int) -> Optional[TeamNotification]:
    """Mark one of ``recipient``'s notifications read; ``None`` when it is not theirs (or does not exist)."""
    row = (await session.execute(select(TeamNotification).where(
        TeamNotification.id == notification_id,
        TeamNotification.recipient_principal_id == recipient))).scalar_one_or_none()
    if row is not None and row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        await session.flush()
    return row


async def mark_all_read(session: AsyncSession, recipient: int) -> int:
    result = await session.execute(update(TeamNotification).where(
        TeamNotification.recipient_principal_id == recipient,
        TeamNotification.read_at.is_(None)).values(read_at=datetime.now(timezone.utc)))
    return int(getattr(result, "rowcount", 0) or 0)


# Self-service Telegram link: the bot sends a one-time code to the chat, the member types it back.


def _code_hash(principal_id: int, chat_id: str, code: str) -> str:
    return hashlib.sha256(f"{principal_id}:{chat_id}:{code}".encode()).hexdigest()


def _mask(chat_id: Optional[str]) -> Optional[str]:
    if not chat_id:
        return None
    return f"…{chat_id[-4:]}" if len(chat_id) > 4 else "…"


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def telegram_link_dict(link: Optional[TeamTelegramLink], *, available: bool) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires = _aware(link.code_expires_at) if link else None
    pending = bool(link and link.pending_chat_id and expires and expires > now)
    return {
        "available": available,
        "linked": bool(link and link.chat_id),
        "chat": _mask(link.chat_id) if link else None,
        "linked_at": link.linked_at.isoformat() if link and link.linked_at else None,
        "pending": pending,
        "pending_chat": _mask(link.pending_chat_id) if pending and link else None,
        "code_expires_at": expires.isoformat() if pending and expires else None,
    }


async def get_telegram_link(session: AsyncSession, principal_id: int) -> Optional[TeamTelegramLink]:
    return await session.get(TeamTelegramLink, principal_id)


async def start_telegram_link(session: AsyncSession, principal_id: int, chat_id: str) -> tuple[TeamTelegramLink, str]:
    """Record a pending link for the caller's own row and return the code to send to ``chat_id``.

    Raises ``TelegramLinkError`` for a malformed chat id or a resend inside ``LINK_CODE_RESEND``."""
    chat_id = (chat_id or "").strip()
    if not _CHAT_ID.match(chat_id):
        raise TelegramLinkError("Enter the numeric Telegram chat id (for example 123456789)")
    now = datetime.now(timezone.utc)
    link = await session.get(TeamTelegramLink, principal_id, with_for_update=True)
    if link is None:
        link = TeamTelegramLink(principal_id=principal_id, attempts=0)
        session.add(link)
    sent = _aware(link.code_sent_at)
    if sent is not None and now - sent < LINK_CODE_RESEND:
        raise TelegramLinkError("A code was sent less than a minute ago. Wait a moment before asking again")
    code = f"{secrets.randbelow(1_000_000):06d}"
    link.pending_chat_id = chat_id
    link.code_hash = _code_hash(principal_id, chat_id, code)
    link.code_sent_at = now
    link.code_expires_at = now + LINK_CODE_TTL
    link.attempts = 0
    await session.flush()
    return link, code


async def cancel_telegram_link(session: AsyncSession, principal_id: int, code_hash: str) -> bool:
    """Clear only the still-current pending code after its delivery failed."""
    result = await session.execute(update(TeamTelegramLink).where(
        TeamTelegramLink.principal_id == principal_id,
        TeamTelegramLink.code_hash == code_hash,
    ).values(pending_chat_id=None, code_hash=None, code_sent_at=None, code_expires_at=None, attempts=0))
    return bool(getattr(result, "rowcount", 0))


async def verify_telegram_link(session: AsyncSession, principal_id: int, code: str) -> TeamTelegramLink:
    """Link the pending chat when ``code`` matches; ``TelegramLinkError`` otherwise (bounded attempts)."""
    link = await session.get(TeamTelegramLink, principal_id, with_for_update=True)
    now = datetime.now(timezone.utc)
    expires = _aware(link.code_expires_at) if link else None
    if link is None or not link.pending_chat_id or not link.code_hash or expires is None:
        raise TelegramLinkError("No link is pending. Ask for a new code first")
    if expires <= now or link.attempts >= LINK_MAX_ATTEMPTS:
        _clear_pending(link)
        await session.flush()
        raise TelegramLinkError("That code expired or was tried too often. Ask for a new code")
    candidate = _code_hash(principal_id, link.pending_chat_id, (code or "").strip())
    if not hmac.compare_digest(candidate, link.code_hash):
        link.attempts += 1
        await session.flush()
        raise TelegramLinkError("That code is not right. Check the latest message from the bot")
    link.chat_id = link.pending_chat_id
    link.linked_at = now
    _clear_pending(link)
    await session.flush()
    return link


def _clear_pending(link: TeamTelegramLink) -> None:
    link.pending_chat_id = None
    link.code_hash = None
    link.code_expires_at = None
    link.attempts = 0


async def unlink_telegram(session: AsyncSession, principal_id: int) -> bool:
    result = await session.execute(delete(TeamTelegramLink).where(TeamTelegramLink.principal_id == principal_id))
    return bool(getattr(result, "rowcount", 0))


__all__ = ["INBOX_LIMIT", "MAX_RECIPIENTS", "REVIEW_KIND", "TelegramLinkError", "cancel_telegram_link",
           "get_telegram_link",
           "list_notifications", "mark_all_read", "mark_read", "notify_review_needed", "record_review_needed",
           "review_dedupe_key", "review_recipients", "start_telegram_link", "telegram_link_dict",
           "unlink_telegram", "verify_telegram_link"]
