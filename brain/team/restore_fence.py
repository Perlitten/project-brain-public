"""Post-restore access fence and its reconciliation.

A backup is a snapshot. Revocations, cancellations and policy changes made
after it are missing from the snapshot, so a plain restore would make revoked
credentials and members valid again. The restore therefore writes an active
``TeamAccessFence`` row in the same transaction as the dump whenever the
snapshot has team members (``scripts/team_restore_fence.sql``). While that row
is active:

* ``resolve_credential`` honours no named credential (every API, MCP and
  Telegram caller is unauthenticated);
* ``team_state`` reports ``"team"``, so the shared key and dev-open access are
  refused as well;
* ``live_credential`` denies every team authorization, including the worker;
* OAuth issuance and refresh rotation are refused;
* invitations cannot be accepted.

``reconcile`` is the only way to clear the fence. The operator first reviews
the restored authorization state (``review``), corrects it with the
break-glass ``brain team`` commands, then attests the digest of exactly that
state. By default reconciliation revokes every restored credential, OAuth
refresh token, run delegation and pending invitation, because nothing in the
snapshot proves which of them were revoked after it. Keeping them is a separate, explicit
attestation that the snapshot is current (for example an upgrade rollback to a
backup taken with the API stopped). A missing or stale attestation leaves the
fence in place: the state is fail-closed.

This module must not import ``brain.team.authorization`` at import time;
``authorization`` imports it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import inspect, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from brain.config.settings import settings
from brain.database.harness_models import AgentTask
from brain.database.models import ApiCredential, OAuthRefreshToken, Principal
from brain.team.models import (AgentRun, ProjectMember, ProjectRepository, TeamAccessFence, TeamIdentity, TeamInvitation,
                               TeamMember, TeamProject, TeamTaskBinding)

FENCED = "Team access is fenced after a restore: run `brain team reconcile`"


async def access_fenced(session: AsyncSession) -> bool:
    """True while a restore fence is active. Lite storage has no team tables and is never fenced."""
    if settings.BRAIN_STORAGE_MODE != "full":
        return False
    fence = (await session.execute(select(TeamAccessFence.id).where(TeamAccessFence.cleared_at.is_(None))
                                   .limit(1))).scalar_one_or_none()
    return fence is not None


async def current_team_access() -> str:
    """``"open"`` or ``"pending_reconciliation"`` for this Brain (``/health`` and ``brain doctor``)."""
    from brain.database.session import async_session_factory

    async with async_session_factory() as session:
        return "pending_reconciliation" if await access_fenced(session) else "open"


async def fence_table_exists(session: AsyncSession) -> bool:
    """The restore SQL creates the table whenever it fences, so a missing table means no fence."""
    return await session.run_sync(lambda sync: inspect(sync.connection()).has_table(TeamAccessFence.__tablename__))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class Review:
    """The authorization state an operator attests, with its digest."""

    state: dict[str, Any]
    digest: str

    @property
    def fenced(self) -> bool:
        return bool(self.state["fences"])

    def summary(self) -> dict[str, Any]:
        """Operator-facing view: the roster in full, credential material as counts only."""
        state = self.state
        return {
            "team_access": "pending_reconciliation" if self.fenced else "open",
            "digest": self.digest,
            "fences": state["fences"],
            "members": state["members"],
            "sso_identities": state["sso_identities"],
            "projects": state["projects"],
            "project_members": state["project_members"],
            "repositories": state["repositories"],
            "open_tasks": [task for task in state["tasks"] if task["status"] not in {"cancelled", "completed"}],
            "to_revoke_unless_kept": {"credentials": len(state["credentials"]),
                                      "oauth_refresh_tokens": len(state["refresh_tokens"]),
                                      "runs": len(state["runs"]),
                                      "pending_invitations": len(state["invitations"])},
        }


async def review(session: AsyncSession) -> Review:
    """Snapshot of every row that decides who may do what, and a SHA-256 over it.

    The digest covers the active fences, team and project membership, SSO
    identity bindings, project policy, repository links, task status and
    cancellation, and the ids of every unrevoked credential, refresh token, run
    delegation and pending invitation. Any change between review and ``reconcile`` changes the digest."""
    fences = (await session.execute(select(TeamAccessFence).where(TeamAccessFence.cleared_at.is_(None))
                                    .order_by(TeamAccessFence.id))).scalars().all()
    members = (await session.execute(select(TeamMember, Principal.name)
                                     .join(Principal, Principal.id == TeamMember.principal_id)
                                     .order_by(TeamMember.principal_id))).all()
    identities = (await session.execute(select(TeamIdentity)
                                        .order_by(TeamIdentity.issuer, TeamIdentity.subject))).scalars().all()
    projects = (await session.execute(select(TeamProject).order_by(TeamProject.name, TeamProject.id))).scalars().all()
    roles = (await session.execute(select(ProjectMember)
                                   .order_by(ProjectMember.project_id, ProjectMember.principal_id))).scalars().all()
    links = (await session.execute(select(ProjectRepository)
                                   .order_by(ProjectRepository.repository_id))).scalars().all()
    tasks = (await session.execute(select(TeamTaskBinding.task_id, TeamTaskBinding.project_id, AgentTask.status,
                                          AgentTask.cancellation_requested_at)
                                   .join(AgentTask, AgentTask.id == TeamTaskBinding.task_id)
                                   .order_by(TeamTaskBinding.task_id))).all()
    credentials = (await session.execute(select(ApiCredential).where(ApiCredential.revoked_at.is_(None))
                                         .order_by(ApiCredential.id))).scalars().all()
    tokens = (await session.execute(select(OAuthRefreshToken).where(OAuthRefreshToken.revoked_at.is_(None))
                                    .order_by(OAuthRefreshToken.id))).scalars().all()
    runs = (await session.execute(select(AgentRun).where(AgentRun.revoked_at.is_(None))
                                  .order_by(AgentRun.id))).scalars().all()
    invites = (await session.execute(select(TeamInvitation).where(TeamInvitation.accepted_at.is_(None),
                                                                  TeamInvitation.revoked_at.is_(None))
                                     .order_by(TeamInvitation.id))).scalars().all()
    state: dict[str, Any] = {
        "fences": [{"id": f.id, "reason": f.reason, "fenced_at": _iso(f.fenced_at)} for f in fences],
        "members": [{"principal_id": m.principal_id, "name": name, "team_id": m.team_id, "is_admin": bool(m.is_admin),
                     "verified": m.verified_at is not None, "revoked": m.revoked_at is not None,
                     "issuer": m.verification_issuer, "subject": m.verification_subject} for m, name in members],
        # Who may sign in with SSO as which member (brain.team.oidc).
        "sso_identities": [{"principal_id": i.principal_id, "issuer": i.issuer, "subject": i.subject}
                           for i in identities],
        "projects": [{"id": str(p.id), "name": p.name, "permissions": sorted(p.permissions or []),
                      "version": p.version, "disabled": p.disabled_at is not None} for p in projects],
        "project_members": [{"project_id": str(r.project_id), "principal_id": r.principal_id, "role": r.role,
                             "version": r.version, "revoked": r.revoked_at is not None} for r in roles],
        "repositories": [{"repository_id": link.repository_id, "project_id": str(link.project_id)} for link in links],
        "tasks": [{"task_id": str(task_id), "project_id": str(project_id), "status": status,
                   "cancellation_requested": cancelled is not None}
                  for task_id, project_id, status, cancelled in tasks],
        "credentials": [{"id": c.id, "principal_id": c.principal_id, "scopes": sorted(c.scopes or []),
                         "expires_at": _iso(c.expires_at)} for c in credentials],
        "refresh_tokens": [{"id": t.id, "client_id": t.client_id, "principal_name": t.principal_name,
                            "access_credential_id": t.access_credential_id} for t in tokens],
        "runs": [{"id": str(r.id), "task_id": str(r.task_id), "principal_id": r.principal_id,
                  "credential_id": r.credential_id} for r in runs],
        "invitations": [{"id": str(i.id), "principal_name": i.principal_name, "is_admin": bool(i.is_admin),
                         "role": i.role, "project_ids": sorted(i.project_ids or []), "expires_at": _iso(i.expires_at)}
                        for i in invites],
    }
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return Review(state=state, digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest())


async def reconcile(session: AsyncSession, *, attest: str, keep_credentials: bool = False) -> dict[str, Any]:
    """Clear every active fence after the operator attests the reviewed state.

    ``attest`` must equal the digest ``review`` returns for the current state;
    otherwise nothing changes and ``TeamConflict`` is raised. Unless
    ``keep_credentials`` is set, every unrevoked credential, OAuth refresh
    token, run delegation and pending invitation is revoked in the same
    transaction, so access reopens only for keys minted after reconciliation."""
    from brain.team.authorization import TeamConflict

    fences = (await session.execute(select(TeamAccessFence).where(TeamAccessFence.cleared_at.is_(None))
                                    .order_by(TeamAccessFence.id).with_for_update()
                                    .execution_options(populate_existing=True))).scalars().all()
    if not fences:
        raise TeamConflict("No restore fence is pending: team access is open")
    current = await review(session)
    if not hmac.compare_digest(current.digest, attest.strip().lower()):
        raise TeamConflict("The authorization state does not match the attested digest; "
                           "run `brain team reconcile` again and review the new state")
    now = datetime.now(timezone.utc)
    revoked = {"credentials": 0, "oauth_refresh_tokens": 0, "runs": 0, "pending_invitations": 0}
    if not keep_credentials:
        for key, model in (("credentials", ApiCredential), ("oauth_refresh_tokens", OAuthRefreshToken),
                           ("runs", AgentRun)):
            result = await session.execute(update(model).where(model.revoked_at.is_(None)).values(revoked_at=now))
            revoked[key] = cast(CursorResult[Any], result).rowcount or 0
        result = await session.execute(update(TeamInvitation).where(
            TeamInvitation.accepted_at.is_(None), TeamInvitation.revoked_at.is_(None)).values(
            revoked_at=now, revoked_by="operator:brain team reconcile"))
        revoked["pending_invitations"] = cast(CursorResult[Any], result).rowcount or 0
    note = (f"brain team reconcile; digest={current.digest}; "
            f"credentials={'kept' if keep_credentials else 'rotated'}")
    for fence in fences:
        fence.cleared_at = now
        fence.cleared_by = note
    await session.flush()
    return {"team_access": "open", "cleared_fences": [fence.id for fence in fences], "digest": current.digest,
            "credentials": "kept" if keep_credentials else "rotated", "revoked": revoked}
