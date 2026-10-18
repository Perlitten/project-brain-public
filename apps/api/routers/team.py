from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.api.auth import require_principal, require_scope
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.database.models import Repository
from brain.database.session import async_session_factory, redis_client
from brain.team import execution, provisioning, store
from brain.team.authorization import (Permission, TeamConflict, TeamDenied, administrator, fresh, require_team_storage,
                                      team_administrator)
from brain.team.models import TeamTaskBinding
from brain.team.provisioning import TeamNotFound
from brain.workers.initiator import JobInitiator
from brain.workers.queue import JobQueue, queue_for_job

router = APIRouter(prefix="/team/projects", tags=["experimental-team"])


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    repository_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=500)
    goal: str = Field(min_length=1, max_length=10000)
    branch: str = Field(min_length=1, max_length=255, pattern=r"^[^\s]+$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    permissions: list[str] = Field(default_factory=lambda: ["read", "run.request"], max_length=16)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    branch: str = Field(min_length=1, max_length=255, pattern=r"^[^\s]+$")
    permissions: list[str] = Field(default_factory=lambda: ["read"], max_length=16)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=128)
    permissions: list[str] | None = Field(default=None, max_length=16)


class TeamMemberRequest(BaseModel):
    """Link an existing human principal as a verified team member.

    Principals and credentials are minted offline (``brain team member add
    --mint-key``); the API only records how the operator verified them."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    principal_id: int | None = Field(default=None, gt=0)
    principal_name: str | None = Field(default=None, min_length=1, max_length=128)
    issuer: str = Field(default=provisioning.CREDENTIAL_ISSUER, min_length=1, max_length=255)
    subject: str | None = Field(default=None, min_length=1, max_length=255)
    is_admin: bool | None = None

    @model_validator(mode="after")
    def _one_principal(self):
        if (self.principal_id is None) == (self.principal_name is None):
            raise ValueError("Provide exactly one of principal_id or principal_name")
        return self


class ProjectRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: Literal["reader", "contributor", "reviewer"]


class RepositoryAttachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: int = Field(gt=0)


class ExecutionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=64)
    args: list[str] = Field(default_factory=list, max_length=64)
    secrets: list[str] = Field(default_factory=list, max_length=16)


class ExecutionBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wall_seconds: int | None = Field(default=None, gt=0)
    steps: int | None = Field(default=None, gt=0)
    step_timeout_seconds: int | None = Field(default=None, gt=0)
    output_bytes: int | None = Field(default=None, gt=0)


class ExecutionLLM(BaseModel):
    """The LLM provider a step may send repository content to; ``max_*`` are reserved in full against the budget."""
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    max_tokens: int = Field(gt=0, le=2**31 - 1)
    max_cost_cents: int | None = Field(default=None, ge=0, le=2**31 - 1)


class LlmConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(default="*", min_length=1, max_length=128)


class LlmBudgetRequest(BaseModel):
    """Limits are optional (unlimited when omitted); ``period`` is ``month`` (UTC calendar month) or ``total``."""
    model_config = ConfigDict(extra="forbid")
    period: Literal["month", "total"] = "month"
    max_tokens: int | None = Field(default=None, ge=0, le=2**31 - 1)
    max_cost_cents: int | None = Field(default=None, ge=0, le=2**31 - 1)
    per_member_max_tokens: int | None = Field(default=None, ge=0, le=2**31 - 1)
    per_member_max_cost_cents: int | None = Field(default=None, ge=0, le=2**31 - 1)


class ExecutionRequest(BaseModel):
    """Capabilities requested for one run; each must lie within the operator and parent-run ceilings.

    ``secrets`` and ``secret_refs`` are reference names configured by the operator, never values."""
    model_config = ConfigDict(extra="forbid")
    steps: list[ExecutionStep] = Field(min_length=1, max_length=100)
    tools: list[str] | None = Field(default=None, max_length=64)
    secret_refs: list[str] | None = Field(default=None, max_length=32)
    egress: list[str] = Field(default_factory=list, max_length=32)
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    # Operator-defined execution environment (image + tools); required by the container backend.
    environment: str | None = Field(default=None, max_length=64)
    # Declared LLM use (provider consent and budget gates); omitted when the steps reach no LLM provider.
    llm: ExecutionLLM | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)


async def team_session():
    try:
        require_team_storage()
        async with async_session_factory() as session, session.begin():
            yield session
    except TeamDenied as exc:
        raise HTTPException(403, "Team authority denied") from exc
    except TeamNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except TeamConflict as exc:
        raise HTTPException(409, str(exc)) from exc


# Team-level administration. Declared before the ``/{project_id}`` routes so
# the literal ``members`` segment is never parsed as a project identifier.

@router.get("/")
async def list_projects(caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    member = await team_administrator(session, caller)
    return await provisioning.list_projects(session, member.team_id)


@router.post("/")
async def create_project(body: ProjectCreateRequest, caller: ResolvedPrincipal = Depends(require_principal),
                         session=Depends(team_session, scope="function")):
    member = await team_administrator(session, caller)
    project = await provisioning.create_project(session, name=body.name, allowed=body.permissions,
                                                team_id=member.team_id)
    return provisioning.project_dict(project)


@router.get("/members")
async def list_team_members(caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    member = await team_administrator(session, caller)
    return await provisioning.list_members(session, member.team_id)


@router.post("/members")
async def add_team_member(body: TeamMemberRequest, caller: ResolvedPrincipal = Depends(require_principal),
                          session=Depends(team_session, scope="function")):
    admin = await team_administrator(session, caller)
    if body.principal_id is not None:
        principal = await provisioning.resolve_principal(session, body.principal_id)
    else:
        assert body.principal_name is not None
        principal = await provisioning.resolve_principal_name(session, body.principal_name)
    if principal.id == admin.principal_id and body.is_admin is False:
        raise TeamConflict("Administrators cannot demote their own membership")
    member = await provisioning.add_member(session, principal=principal, issuer=body.issuer,
                                           subject=body.subject or principal.name, is_admin=body.is_admin,
                                           team_id=admin.team_id, actor_id=caller.author_id)
    return provisioning.member_dict(member, principal)


@router.delete("/members/{principal_id}")
async def revoke_team_member(principal_id: int, caller: ResolvedPrincipal = Depends(require_principal),
                             session=Depends(team_session, scope="function")):
    admin = await team_administrator(session, caller)
    if principal_id == admin.principal_id:
        raise TeamConflict("Administrators cannot revoke their own membership")
    principal = await provisioning.resolve_principal(session, principal_id)
    member = await provisioning.revoke_member(session, principal=principal, actor_id=caller.author_id)
    return provisioning.member_dict(member, principal)


# Project-scoped administration (``administrator``: team admin, locked project).

@router.get("/{project_id}/members")
async def list_project_members(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                               session=Depends(team_session, scope="function")):
    await administrator(session, caller, project_id)
    return await provisioning.list_project_members(session, project_id)


@router.put("/{project_id}/members/{principal_id}")
async def assign_project_member(project_id: uuid.UUID, principal_id: int, body: ProjectRoleRequest,
                                caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    await administrator(session, caller, project_id)
    membership = await provisioning.assign_project_member(session, project_id=project_id, principal_id=principal_id,
                                                          role=body.role, actor_id=caller.author_id)
    return provisioning.project_member_dict(membership)


@router.delete("/{project_id}/members/{principal_id}")
async def revoke_project_member(project_id: uuid.UUID, principal_id: int,
                                caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    await administrator(session, caller, project_id)
    membership = await provisioning.revoke_project_member(session, project_id=project_id, principal_id=principal_id,
                                                          actor_id=caller.author_id)
    return provisioning.project_member_dict(membership)


@router.get("/{project_id}/repositories")
async def list_project_repositories(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                                    session=Depends(team_session, scope="function")):
    await administrator(session, caller, project_id)
    return await provisioning.list_project_repositories(session, project_id)


@router.post("/{project_id}/repositories")
async def attach_project_repository(project_id: uuid.UUID, body: RepositoryAttachRequest,
                                    caller: ResolvedPrincipal = Depends(require_principal),
                                    session=Depends(team_session, scope="function")):
    await administrator(session, caller, project_id)
    repository = await provisioning.resolve_repository(session, body.repository_id)
    link = await provisioning.attach_repository(session, project_id=project_id, repository=repository)
    return provisioning.repository_dict(link, repository)


# Task and run surface (live membership / project policy).

@router.get("/{project_id}/tasks")
async def list_tasks(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                     session=Depends(team_session, scope="function")):
    return await store.list_tasks(session, caller, project_id)


@router.post("/{project_id}/tasks")
async def create_task(project_id: uuid.UUID, body: TaskRequest, caller: ResolvedPrincipal = Depends(require_principal),
                      session=Depends(team_session, scope="function")):
    return await store.create_task(session, caller, project_id, body.model_dump())


@router.post("/{project_id}/tasks/{task_id}/runs")
async def request_run(project_id: uuid.UUID, task_id: uuid.UUID, body: RunRequest,
                      caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    return await store.request_run(session, caller, project_id, task_id, body.model_dump())


# Bounded execution (M2): off unless the operator enables it and an administrator grants it.
# Scope gates resolve before body/parameter validation; the handlers still check live membership and,
# for mutations, a live project administrator holding team:admin.
EXECUTION_READ = [Depends(require_scope("team:read"))]
EXECUTION_ADMIN_WRITE = [Depends(require_scope("setup:write"))]

@router.get("/{project_id}/execution-grants")
async def list_execution_grants(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                                session=Depends(team_session, scope="function")):
    return await execution.list_execution_grants(session, caller, project_id)


@router.put("/{project_id}/members/{principal_id}/execution")
async def grant_execution(project_id: uuid.UUID, principal_id: int,
                          caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    return await execution.grant_execution(session, caller, project_id, principal_id)


@router.delete("/{project_id}/members/{principal_id}/execution")
async def revoke_execution_grant(project_id: uuid.UUID, principal_id: int,
                                 caller: ResolvedPrincipal = Depends(require_principal),
                                 session=Depends(team_session, scope="function")):
    return await execution.revoke_execution(session, caller, project_id, principal_id)


@router.get("/{project_id}/execution/llm-consents", dependencies=EXECUTION_READ)
async def list_llm_consents(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                            session=Depends(team_session, scope="function")):
    return await execution.list_llm_consents(session, caller, project_id)


@router.put("/{project_id}/execution/llm-consents", dependencies=EXECUTION_ADMIN_WRITE)
async def consent_llm(project_id: uuid.UUID, body: LlmConsentRequest,
                      caller: ResolvedPrincipal = Depends(require_principal),
                      session=Depends(team_session, scope="function")):
    """A team administrator consents to executions sending repository content to this provider and model."""
    return await execution.consent_llm(session, caller, project_id, body.provider, body.model)


@router.delete("/{project_id}/execution/llm-consents/{provider}", dependencies=EXECUTION_ADMIN_WRITE)
async def revoke_llm_consent(project_id: uuid.UUID, provider: str, model: str = "*",
                             caller: ResolvedPrincipal = Depends(require_principal),
                             session=Depends(team_session, scope="function")):
    return await execution.revoke_llm_consent(session, caller, project_id, provider, model)


@router.get("/{project_id}/execution/llm-budget", dependencies=EXECUTION_READ)
async def get_llm_budget(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                         session=Depends(team_session, scope="function")):
    return await execution.llm_budget(session, caller, project_id)


@router.put("/{project_id}/execution/llm-budget", dependencies=EXECUTION_ADMIN_WRITE)
async def set_llm_budget(project_id: uuid.UUID, body: LlmBudgetRequest,
                         caller: ResolvedPrincipal = Depends(require_principal),
                         session=Depends(team_session, scope="function")):
    return await execution.set_llm_budget(session, caller, project_id, body.model_dump())


@router.delete("/{project_id}/execution/llm-budget", dependencies=EXECUTION_ADMIN_WRITE)
async def clear_llm_budget(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                           session=Depends(team_session, scope="function")):
    return await execution.clear_llm_budget(session, caller, project_id)


def _execution_queue() -> JobQueue:
    return queue_for_job(redis_client, settings.WORKER_REDIS_PREFIX, "team_run_execution",
                         pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED)


@router.post("/{project_id}/tasks/{task_id}/runs/{run_id}/execution")
async def request_execution(project_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID, body: ExecutionRequest,
                            caller: ResolvedPrincipal = Depends(require_principal)):
    """Record the execution, commit, then queue it; a replay of a queued request queues nothing new."""
    try:
        require_team_storage()
        async with async_session_factory() as session, session.begin():
            state = await execution.request_execution(session, caller, project_id, task_id, run_id,
                                                      body.model_dump())
            binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == task_id)
            repository = await fresh(session, Repository, Repository.id == binding.repository_id) if binding else None
    except execution.ExecutionDenied as exc:
        # Execution gates name themselves; base authorization denials stay generic.
        raise HTTPException(403, str(exc)) from exc
    except TeamDenied as exc:
        raise HTTPException(403, "Team authority denied") from exc
    except (TeamNotFound, execution.ExecutionNotFound) as exc:
        raise HTTPException(404, str(exc)) from exc
    except TeamConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if state["state"] == "requested" and repository is not None:
        state["job_id"] = await _execution_queue().enqueue(
            "team_run_execution", {"run_id": str(run_id), "repo_path": repository.path},
            idempotency_key=f"team-run-execution:{run_id}",
            timeout_seconds=int(state["budget"]["wall_seconds"]) + 120,
            initiator=JobInitiator.for_principal(caller, Permission.REQUEST_RUN))
    return state


@router.get("/{project_id}/tasks/{task_id}/runs/{run_id}/execution")
async def get_execution(project_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID,
                        caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    try:
        return await execution.get_execution(session, caller, project_id, task_id, run_id)
    except execution.ExecutionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{project_id}/tasks/{task_id}/runs/{run_id}/execution/cancel")
async def cancel_execution(project_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID,
                           caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session, scope="function")):
    try:
        return await execution.cancel_execution(session, caller, project_id, task_id, run_id)
    except execution.ExecutionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{project_id}/tasks/{task_id}/runs/{run_id}/execution/artifacts", dependencies=EXECUTION_READ)
async def list_execution_artifacts(project_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID,
                                   caller: ResolvedPrincipal = Depends(require_principal),
                                   session=Depends(team_session, scope="function")):
    try:
        return await execution.list_artifacts(session, caller, project_id, task_id, run_id)
    except execution.ExecutionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{project_id}/tasks/{task_id}/runs/{run_id}/execution/artifacts/{artifact_id}", dependencies=EXECUTION_READ)
async def download_execution_artifact(project_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID,
                                      artifact_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                                      session=Depends(team_session, scope="function")):
    """The stored (capped, redacted) bytes, always as a download so a browser never renders them."""
    try:
        meta, content = await execution.get_artifact(session, caller, project_id, task_id, run_id, artifact_id)
    except execution.ExecutionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content, media_type=meta["content_type"], headers={
        "Content-Disposition": f'attachment; filename="{meta["name"]}"', "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store", "X-Artifact-SHA256": meta["sha256"],
        "X-Artifact-Truncated": "true" if meta["truncated"] else "false"})
