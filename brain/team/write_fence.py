"""Write fence: a write authorized before a revocation does not land after it.

Personal routes authorize in one short transaction (``apps/api/team_access.py``)
and write in another. Without a fence, a member revoked between the two still
commits whatever the request was about to write. The fence closes that gap
inside the write transaction itself:

* ``WriteFenceMiddleware`` gives every HTTP request a holder (a ContextVar);
* team-state authorization ``arm``s it with the identity it just authorized
  (the caller, and the human initiator for a run);
* before the first write of each transaction opened during that request
  (an ORM flush, or an INSERT/UPDATE/DELETE through ``Session.execute``) the
  fence re-reads the restore fence, the principals, their credentials and the
  initiator's team membership ``FOR SHARE`` and raises ``TeamDenied`` when any
  of them is no longer live.

The share locks follow the global lock order (``brain/team/authorization.py``)
and are held until the write commits, so a concurrent revocation (which locks
those principals ``FOR NO KEY UPDATE``) either waits for the write and sees it
(its proposals are withdrawn with the rest), or commits first and the write is
refused. The check costs three indexed reads once per write transaction; reads
and unarmed requests (solo state, unauthenticated or not-yet-authorized code,
background workers) are untouched.

Raw ``text()`` SQL is not recognised as a write; team-state writes go through
the ORM or SQLAlchemy Core.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, func, or_, select
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction

from brain.database.models import ApiCredential, Principal
from brain.team.models import TeamAccessFence, TeamMember

DENIED = "Credential is no longer authorized"
_CHECKED = "team_write_fence_checked"
_CHECKING = "team_write_fence_checking"


@dataclass(frozen=True)
class Identity:
    actor_id: int
    actor_credential_id: int
    initiator_id: int
    initiator_credential_id: int
    team_id: int = 1


@dataclass
class _Holder:
    identity: Identity | None = None


_current: ContextVar[_Holder | None] = ContextVar("team_write_fence", default=None)


@contextmanager
def request_scope() -> Iterator[None]:
    """One request's fence holder; nothing is fenced until ``arm`` is called inside it."""
    token = _current.set(_Holder())
    try:
        yield
    finally:
        _current.reset(token)


def arm(actor_id: int, actor_credential_id: int | None, initiator_id: int,
        initiator_credential_id: int | None, team_id: int = 1) -> None:
    """Fence the rest of this request's writes on these identities. Outside a request it does nothing."""
    holder = _current.get()
    if holder is None or actor_credential_id is None or initiator_credential_id is None:
        return
    holder.identity = Identity(actor_id, actor_credential_id, initiator_id, initiator_credential_id, team_id)


def _innermost(session: Session) -> SessionTransaction | None:
    return session.get_nested_transaction() or session.get_transaction()


def _checked(session: Session) -> bool:
    """Whether this transaction (or an enclosing, still active one) already passed the fence."""
    done = session.info.get(_CHECKED)
    transaction = _innermost(session)
    while transaction is not None:
        if transaction.is_active and transaction is done:
            return True
        transaction = transaction.parent
    return False


def _verify(session: Session, identity: Identity) -> None:
    from brain.team.authorization import TeamDenied
    from brain.team.restore_fence import FENCED

    connection = session.connection()
    if connection.execute(select(TeamAccessFence.id).where(TeamAccessFence.cleared_at.is_(None))
                          .limit(1)).first() is not None:
        raise TeamDenied(FENCED)
    principal_ids = sorted({identity.actor_id, identity.initiator_id}, reverse=True)  # runs before their initiator
    live_principals = connection.execute(
        select(Principal.id).where(Principal.id.in_(principal_ids), Principal.disabled_at.is_(None))
        .order_by(Principal.id.desc()).with_for_update(read=True)).scalars().all()
    credentials = {identity.actor_credential_id: identity.actor_id,
                   identity.initiator_credential_id: identity.initiator_id}
    live_credentials = connection.execute(
        select(ApiCredential.id, ApiCredential.principal_id)
        .where(ApiCredential.id.in_(sorted(credentials)), ApiCredential.revoked_at.is_(None),
               or_(ApiCredential.expires_at.is_(None), ApiCredential.expires_at > func.now()))
        .order_by(ApiCredential.id).with_for_update(read=True)).all()
    member = connection.execute(
        select(TeamMember.principal_id)
        .where(TeamMember.principal_id == identity.initiator_id, TeamMember.team_id == identity.team_id,
               TeamMember.revoked_at.is_(None), TeamMember.verified_at.is_not(None))
        .with_for_update(read=True)).first()
    if (len(live_principals) != len(principal_ids) or member is None
            or {row.id: row.principal_id for row in live_credentials} != credentials):
        raise TeamDenied(DENIED)


def _fence(session: Session) -> None:
    holder = _current.get()
    if holder is None or holder.identity is None or session.info.get(_CHECKING) or _checked(session):
        return
    session.info[_CHECKING] = True
    try:
        _verify(session, holder.identity)
    finally:
        session.info.pop(_CHECKING, None)
    session.info[_CHECKED] = _innermost(session)


@event.listens_for(Session, "before_flush")
def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    if session.new or session.dirty or session.deleted:
        _fence(session)


@event.listens_for(Session, "do_orm_execute")
def _before_execute(state: ORMExecuteState) -> None:
    if state.is_insert or state.is_update or state.is_delete:
        _fence(state.session)


class WriteFenceMiddleware:
    """Pure ASGI middleware: one fence holder per request (no task boundary, so ``arm`` reaches it)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        with request_scope():
            await self.app(scope, receive, send)


__all__ = ["DENIED", "Identity", "WriteFenceMiddleware", "arm", "request_scope"]
