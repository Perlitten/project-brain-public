"""Team activity feed: a newest-first, paginated log of who changed what.

The feed is assembled at query time from the tables that already carry the
history, plus one additive append-only table for the changes that leave no
other dated trace:

* decision and rule versions (the PR #129 supersede chain: version ``v`` of a
  row happened when the history row it supersedes was written, version 1 at
  ``created_at``), attributed to ``updated_by_principal_id`` / the author;
* learnings, episodes and skills: creation by ``author_principal_id`` and the
  latest change by ``updated_by_principal_id`` (those tables keep no history).
  Personal-tier learnings (status ``personal``) are private to their author and
  never appear in the feed; a promoted learning appears once it is shared;
* invitations (created, accepted, revoked) from ``team_invitations``;
* team tasks and runs from ``team_task_bindings`` / ``agent_runs``;
* membership and project-role changes, single sign-on bindings, queued jobs,
  coordination events (scope overlaps, integration queue outcomes) and memory
  review events (``memory.proposed`` / ``memory.approved`` / ``memory.rejected``)
  from ``team_activity_events`` (written by provisioning, ``brain.team.oidc``, the
  job endpoints, ``brain/team/coordination.py`` and ``brain/memory/proposals.py``).

Authorization is never applied to fetched events. The caller passes an
``ActivityScope`` that the API computed with the central team-access helpers
before any event is read, and every source query carries that scope as SQL
predicates; a source the scope excludes is not queried at all. This module
does not import the API layer (``brain/`` never imports ``apps/``).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import ColumnElement, Select, String, and_, cast, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from brain.database.harness_models import AgentTask
from brain.database.models import Decision, Learning, MemoryEpisode, MemorySkill, Principal, Repository, Rule
from brain.database.repository_utils import find_repository_id, repository_path_candidates
from brain.memory.repo_scope import normalize_repo_scope
from brain.team.models import AgentRun, ProjectRepository, TeamActivityEvent, TeamInvitation, TeamTaskBinding

# The personal tier (brain.memory.proposals.PERSONAL_STATUS): only the author ever reads those learnings.
PERSONAL_LEARNING_STATUS = "personal"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_OFFSET = 5000

# Every kind the feed can return, grouped by family; filters accept either.
KINDS: dict[str, tuple[str, ...]] = {
    "decision": ("decision.created", "decision.updated"),
    "rule": ("rule.created", "rule.updated"),
    "learning": ("learning.created", "learning.updated"),
    "episode": ("episode.created", "episode.updated"),
    "skill": ("skill.created", "skill.updated"),
    "membership": ("membership.added", "membership.revoked", "membership.admin_granted",
                   "membership.admin_revoked"),
    "project_role": ("project_role.granted", "project_role.changed", "project_role.revoked"),
    "execution": ("execution.granted", "execution.revoked"),
    "invitation": ("invitation.created", "invitation.accepted", "invitation.revoked"),
    "sso": ("sso.bound", "sso.unbound"),
    "task": ("task.created",),
    "run": ("run.requested",),
    "job": ("job.queued",),
    "coordination": ("coordination.overlap", "coordination.integration_queued", "coordination.integrated",
                     "coordination.needs_rebase", "coordination.stale_instructions",
                     "coordination.integration_rejected"),
    # Governed memory review: a proposal was submitted, approved or rejected (no draft content in the event).
    "memory": ("memory.proposed", "memory.approved", "memory.rejected"),
}
ALL_KINDS = frozenset(kind for kinds in KINDS.values() for kind in kinds)
# Families only team administrators read: team administration itself, and the
# episode/skill memory whose API is administrator-only as well.
ADMIN_FAMILIES = frozenset({"membership", "project_role", "execution", "invitation", "sso", "episode", "skill"})
TABLE_FAMILIES = frozenset({"membership", "project_role", "execution", "sso", "job", "coordination", "memory"})

_PRINCIPAL_LABEL = re.compile(r"^principal:(\d+)$")


class UnknownKind(ValueError):
    """A ``kind`` filter names neither a family nor a kind of the feed."""


def parse_kinds(values: Iterable[str]) -> frozenset[str]:
    """Validate kind filters: a family (``decision``) or a full kind (``decision.updated``)."""
    wanted = frozenset(value.strip() for value in values if value and value.strip())
    unknown = sorted(value for value in wanted if value not in KINDS and value not in ALL_KINDS)
    if unknown:
        raise UnknownKind(f"Unknown activity kind: {', '.join(unknown)}")
    return wanted


@dataclass(frozen=True)
class ActivityScope:
    """What the caller may read, decided by the central authorization before retrieval.

    ``administrator``: team-administration events and administrator-only memory.
    ``repository_ids``: repositories whose events are readable; ``None`` means every
    repository, including unattached ones. ``project_ids``: projects whose
    project-bound events are readable; ``None`` means every project.
    ``include_global``: global (repository-less) decisions and rules, which every
    member reads. ``unscoped``: other events bound to no readable repository or
    project (repository-less learnings and jobs, team-level membership changes,
    memory of unindexed paths); only a team administrator without a filter."""
    administrator: bool
    repository_ids: frozenset[int] | None
    project_ids: frozenset[uuid.UUID] | None
    include_global: bool = True
    unscoped: bool = False
    task_id: uuid.UUID | None = None

    @classmethod
    def everything(cls) -> "ActivityScope":
        return cls(administrator=True, repository_ids=None, project_ids=None, include_global=True, unscoped=True)


@dataclass(frozen=True)
class ActivityQuery:
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    actor_id: int | None = None
    kinds: frozenset[str] = frozenset()
    since: datetime | None = None


@dataclass
class _Stream:
    kind: str
    statement: Select
    to_event: Callable[[Any], dict]
    paths: Callable[[Any], str | None] = field(default=lambda _row: None)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _wanted(kind: str, kinds: frozenset[str]) -> bool:
    return not kinds or kind in kinds or kind.split(".", 1)[0] in kinds


def _family_allowed(family: str, scope: ActivityScope) -> bool:
    return scope.administrator or family not in ADMIN_FAMILIES


def _global_scope(column):
    """A memory row that belongs to no repository (``normalize_repo_scope`` -> ``None``)."""
    return or_(column.is_(None), func.lower(func.trim(column)).in_(["", "system_global"]))


async def _scope_spellings(session: AsyncSession, columns: list, wanted: frozenset[int]) -> dict[str, int]:
    """Stored path spellings that resolve to one of ``wanted`` repositories.

    Memory keeps the writer's spelling of ``repo_path`` / ``repo_scope``; only
    the distinct spellings are read (scope keys, never event content) and each is
    resolved through the same lookup repository authorization uses."""
    resolved: dict[str, int] = {}
    if not wanted:
        return resolved
    seen: set[str] = set()
    for column in columns:
        for (spelling,) in (await session.execute(select(column).where(column.is_not(None)).distinct())).all():
            if spelling in seen or normalize_repo_scope(spelling) is None:
                continue
            seen.add(spelling)
    candidates = {spelling: set(repository_path_candidates(spelling)) for spelling in seen}
    every_candidate = sorted({candidate for values in candidates.values() for candidate in values})
    if not every_candidate:
        return resolved
    repositories = (await session.execute(
        select(Repository.id, Repository.path).where(
            Repository.id.in_(sorted(wanted)), Repository.path.in_(every_candidate)
        )
    )).all()
    by_path = {path: repository_id for repository_id, path in repositories}
    for spelling, values in candidates.items():
        repository_id = next((by_path[value] for value in values if value in by_path), None)
        if repository_id is not None:
            resolved[spelling] = repository_id
    return resolved


def _memory_location(column, scope: ActivityScope, spellings: dict[str, int], *, global_visible: bool):
    """SQL predicate for memory keyed by a stored path: global rows plus readable repositories."""
    parts = []
    if global_visible:
        parts.append(_global_scope(column))
    if scope.repository_ids is None:
        if scope.unscoped:
            return true()
        parts.append(and_(column.is_not(None), ~_global_scope(column)))
    elif spellings:
        parts.append(column.in_(sorted(spellings)))
    return or_(*parts) if parts else false()


def _ordered(statement: Select, occurred, *ids) -> Select:
    return statement.order_by(occurred.desc(), *(column.desc() for column in ids))


def _apply_common(statement: Select, occurred, actor, query: ActivityQuery) -> Select:
    if query.since is not None:
        statement = statement.where(occurred >= query.since)
    if query.actor_id is not None:
        statement = statement.where(actor == query.actor_id) if actor is not None else statement.where(false())
    return statement


def _event(kind: str, key: str, occurred_at: datetime | None, actor_id: int | None, summary: str, *,
           subject: dict, repo_path: str | None = None, repository_id: int | None = None,
           project_id: uuid.UUID | str | None = None, actor_label: str | None = None,
           details: dict | None = None) -> dict:
    return {
        "id": key, "kind": kind, "occurred_at": _utc(occurred_at),
        "actor_principal_id": actor_id, "actor_label": actor_label, "summary": summary,
        "project_id": str(project_id) if project_id else None, "repository_id": repository_id,
        "repo_path": repo_path, "subject": subject, "details": details or {},
    }


def _clip(text: str | None, size: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[: size - 1] + "…"


def _versioned_streams(model, name_column, label: str, query: ActivityQuery, location) -> list[_Stream]:
    prev = aliased(model)
    occurred = func.coalesce(prev.updated_at, model.created_at)
    version = func.coalesce(model.version, 1)
    base = (select(model.id, name_column.label("name"), version.label("version"), model.repo_path,
                   model.status, model.superseded_by_id, model.author_principal_id,
                   model.updated_by_principal_id, occurred.label("occurred_at"))
            .outerjoin(prev, prev.id == model.supersedes_id)
            .where(location))

    def to_event(action: str):
        def build(row) -> dict:
            canonical = row.superseded_by_id if row.superseded_by_id is not None else row.id
            actor = row.author_principal_id if action == "created" else row.updated_by_principal_id
            verb = "recorded" if action == "created" else "updated"
            return _event(f"{label}.{action}", f"{label}:{row.id}:v{row.version}", row.occurred_at, actor,
                          f"{label.capitalize()} {verb}: {_clip(row.name)} (v{row.version})",
                          subject={"type": label, "id": str(canonical), "title": row.name,
                                   "version": row.version, "status": row.status},
                          repo_path=row.repo_path)
        return build

    streams = []
    for action, condition, actor in (("created", version <= 1, model.author_principal_id),
                                     ("updated", version > 1, model.updated_by_principal_id)):
        if not _wanted(f"{label}.{action}", query.kinds):
            continue
        statement = _apply_common(base.where(condition), occurred, actor, query)
        streams.append(_Stream(f"{label}.{action}", _ordered(statement, occurred, model.id), to_event(action),
                               paths=lambda row: row.repo_path))
    return streams


def _authored_streams(model, text_column, label: str, query: ActivityQuery, location) -> list[_Stream]:
    """Creation and latest change of a memory row without a version history."""
    streams = []
    for action, occurred, actor, condition in (
            ("created", model.created_at, model.author_principal_id, true()),
            ("updated", model.updated_at, model.updated_by_principal_id,
             model.updated_at > model.created_at)):
        if not _wanted(f"{label}.{action}", query.kinds):
            continue
        statement = (select(model.id, text_column.label("text"), model.status, model.repo_scope,
                            actor.label("actor_id"), occurred.label("occurred_at"))
                     .where(location, condition))
        statement = _ordered(_apply_common(statement, occurred, actor, query), occurred, model.id)

        def build(row, action=action) -> dict:
            verb = "recorded" if action == "created" else "changed"
            return _event(f"{label}.{action}", f"{label}:{row.id}:{action}", row.occurred_at, row.actor_id,
                          f"{label.capitalize()} {verb}: {_clip(row.text)}",
                          subject={"type": label, "id": str(row.id), "title": _clip(row.text, 255),
                                   "status": row.status},
                          repo_path=row.repo_scope,
                          actor_label="system" if action == "updated" and row.actor_id is None else None)
        streams.append(_Stream(f"{label}.{action}", statement, build, paths=lambda row: row.repo_scope))
    return streams


def _label_principal(label: str | None) -> int | None:
    match = _PRINCIPAL_LABEL.match(label or "")
    return int(match.group(1)) if match else None


def _invitation_streams(query: ActivityQuery, scope: ActivityScope) -> list[_Stream]:
    location: ColumnElement[bool]
    if scope.project_ids is None:
        location = true()
    elif scope.project_ids:
        # ``project_ids`` is a JSON list of UUID strings; match the quoted id in its text form.
        text = cast(TeamInvitation.project_ids, String)
        location = or_(*(text.like(f'%"{project_id}"%') for project_id in sorted(map(str, scope.project_ids))))
    else:
        location = false()
    if scope.project_ids is None and not scope.unscoped:
        location = false()
    columns = (TeamInvitation.id, TeamInvitation.principal_name, TeamInvitation.role, TeamInvitation.is_admin,
               TeamInvitation.project_ids)
    specs = (
        ("created", TeamInvitation.created_at, TeamInvitation.invited_by_principal_id, TeamInvitation.invited_by,
         true(), "Invitation issued for"),
        ("accepted", TeamInvitation.accepted_at, TeamInvitation.accepted_principal_id, None,
         TeamInvitation.accepted_at.is_not(None), "Invitation accepted by"),
        ("revoked", TeamInvitation.revoked_at, None, TeamInvitation.revoked_by,
         TeamInvitation.revoked_at.is_not(None), "Invitation revoked for"),
    )
    streams = []
    for action, occurred, actor_column, label_column, condition, verb in specs:
        kind = f"invitation.{action}"
        if not _wanted(kind, query.kinds):
            continue
        extra = [actor_column.label("actor_id") if actor_column is not None else None,
                 label_column.label("actor_label") if label_column is not None else None]
        statement = select(*columns, occurred.label("occurred_at"), *(column for column in extra if column is not None))
        statement = statement.where(location, condition)
        if query.since is not None:
            statement = statement.where(occurred >= query.since)
        if query.actor_id is not None:
            if actor_column is not None:
                statement = statement.where(actor_column == query.actor_id)
            elif label_column is not None:
                statement = statement.where(label_column == f"principal:{query.actor_id}")
        statement = _ordered(statement, occurred, TeamInvitation.id)

        def build(row, kind=kind, verb=verb, action=action) -> dict:
            label = getattr(row, "actor_label", None)
            actor_id = getattr(row, "actor_id", None)
            if actor_id is None:
                actor_id = _label_principal(label)
            projects = list(row.project_ids or [])
            return _event(kind, f"invitation:{row.id}:{action}", row.occurred_at, actor_id,
                          f"{verb} {row.principal_name}",
                          subject={"type": "invitation", "id": str(row.id), "title": row.principal_name},
                          project_id=projects[0] if len(projects) == 1 else None,
                          actor_label=None if actor_id is not None else label,
                          details={"role": row.role, "is_admin": bool(row.is_admin), "project_ids": projects})
        streams.append(_Stream(kind, statement, build))
    return streams


def _task_streams(query: ActivityQuery, scope: ActivityScope) -> list[_Stream]:
    repository: ColumnElement[bool]
    if scope.repository_ids is None:
        repository = true()
    elif scope.repository_ids:
        repository = TeamTaskBinding.repository_id.in_(sorted(scope.repository_ids))
    else:
        repository = false()
    if scope.task_id is not None:
        repository = and_(repository, TeamTaskBinding.task_id == scope.task_id)
    streams: list[_Stream] = []
    if _wanted("task.created", query.kinds):
        occurred = AgentTask.created_at
        task_statement = (select(AgentTask.id, AgentTask.title, AgentTask.status, TeamTaskBinding.project_id,
                            TeamTaskBinding.repository_id, TeamTaskBinding.author_id, TeamTaskBinding.branch,
                            occurred.label("occurred_at"))
                     .join(TeamTaskBinding, TeamTaskBinding.task_id == AgentTask.id)
                     .where(repository))
        task_statement = _ordered(_apply_common(task_statement, occurred, TeamTaskBinding.author_id, query),
                                  occurred, AgentTask.id)

        def task_event(row) -> dict:
            return _event("task.created", f"task:{row.id}", row.occurred_at, row.author_id,
                          f"Task created: {_clip(row.title)}",
                          subject={"type": "task", "id": str(row.id), "title": row.title, "status": row.status},
                          repository_id=row.repository_id, project_id=row.project_id,
                          details={"branch": row.branch})
        streams.append(_Stream("task.created", task_statement, task_event))
    if _wanted("run.requested", query.kinds):
        occurred = AgentRun.created_at
        run_statement = (select(AgentRun.id, AgentRun.task_id, AgentRun.initiator_id, AgentRun.branch,
                            AgentRun.permissions, AgentRun.revoked_at, AgentTask.title,
                            TeamTaskBinding.project_id, TeamTaskBinding.repository_id,
                            occurred.label("occurred_at"))
                     .join(TeamTaskBinding, TeamTaskBinding.task_id == AgentRun.task_id)
                     .join(AgentTask, AgentTask.id == AgentRun.task_id)
                     .where(repository))
        run_statement = _ordered(_apply_common(run_statement, occurred, AgentRun.initiator_id, query), occurred,
                                 AgentRun.id)

        def run_event(row) -> dict:
            return _event("run.requested", f"run:{row.id}", row.occurred_at, row.initiator_id,
                          f"Run requested for task: {_clip(row.title)}",
                          subject={"type": "run", "id": str(row.id), "task_id": str(row.task_id),
                                   "title": row.title},
                          repository_id=row.repository_id, project_id=row.project_id,
                          details={"branch": row.branch, "permissions": list(row.permissions or []),
                                   "revoked": row.revoked_at is not None})
        streams.append(_Stream("run.requested", run_statement, run_event))
    return streams


def _recorded_stream(query: ActivityQuery, scope: ActivityScope) -> list[_Stream]:
    if scope.task_id is not None:
        return []
    families = [family for family in sorted(TABLE_FAMILIES)
                if _family_allowed(family, scope) and any(_wanted(kind, query.kinds) for kind in KINDS[family])]
    if not families:
        return []
    event = TeamActivityEvent
    kind_filter = or_(*(event.kind.in_([kind for kind in KINDS[family] if _wanted(kind, query.kinds)])
                        for family in families))
    locations = []
    if scope.administrator:
        # Administrative rows: project-bound ones follow the project filter, team-level ones need ``unscoped``.
        if scope.project_ids is None:
            locations.append(and_(event.audience == "admin",
                                  true() if scope.unscoped else event.project_id.is_not(None)))
        elif scope.project_ids:
            locations.append(and_(event.audience == "admin", event.project_id.in_(sorted(scope.project_ids))))
    if scope.repository_ids is None:
        locations.append(and_(event.audience == "repository",
                              true() if scope.unscoped else event.repository_id.is_not(None)))
    elif scope.repository_ids:
        locations.append(and_(event.audience == "repository", event.repository_id.in_(sorted(scope.repository_ids))))
    if not locations:
        return []
    occurred = event.occurred_at
    statement = select(event).where(kind_filter, or_(*locations))
    statement = _ordered(_apply_common(statement, occurred, event.actor_principal_id, query), occurred, event.id)

    def build(row) -> dict:
        item = row[0]
        return _event(item.kind, f"event:{item.id}", item.occurred_at, item.actor_principal_id, item.summary,
                      subject={"type": item.kind.split(".", 1)[0], "principal_id": item.subject_principal_id,
                               **{key: value for key, value in (item.details or {}).items()
                                  if key in {"job_id", "job_type", "proposal_id", "proposal_kind"}}},
                      repository_id=item.repository_id, project_id=item.project_id,
                      actor_label=item.actor_label if item.actor_principal_id is None else None,
                      details=dict(item.details or {}))
    return [_Stream("recorded", statement, build)]


async def list_activity(session: AsyncSession, scope: ActivityScope, query: ActivityQuery) -> dict:
    """One page of the feed, newest first, restricted by ``scope`` inside every source query."""
    limit = max(1, min(query.limit, MAX_LIMIT))
    offset = max(0, min(query.offset, MAX_OFFSET))
    window = offset + limit + 1

    memory_columns = []
    if _family_allowed("decision", scope) and any(_wanted(kind, query.kinds) for kind in KINDS["decision"]):
        memory_columns.append(Decision.repo_path)
    if _family_allowed("rule", scope) and any(_wanted(kind, query.kinds) for kind in KINDS["rule"]):
        memory_columns.append(Rule.repo_path)
    if any(_wanted(kind, query.kinds) for kind in KINDS["learning"]):
        memory_columns.append(Learning.repo_scope)
    if scope.administrator and any(_wanted(kind, query.kinds) for kind in KINDS["episode"]):
        memory_columns.append(MemoryEpisode.repo_scope)
    if scope.administrator and any(_wanted(kind, query.kinds) for kind in KINDS["skill"]):
        memory_columns.append(MemorySkill.repo_scope)
    spellings: dict[str, int] = {}
    if scope.repository_ids is not None and memory_columns:
        spellings = await _scope_spellings(session, memory_columns, scope.repository_ids)

    streams: list[_Stream] = []
    if _family_allowed("decision", scope):
        streams += _versioned_streams(Decision, Decision.title, "decision", query,
                                      _memory_location(Decision.repo_path, scope, spellings,
                                                       global_visible=scope.include_global))
    if _family_allowed("rule", scope):
        streams += _versioned_streams(Rule, Rule.name, "rule", query,
                                      _memory_location(Rule.repo_path, scope, spellings,
                                                       global_visible=scope.include_global))
    streams += _authored_streams(Learning, Learning.statement, "learning", query,
                                 and_(_memory_location(Learning.repo_scope, scope, spellings,
                                                       global_visible=scope.unscoped),
                                      Learning.status != PERSONAL_LEARNING_STATUS))
    if _family_allowed("episode", scope):
        streams += _authored_streams(MemoryEpisode, MemoryEpisode.distilled_summary, "episode", query,
                                     _memory_location(MemoryEpisode.repo_scope, scope, spellings,
                                                      global_visible=scope.unscoped))
    if _family_allowed("skill", scope):
        streams += _authored_streams(MemorySkill, MemorySkill.name, "skill", query,
                                     _memory_location(MemorySkill.repo_scope, scope, spellings,
                                                      global_visible=scope.unscoped))
    if _family_allowed("invitation", scope):
        streams += _invitation_streams(query, scope)
    streams += _task_streams(query, scope)
    streams += _recorded_stream(query, scope)

    collected: list[tuple[datetime, int, int, dict, str | None]] = []
    for index, stream in enumerate(streams):
        rows = (await session.execute(stream.statement.limit(window))).all()
        for rank, row in enumerate(rows):
            item = stream.to_event(row)
            occurred = item["occurred_at"] or datetime.min.replace(tzinfo=timezone.utc)
            collected.append((occurred, index, rank, item, stream.paths(row)))
    # Newest first; ties keep each source's own SQL order, sources in a fixed order.
    collected.sort(key=lambda entry: (-entry[0].timestamp(), entry[1], entry[2]))
    page = collected[offset: offset + limit]

    await _describe(session, page, spellings)
    events = [entry[3] for entry in page]
    for item in events:
        item["occurred_at"] = item["occurred_at"].isoformat() if item["occurred_at"] else None
    has_more = len(collected) > offset + limit
    return {"events": events, "limit": limit, "offset": offset, "has_more": has_more,
            "next_offset": offset + limit if has_more and offset + limit <= MAX_OFFSET else None}


async def _describe(session: AsyncSession, page: list, spellings: dict[str, int]) -> None:
    """Fill repository/project ids for memory events and actor names for the page only."""
    resolved = dict(spellings)
    for _, _, _, item, path in page:
        if path is None or item["repository_id"] is not None or normalize_repo_scope(path) is None:
            continue
        if path not in resolved:
            repository_id = await find_repository_id(session, path)
            if repository_id is None:
                continue
            resolved[path] = repository_id
        item["repository_id"] = resolved[path]
    repository_ids = sorted({item["repository_id"] for *_, item, _ in page
                             if item["repository_id"] is not None and item["project_id"] is None})
    if repository_ids:
        links: dict[int, uuid.UUID] = {
            repository_id: project_id for repository_id, project_id in (await session.execute(
                select(ProjectRepository.repository_id, ProjectRepository.project_id)
                .where(ProjectRepository.repository_id.in_(repository_ids)))).all()}
        for *_, item, _ in page:
            if item["project_id"] is None and item["repository_id"] in links:
                item["project_id"] = str(links[item["repository_id"]])
    principal_ids = sorted({item["actor_principal_id"] for *_, item, _ in page if item["actor_principal_id"]}
                           | {item["subject"]["principal_id"] for *_, item, _ in page
                              if item["subject"].get("principal_id")})
    names: dict[int, str] = {}
    if principal_ids:
        names = {principal_id: name for principal_id, name in (await session.execute(
            select(Principal.id, Principal.name).where(Principal.id.in_(principal_ids)))).all()}
    for *_, item, _ in page:
        actor_id = item.pop("actor_principal_id")
        item["actor"] = {"principal_id": actor_id, "name": names.get(actor_id)} if actor_id else None
        if item["subject"].get("principal_id"):
            item["subject"]["name"] = names.get(item["subject"]["principal_id"])


async def record_event(session: AsyncSession, *, kind: str, summary: str, audience: str,
                       actor_principal_id: int | None = None, actor_label: str | None = None,
                       subject_principal_id: int | None = None, project_id: uuid.UUID | None = None,
                       repository_id: int | None = None, details: dict | None = None,
                       dedupe_key: str | None = None) -> TeamActivityEvent:
    """Append one row to ``team_activity_events`` in the caller's transaction."""
    if kind not in ALL_KINDS or kind.split(".", 1)[0] not in TABLE_FAMILIES:
        raise ValueError(f"{kind!r} is not a recorded activity kind")
    if audience not in {"admin", "repository"}:
        raise ValueError(f"Unknown activity audience {audience!r}")
    row = TeamActivityEvent(kind=kind, summary=summary[:255], audience=audience,
                            dedupe_key=dedupe_key[:255] if dedupe_key else None,
                            actor_principal_id=actor_principal_id if actor_principal_id and actor_principal_id > 0
                            else None,
                            actor_label=(actor_label or None) and actor_label[:160],
                            subject_principal_id=subject_principal_id, project_id=project_id,
                            repository_id=repository_id, details=details or {})
    session.add(row)
    await session.flush()
    return row


__all__ = ["ADMIN_FAMILIES", "ALL_KINDS", "ActivityQuery", "ActivityScope", "DEFAULT_LIMIT", "KINDS", "MAX_LIMIT",
           "MAX_OFFSET", "UnknownKind", "list_activity", "parse_kinds", "record_event"]
