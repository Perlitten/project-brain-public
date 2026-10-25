"""Exclusive, expiring path/resource leases with fencing tokens."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.team._util import _iso, _now
from brain.team.activity import record_event
from brain.team.authorization import Permission, TeamConflict, TeamDenied, administrator, authorize, fresh
from brain.team.models import AgentRun, ProjectRepository, TeamResourceLease
from brain.team.provisioning import TeamNotFound
from brain.team.coordination._common import (MAX_LEASE_SECONDS, MAX_REASON, MIN_LEASE_SECONDS, _uuid, normalize_resource, overlapping, paths_overlap)
from brain.team.coordination.overlaps import _active_plans, _live_leases, _record_overlaps
from brain.team.coordination.plans import _binding


def lease_dict(lease: TeamResourceLease, now: datetime | None = None) -> dict:
    now = now or _now()
    state = ("released" if lease.released_at is not None
             else "held" if lease.expires_at > now else "expired")
    return {"id": lease.id, "project_id": str(lease.project_id), "repository_id": lease.repository_id,
            "resource": lease.resource, "holder_id": lease.holder_id,
            "task_id": str(lease.task_id) if lease.task_id else None, "fencing_token": lease.fencing_token,
            "state": state, "acquired_at": _iso(lease.acquired_at), "expires_at": _iso(lease.expires_at),
            "released_at": _iso(lease.released_at), "released_reason": lease.released_reason}


def _ttl(value: int | None) -> timedelta:
    seconds = 300 if value is None else int(value)
    if not MIN_LEASE_SECONDS <= seconds <= MAX_LEASE_SECONDS:
        raise TeamConflict(f"Lease duration must be {MIN_LEASE_SECONDS}-{MAX_LEASE_SECONDS} seconds")
    return timedelta(seconds=seconds)


async def acquire_task_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                        payload: dict) -> dict:
    """Take an exclusive lease, or take over an expired or released one (the fencing token increases)."""
    task_id = _uuid(payload["task_id"], "task id") if payload.get("task_id") else None
    authority = await authorize(session, caller, project_id, Permission.REQUEST_RUN, task_id=task_id)
    if authority.run_id is not None:
        run = await fresh(session, AgentRun, AgentRun.id == authority.run_id)
        task_id = run.task_id
    # Exclusive lock on the repository link serialises acquisitions, so nested paths cannot both be leased.
    repository = await fresh(session, ProjectRepository, ProjectRepository.repository_id == payload["repository_id"],
                             ProjectRepository.project_id == project_id, exclusive=True)
    if repository is None:
        raise TeamDenied("Repository access denied")
    binding = None
    if task_id is not None:
        binding = await _binding(session, project_id, task_id)
        if binding.repository_id != repository.repository_id:
            raise TeamConflict("Task belongs to another repository")
    resource = normalize_resource(payload["resource"])
    ttl = _ttl(payload.get("ttl_seconds"))
    now = _now()
    for lease in await _live_leases(session, project_id, repository.repository_id):
        if lease.resource != resource and paths_overlap(lease.resource, resource) and lease.holder_id != authority.actor_id:
            raise TeamConflict(f"Overlapping resource is leased: {lease.resource} (lease {lease.id})")
    row = await fresh(session, TeamResourceLease, TeamResourceLease.repository_id == repository.repository_id,
                      TeamResourceLease.resource == resource, exclusive=True)
    if row is None:
        try:
            async with session.begin_nested():
                row = TeamResourceLease(project_id=project_id, repository_id=repository.repository_id, resource=resource,
                                        holder_id=authority.actor_id, task_id=task_id, fencing_token=1,
                                        acquired_at=now, expires_at=now + ttl)
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            raise TeamConflict("Resource is being leased concurrently; retry") from exc
    elif row.released_at is None and row.expires_at > now:
        if row.holder_id != authority.actor_id:
            raise TeamConflict(f"Resource is leased until {row.expires_at.isoformat()}")
        # The holder repeating its acquisition (a retry) gets its live lease back unchanged.
    else:
        changed = await session.execute(update(TeamResourceLease).where(
            TeamResourceLease.id == row.id, TeamResourceLease.fencing_token == row.fencing_token)
            .values(holder_id=authority.actor_id, task_id=task_id, project_id=project_id,
                    fencing_token=row.fencing_token + 1, acquired_at=now, expires_at=now + ttl,
                    released_at=None, released_reason=None).returning(TeamResourceLease.id))
        if changed.scalar_one_or_none() is None:
            raise TeamConflict("Resource is being leased concurrently; retry")
        row = await fresh(session, TeamResourceLease, TeamResourceLease.id == row.id)
    warnings = []
    for other_binding, other_plan, other_task in await _active_plans(session, project_id, repository.repository_id):
        if other_binding.task_id == task_id:
            continue
        pairs = overlapping(list(other_plan.scope_paths or []), [resource])
        if pairs:
            warnings.append({"kind": "lease", "repository_id": repository.repository_id,
                             "task_id": str(other_binding.task_id), "lease_id": row.id, "resource": resource,
                             "holder_id": row.holder_id, "expires_at": _iso(row.expires_at), "paths": pairs})
    if warnings:
        await _record_overlaps(session, project_id, warnings, authority.actor_id)
    return {"lease": lease_dict(row, now), "warnings": warnings}


async def _held_lease(session: AsyncSession, project_id: uuid.UUID, lease_id: int, holder_id: int, token: int,
                      values: dict) -> TeamResourceLease:
    now = _now()
    changed = await session.execute(update(TeamResourceLease).where(
        TeamResourceLease.id == lease_id, TeamResourceLease.project_id == project_id,
        TeamResourceLease.holder_id == holder_id, TeamResourceLease.fencing_token == token,
        TeamResourceLease.released_at.is_(None), TeamResourceLease.expires_at > now)
        .values(**values).returning(TeamResourceLease.id))
    if changed.scalar_one_or_none() is None:
        if await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id,
                       TeamResourceLease.project_id == project_id) is None:
            raise TeamNotFound("Lease not found")
        raise TeamConflict("Lease is not held with this fencing token (expired, released or taken over)")
    return await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id)


async def renew_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, lease_id: int,
                      payload: dict) -> dict:
    authority = await authorize(session, caller, project_id, Permission.REQUEST_RUN)
    ttl = _ttl(payload.get("ttl_seconds"))
    lease = await _held_lease(session, project_id, lease_id, authority.actor_id, payload["fencing_token"],
                              {"expires_at": _now() + ttl})
    return lease_dict(lease)


async def release_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, lease_id: int,
                        payload: dict) -> dict:
    authority = await authorize(session, caller, project_id, Permission.READ)
    lease = await _held_lease(session, project_id, lease_id, authority.actor_id, payload["fencing_token"],
                              {"released_at": _now(), "released_reason": "released"})
    return lease_dict(lease)


async def force_release_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, lease_id: int,
                              payload: dict) -> dict:
    """A project administrator releases someone's lease, with a reason recorded in the activity feed.

    The fencing token increases, so the previous holder can no longer renew or
    release it (and any write it fences on that token is refused)."""
    await administrator(session, caller, project_id)
    return await _force_release(session, project_id, lease_id, payload, actor_id=caller.id, actor_label=None)


async def operator_force_release_lease(session: AsyncSession, project_id: uuid.UUID, lease_id: int, payload: dict,
                                       *, actor_label: str) -> dict:
    """The same release for the server operator (``brain team coord force-release``), who has database access.

    There is no principal to authorize: like the other operator commands the
    CLI is trusted, and the activity row names ``actor_label`` instead."""
    return await _force_release(session, project_id, lease_id, payload, actor_id=None, actor_label=actor_label)


async def _force_release(session: AsyncSession, project_id: uuid.UUID, lease_id: int, payload: dict, *,
                         actor_id: int | None, actor_label: str | None) -> dict:
    reason = " ".join(str(payload.get("reason") or "").split())
    if not reason:
        raise TeamConflict("A reason is required to force-release a lease")
    lease = await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id,
                        TeamResourceLease.project_id == project_id, exclusive=True)
    if lease is None:
        raise TeamNotFound("Lease not found")
    if lease.released_at is not None:
        raise TeamConflict("Lease is already released")
    expected = payload.get("fencing_token")
    if expected is not None and int(expected) != lease.fencing_token:
        raise TeamConflict("Lease fencing token changed; reload the lease")
    now = _now()
    previous_holder, previous_token = lease.holder_id, lease.fencing_token
    changed = await session.execute(update(TeamResourceLease).where(
        TeamResourceLease.id == lease.id, TeamResourceLease.fencing_token == previous_token,
        TeamResourceLease.released_at.is_(None))
        .values(released_at=now, released_reason=f"force released: {reason}"[:64],
                fencing_token=previous_token + 1).returning(TeamResourceLease.id))
    if changed.scalar_one_or_none() is None:
        raise TeamConflict("Lease changed concurrently; retry")
    lease = await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id)
    by = "the server operator" if actor_id is None else "an administrator"
    await record_event(session, kind="coordination.lease_force_released",
                       summary=f"Lease on {lease.resource} force-released by {by}",
                       audience="repository", actor_principal_id=actor_id, actor_label=actor_label,
                       subject_principal_id=previous_holder, project_id=project_id,
                       repository_id=lease.repository_id,
                       details={"lease_id": lease.id, "resource": lease.resource, "holder_id": previous_holder,
                                "task_id": str(lease.task_id) if lease.task_id else None,
                                "previous_fencing_token": previous_token, "fencing_token": lease.fencing_token,
                                "reason": reason[:MAX_REASON]})
    return lease_dict(lease, now)


async def leases_view(session: AsyncSession, project_id: uuid.UUID, repository_id: int | None = None,
                      include_inactive: bool = False) -> list[dict]:
    query = select(TeamResourceLease).where(TeamResourceLease.project_id == project_id)
    if repository_id is not None:
        query = query.where(TeamResourceLease.repository_id == repository_id)
    if not include_inactive:
        query = query.where(TeamResourceLease.released_at.is_(None), TeamResourceLease.expires_at > _now())
    now = _now()
    return [lease_dict(row, now) for row in (await session.execute(query.order_by(TeamResourceLease.id).limit(500)))
            .scalars().all()]


async def list_leases(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                      repository_id: int | None = None, include_inactive: bool = False) -> list[dict]:
    await authorize(session, caller, project_id, Permission.READ)
    return await leases_view(session, project_id, repository_id, include_inactive)


# Compat alias: historical name; new code should use acquire_task_lease.
acquire_lease = acquire_task_lease
