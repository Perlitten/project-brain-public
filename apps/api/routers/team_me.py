"""``/team/me`` (who am I, what can I touch), ``/team/activity`` (the shared team log), and the
caller's own notifications inbox and Telegram link (``/team/me/notifications``, ``/team/me/telegram``).

Both work in solo state, where the owner is a team of one and the implicit
administrator of every repository. ``/team/activity`` computes what the caller
may read with the central team-access helpers before any event is retrieved and
hands that scope to ``brain.team.activity``, which applies it inside every
source query; nothing is fetched and filtered afterwards.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from apps.api import team_access
from apps.api.auth import require_principal, require_scope
from apps.api.routers.team import team_session
from brain.alerts import telegram
from brain.auth.principals import ALL_SCOPES, ResolvedPrincipal
from brain.config.settings import settings
from brain.database.models import Repository
from brain.memory import proposals
from brain.memory.repo_scope import normalize_repo_scope
from brain.team.activity import (DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET, ActivityQuery, ActivityScope, UnknownKind,
                                 list_activity, parse_kinds)
from brain.team.authorization import (FULL_AUTHORITY, ROLE_PERMISSIONS, Permission, TeamDenied, human_member,
                                      live_credential, require_team_storage)
from brain.team import notifications
from brain.team.models import AgentRun, ProjectMember, ProjectRepository, TeamMember, TeamProject

router = APIRouter(prefix="/team", tags=["experimental-team"])
logger = logging.getLogger(__name__)


def _effective_scopes(principal: ResolvedPrincipal) -> list[str]:
    """Granted scopes plus the reads a write grant implies (``core:write`` -> ``core:read``)."""
    granted = set(principal.scopes)
    if ALL_SCOPES in granted:
        return [ALL_SCOPES]
    implied = {f"{scope.rpartition(':')[0]}:read" for scope in granted
               if scope.endswith(":write") and scope.rpartition(":")[0]}
    return sorted(granted | implied)


def _permission_list(values) -> list[str]:
    return sorted(str(value) for value in values)


def _policy(values: list[str] | None) -> frozenset[Permission]:
    try:
        return frozenset(Permission(value) for value in values or [])
    except ValueError:
        return frozenset()  # an invalid policy grants nothing, as ``authorize`` denies it


async def _memberships(principal: ResolvedPrincipal) -> tuple[dict | None, list[dict]]:
    """The caller's team membership row and live project roles, with each project's repositories."""
    async with team_access.async_session_factory() as session:
        member = (await session.execute(select(TeamMember).where(TeamMember.principal_id == principal.id,
                                                                 TeamMember.team_id == 1))).scalar_one_or_none()
        rows = (await session.execute(
            select(ProjectMember.role, TeamProject)
            .join(TeamProject, TeamProject.id == ProjectMember.project_id)
            .where(ProjectMember.principal_id == principal.id, ProjectMember.revoked_at.is_(None))
            .order_by(TeamProject.name)
        )).all()
        project_ids = [project.id for _, project in rows]
        repositories: dict[uuid.UUID, list[dict]] = {project_id: [] for project_id in project_ids}
        if project_ids:
            for project_id, repository_id, name, path in (await session.execute(
                    select(ProjectRepository.project_id, Repository.id, Repository.name, Repository.path)
                    .join(Repository, Repository.id == ProjectRepository.repository_id)
                    .where(ProjectRepository.project_id.in_(project_ids))
                    .order_by(Repository.name))).all():
                repositories[project_id].append({"id": repository_id, "name": name, "path": path})
    membership = None
    if member is not None:
        membership = {
            "verified": bool(member.verified_at), "is_admin": bool(member.is_admin),
            "revoked": member.revoked_at is not None,
            "verified_at": member.verified_at.isoformat() if member.verified_at else None,
        }
    projects: list[dict] = []
    credential_permissions = frozenset(permission for permission in Permission
                                       if principal.has_scope(f"team:{permission.value}"))
    for role, project in rows:
        role_permissions = ROLE_PERMISSIONS.get(role, frozenset())
        policy = _policy(project.permissions)
        disabled = project.disabled_at is not None
        projects.append({
            "id": str(project.id), "name": project.name, "role": role, "disabled": disabled,
            "role_permissions": _permission_list(role_permissions),
            "project_policy": _permission_list(policy),
            "effective_permissions": [] if disabled else _permission_list(
                role_permissions & policy & credential_permissions
            ),
            "repositories": repositories.get(project.id, []),
        })
    return membership, projects


async def _pending_review_count(principal: ResolvedPrincipal, state: str, is_admin: bool,
                                projects: list[dict]) -> int:
    """Pending memory proposals the caller could decide on, never counting their own.

    Solo: every pending proposal. Team administrator: every scope. Member: the
    repositories of projects where their effective permissions include
    ``knowledge.approve`` (Reviewer). Best effort: a failure reports 0."""
    try:
        if state == "solo":
            return await proposals.count_pending_proposals()
        if is_admin:
            return await proposals.count_pending_proposals(exclude_author=principal.author_id,
                                                           exclude_evidence_author=principal.author_id)
        paths = {normalize_repo_scope(repository.get("path"))
                 for project in projects if Permission.APPROVE.value in project["effective_permissions"]
                 for repository in project["repositories"]}
        scopes: list[str | None] = [path for path in sorted(p for p in paths if p)]
        if not scopes:
            return 0
        return await proposals.count_pending_proposals(scopes=scopes, exclude_author=principal.author_id,
                                                       exclude_evidence_author=principal.author_id)
    except Exception as exc:  # the identity endpoint must answer even if the review queue cannot be read
        logger.warning("Pending review count failed: %s: %s", type(exc).__name__, exc)
        return 0


@router.get("/me")
async def team_me(principal: ResolvedPrincipal = Depends(require_principal)):
    """Identity, team state, team-admin status, project roles and scopes of the presented credential."""
    state = await team_access.current_team_state()
    is_admin = await team_access.is_team_administrator(principal)
    membership: dict | None = None
    projects: list[dict] = []
    if settings.BRAIN_STORAGE_MODE == "full" and principal.id > 0:
        membership, projects = await _memberships(principal)
    access: dict[str, object]
    if state == "solo":
        access = {"repositories": "all", "permissions": _permission_list(FULL_AUTHORITY),
                  "reason": "Solo state: the owner is a team of one and administers every repository."}
    elif is_admin:
        access = {"repositories": "all", "permissions": _permission_list(FULL_AUTHORITY),
                  "reason": "Team administrator: every repository, including ones attached to no project."}
    else:
        access = {"repositories": "projects", "permissions": None,
                  "reason": "Team member: the repositories of your projects, with each project's "
                            "effective permissions."}
    return {
        "principal": {"id": principal.id, "name": principal.name, "kind": principal.kind,
                      "credential_id": principal.credential_id},
        "team_state": state,
        "is_team_admin": is_admin,
        "team_membership": membership,
        "projects": projects,
        "access": access,
        "scopes": sorted(principal.scopes),
        "effective_scopes": _effective_scopes(principal),
        "can_read_activity": principal.has_scope("team:read"),
        # Memory proposals awaiting a decision this caller may make (the review queue badge).
        "pending_review_count": await _pending_review_count(principal, state, is_admin, projects),
    }


async def _project_repository_ids(project_id: uuid.UUID) -> list[int] | None:
    """Repository ids attached to ``project_id``; ``None`` when the project does not exist."""
    async with team_access.async_session_factory() as session:
        project = await session.get(TeamProject, project_id)
        if project is None:
            return None
        return list((await session.execute(select(ProjectRepository.repository_id)
                                           .where(ProjectRepository.project_id == project_id))).scalars())


async def _activity_scope(principal: ResolvedPrincipal, *, project_id: uuid.UUID | None,
                          repository_id: int | None) -> ActivityScope:
    """What the caller may read, from the central team-access helpers, before any event is fetched."""
    administrator = await team_access.is_team_administrator(principal)
    task_id: uuid.UUID | None = None
    if not administrator:
        try:
            async with team_access.async_session_factory() as session:
                live = await live_credential(session, principal.id, principal.credential_id)
                if live.kind == "human":
                    await human_member(session, live, 1)
                elif live.kind == "run":
                    run = (await session.execute(select(AgentRun).where(
                        AgentRun.principal_id == live.id,
                        AgentRun.credential_id == live.credential_id,
                        AgentRun.revoked_at.is_(None),
                    ))).scalar_one_or_none()
                    if run is None:
                        raise TeamDenied("Run identity mismatch")
                    initiator = await live_credential(session, run.initiator_id, run.initiator_credential_id)
                    await human_member(session, initiator, 1)
                    task_id = run.task_id
                else:
                    raise TeamDenied("Verified team membership is required")
        except TeamDenied as exc:
            raise HTTPException(403, f"Team member required: {exc}") from exc
    project_ids: frozenset[uuid.UUID] | None = None
    repository_ids: frozenset[int] | None = None
    narrowed = project_id is not None or repository_id is not None

    if project_id is not None:
        readable_projects = await team_access.readable_project_ids(principal)
        if readable_projects is not None and project_id not in readable_projects:
            raise HTTPException(403, "Project access denied")
        attached = await _project_repository_ids(project_id)
        if attached is None:
            raise HTTPException(404, "Project not found")
        readable = await team_access.readable_repository_ids(principal, attached)
        repository_ids = frozenset(attached if readable is None else readable)
        project_ids = frozenset({project_id})

    if repository_id is not None:
        # The same 403 every personal route returns for an unreadable repository.
        await team_access.repository_authority(principal, Permission.READ, repository_id=repository_id)
        if repository_ids is not None and repository_id not in repository_ids:
            raise HTTPException(400, "Repository is not attached to this project")
        repository_ids = frozenset({repository_id})
        if project_ids is None:
            project_ids = frozenset()

    if narrowed:
        return ActivityScope(administrator=administrator, repository_ids=repository_ids, project_ids=project_ids,
                             include_global=False, unscoped=False, task_id=task_id)
    if administrator:
        return ActivityScope.everything()
    async with team_access.async_session_factory() as session:
        every_repository = list((await session.execute(select(Repository.id))).scalars())
    readable = await team_access.readable_repository_ids(principal, every_repository)
    readable_projects = await team_access.readable_project_ids(principal)
    return ActivityScope(
        administrator=False,
        repository_ids=None if readable is None else frozenset(readable),
        project_ids=None if readable_projects is None else frozenset(readable_projects),
        include_global=task_id is None, unscoped=False, task_id=task_id,
    )


@router.get("/activity", dependencies=[Depends(require_scope("team:read"))])
async def team_activity(
    principal: ResolvedPrincipal = Depends(require_principal),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    project_id: uuid.UUID | None = Query(None),
    repository_id: int | None = Query(None, gt=0),
    actor_id: int | None = Query(None, gt=0),
    kind: list[str] = Query(default_factory=list),
    since: datetime | None = Query(None),
):
    """Newest-first team activity: memory changes, membership, invitations, tasks, runs and queued jobs.

    Members read the events of projects and repositories they can read plus
    global decisions and rules; team administration (membership, roles,
    invitations) and administrator-only memory need a team administrator."""
    try:
        require_team_storage()
    except TeamDenied as exc:
        raise HTTPException(403, "Team activity requires full storage") from exc
    try:
        kinds = parse_kinds(kind)
    except UnknownKind as exc:
        raise HTTPException(422, str(exc)) from exc
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    scope = await _activity_scope(principal, project_id=project_id, repository_id=repository_id)
    query = ActivityQuery(limit=limit, offset=offset, actor_id=actor_id, kinds=kinds, since=since)
    async with team_access.async_session_factory() as session:
        page = await list_activity(session, scope, query)
    page["filters"] = {"project_id": str(project_id) if project_id else None, "repository_id": repository_id,
                       "actor_id": actor_id, "kind": sorted(kinds),
                       "since": since.isoformat() if since else None}
    page["scope"] = {"administrator": scope.administrator,
                     "repositories": "all" if scope.repository_ids is None else len(scope.repository_ids)}
    return page


# The caller's own notifications inbox and Telegram link. Every route acts on the principal the
# presented credential belongs to (``member_authority``): no request names another member.


class TelegramLinkRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=32)


class TelegramVerifyRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16)


async def _inbox_owner(principal: ResolvedPrincipal) -> int | None:
    """The caller's principal id in team state (403 unless a live, verified human member); ``None`` in solo."""
    authority = await team_access.member_authority(principal)
    return None if authority.team_state == "solo" else authority.initiator_id


