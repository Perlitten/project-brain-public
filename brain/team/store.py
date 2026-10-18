from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import ResolvedPrincipal, generate_credential, hash_credential
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import ApiCredential, Principal, Repository
from brain.team.authorization import (
    Permission, RUN_CAPABILITIES, TeamConflict, TeamDenied, authorize, fresh, permissions,
)
from brain.team.models import AgentRun, ProjectRepository, TeamTaskBinding


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def task_dict(task: AgentTask, binding: TeamTaskBinding) -> dict:
    return {"id": str(task.id), "project_id": str(binding.project_id), "repository_id": binding.repository_id,
            "author_id": binding.author_id, "title": task.title, "goal": task.goal, "status": task.status,
            "version": task.version, "branch": binding.branch, "declared_base_sha": binding.base_sha}


async def list_tasks(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID) -> list[dict]:
    authority = await authorize(session, caller, project_id, Permission.READ)
    query = select(TeamTaskBinding).where(TeamTaskBinding.project_id == project_id).order_by(TeamTaskBinding.task_id).limit(100)
    if authority.run_id is not None:
        run = await fresh(session, AgentRun, AgentRun.id == authority.run_id)
        query = query.where(TeamTaskBinding.task_id == run.task_id)
    result = []
    for binding in (await session.execute(query)).scalars().all():
        try:
            await authorize(session, caller, project_id, Permission.READ, task_id=binding.task_id)
        except TeamDenied:
            continue
        task = await fresh(session, AgentTask, AgentTask.id == binding.task_id)
        result.append(task_dict(task, binding))
    return result


async def create_task(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                      payload: dict) -> dict:
    authority = await authorize(session, caller, project_id, Permission.CREATE_TASK)
    requested = permissions(payload["permissions"])
    if Permission.READ not in requested or not requested <= (authority.allowed & RUN_CAPABILITIES):
        raise TeamDenied("Task authority exceeds initiator or supported capabilities")
    repository = await fresh(session, ProjectRepository, ProjectRepository.repository_id == payload["repository_id"],
                             ProjectRepository.project_id == project_id)
    if repository is None:
        raise TeamDenied("Repository access denied")
    stored = dict(payload, permissions=sorted(requested))
    fingerprint = digest(stored)

    async def existing():
        binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.project_id == project_id,
                              TeamTaskBinding.author_id == authority.actor_id,
                              TeamTaskBinding.request_key == payload["idempotency_key"])
        if binding is None:
            return None
        if binding.request_digest != fingerprint:
            raise TeamConflict("Idempotency key has different task content")
        task = await fresh(session, AgentTask, AgentTask.id == binding.task_id)
        return task_dict(task, binding)

    found = await existing()
    if found is not None:
        return found
    try:
        async with session.begin_nested():
            repo = await fresh(session, Repository, Repository.id == repository.repository_id)
            task = AgentTask(id=uuid.uuid4(), title=payload["title"], goal=payload["goal"], repo_path=repo.path)
            session.add(task)
            await session.flush()
            binding = TeamTaskBinding(task_id=task.id, project_id=project_id, repository_id=repo.id,
                                      author_id=authority.actor_id, branch=payload["branch"], base_sha=payload["base_sha"],
                                      permissions=sorted(requested), request_key=payload["idempotency_key"],
                                      request_digest=fingerprint)
            session.add(binding)
            session.add(AgentTaskEvent(task_id=task.id, event_type="created", actor=f"principal:{authority.actor_id}",
                                       payload_json={"project_id": str(project_id), "repository_id": repo.id}))
            await session.flush()
            return task_dict(task, binding)
    except IntegrityError:
        found = await existing()
        if found is None:
            raise
        return found


def run_dict(run: AgentRun, raw_key: str | None = None) -> dict:
    return {"id": str(run.id), "task_id": str(run.task_id), "principal_id": run.principal_id,
            "initiator_id": run.initiator_id, "parent_id": str(run.parent_id) if run.parent_id else None,
            "permissions": run.permissions, "branch": run.branch, "declared_base_sha": run.base_sha,
            "head_sha": run.head_sha, "worktree_path": run.worktree_path,
            "execution_available": False, "credential": raw_key}


async def request_run(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                      task_id: uuid.UUID, payload: dict) -> dict:
    authority = await authorize(session, caller, project_id, Permission.REQUEST_RUN, task_id=task_id)
    requested = permissions(payload["permissions"])
    if not requested or not requested <= (authority.allowed & RUN_CAPABILITIES):
        raise TeamDenied("Delegation cannot expand authority")
    fingerprint = digest(dict(payload, permissions=sorted(requested)))

    async def existing():
        run = await fresh(session, AgentRun, AgentRun.task_id == task_id,
                          AgentRun.requested_by == authority.actor_id, AgentRun.request_key == payload["idempotency_key"])
        if run is not None and run.request_digest != fingerprint:
            raise TeamConflict("Idempotency key has different run content")
        return run

    found = await existing()
    if found is not None:
        return run_dict(found)
    from brain.team import coordination  # lazy: coordination builds on this module

    await coordination.require_dependencies_done(session, task_id)
    binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == task_id)
    raw_key = generate_credential()
    try:
        async with session.begin_nested():
            identity = Principal(name=f"run:{uuid.uuid4()}", kind="run")
            session.add(identity)
            await session.flush()
            credential = ApiCredential(principal_id=identity.id, key_hash=hash_credential(raw_key),
                                       scopes=[f"team:{p}" for p in sorted(requested)])
            session.add(credential)
            await session.flush()
            run = AgentRun(task_id=task_id, principal_id=identity.id, credential_id=credential.id,
                           initiator_id=authority.initiator_id, initiator_credential_id=authority.initiator_credential_id,
                           parent_id=authority.run_id, permissions=sorted(requested),
                           executor_capabilities=sorted(RUN_CAPABILITIES), branch=payload["branch"],
                           base_sha=binding.base_sha, requested_by=authority.actor_id,
                           request_key=payload["idempotency_key"], request_digest=fingerprint)
            session.add(run)
            await session.flush()
            await coordination.snapshot_instructions(session, run)
            return run_dict(run, raw_key)
    except IntegrityError:
        found = await existing()
        if found is None:
            raise
        return run_dict(found)