@router.get("/me/notifications")
async def my_notifications(principal: ResolvedPrincipal = Depends(require_principal),
                           unread_only: bool = Query(False),
                           limit: int = Query(50, ge=1, le=notifications.LIST_LIMIT)):
    """The caller's newest notifications (memory proposals awaiting their review). Solo state: empty."""
    owner = await _inbox_owner(principal)
    if owner is None or settings.BRAIN_STORAGE_MODE != "full":
        return {"notifications": [], "unread_count": 0, "team_state": "solo"}
    async with team_access.async_session_factory() as session:
        page = await notifications.list_notifications(session, owner, unread_only=unread_only, limit=limit)
    page["team_state"] = "team"
    return page


@router.post("/me/notifications/read-all")
async def read_all_my_notifications(principal: ResolvedPrincipal = Depends(require_principal),
                                    session=Depends(team_session, scope="function")):
    owner = await _inbox_owner(principal)
    if owner is None:
        return {"updated": 0}
    return {"updated": await notifications.mark_all_read(session, owner)}


@router.post("/me/notifications/{notification_id}/read")
async def read_my_notification(notification_id: int, principal: ResolvedPrincipal = Depends(require_principal),
                               session=Depends(team_session, scope="function")):
    owner = await _inbox_owner(principal)
    row = None if owner is None else await notifications.mark_read(session, owner, notification_id)
    if row is None:
        # Someone else's notification answers exactly like a missing one.
        raise HTTPException(404, "Notification not found")
    return notifications.notification_dict(row)


@router.get("/me/telegram")
async def my_telegram_link(principal: ResolvedPrincipal = Depends(require_principal)):
    """Whether the caller's own chat is linked for review notices (the chat id is masked)."""
    owner = await _inbox_owner(principal)
    available = telegram.member_telegram_available()
    if owner is None or settings.BRAIN_STORAGE_MODE != "full":
        return {**notifications.telegram_link_dict(None, available=False), "team_state": "solo"}
    async with team_access.async_session_factory() as session:
        link = await notifications.get_telegram_link(session, owner)
    return {**notifications.telegram_link_dict(link, available=available), "team_state": "team"}


async def _team_owner(principal: ResolvedPrincipal) -> int:
    owner = await _inbox_owner(principal)
    if owner is None:
        raise HTTPException(409, "Per-member Telegram links are for team state; solo uses TELEGRAM_ALERT_CHAT_ID")
    return owner


@router.post("/me/telegram", status_code=202)
async def start_my_telegram_link(body: TelegramLinkRequest, principal: ResolvedPrincipal = Depends(require_principal),
                                 session=Depends(team_session, scope="function")):
    """Send a one-time code to ``chat_id``; the link takes effect once the caller types it back."""
    owner = await _team_owner(principal)
    if not telegram.member_telegram_available():
        raise HTTPException(409, "Telegram review notices are not enabled on this Brain")
    try:
        link, code = await notifications.start_telegram_link(session, owner, body.chat_id)
    except notifications.TelegramLinkError as exc:
        raise HTTPException(400, str(exc)) from exc
    sent = await telegram.deliver_member_link_code(chat_id=link.pending_chat_id or "", code=code)
    if sent.get("status") != "sent":
        # Rolls the pending code back: nothing was delivered, so nothing can be verified.
        raise HTTPException(502, "The bot could not send the code. Start a chat with the bot first, then retry")
    return notifications.telegram_link_dict(link, available=True)


@router.post("/me/telegram/verify")
async def verify_my_telegram_link(body: TelegramVerifyRequest,
                                  principal: ResolvedPrincipal = Depends(require_principal),
                                  session=Depends(team_session, scope="function")):
    owner = await _team_owner(principal)
    try:
        link = await notifications.verify_telegram_link(session, owner, body.code)
    except notifications.TelegramLinkError as exc:
        # A response, not an exception: the failed attempt is committed, so attempts stay bounded.
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return notifications.telegram_link_dict(link, available=telegram.member_telegram_available())


@router.delete("/me/telegram")
async def unlink_my_telegram(principal: ResolvedPrincipal = Depends(require_principal),
                             session=Depends(team_session, scope="function")):
    owner = await _team_owner(principal)
    return {"unlinked": await notifications.unlink_telegram(session, owner)}
