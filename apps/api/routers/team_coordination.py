"""Team coordination routes (M3): subtasks and dependencies, overlaps, leases, integration queue.

Logic lives in ``brain/team/coordination.py``; every route authorizes through
the central ``authorize()`` there, against live membership and project policy.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import require_principal
from apps.api.routers.team import team_session
from brain.auth.principals import ResolvedPrincipal
from brain.team import coordination

router = APIRouter(prefix="/team/projects", tags=["experimental-team"])

# Prefixed with the parent or subtask id inside the 128-character idempotency column.
IdempotencyKey = Field(min_length=8, max_length=64)


class SubtaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    goal: str = Field(min_length=1, max_length=10000)
    branch: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[^\s]+$")
    permissions: list[str] | None = Field(default=None, max_length=16)
    scope_paths: list[str] = Field(default_factory=list, max_length=coordination.MAX_SCOPE_PATHS)
    depends_on: list[str] = Field(default_factory=list, max_length=coordination.MAX_DEPENDENCIES)
    idempotency_key: str = IdempotencyKey


class PlanUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    scope_paths: list[str] | None = Field(default=None, max_length=coordination.MAX_SCOPE_PATHS)
    depends_on: list[str] | None = Field(default=None, max_length=coordination.MAX_DEPENDENCIES)
    expected_version: int = Field(ge=0)


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    permissions: list[str] = Field(default_factory=lambda: ["read"], max_length=16)
    idempotency_key: str = IdempotencyKey


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    repository_id: int = Field(gt=0)
    resource: str = Field(min_length=1, max_length=512)
    task_id: uuid.UUID | None = None
    ttl_seconds: int = Field(default=300, ge=coordination.MIN_LEASE_SECONDS, le=coordination.MAX_LEASE_SECONDS)


class LeaseRenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fencing_token: int = Field(gt=0)
    ttl_seconds: int = Field(default=300, ge=coordination.MIN_LEASE_SECONDS, le=coordination.MAX_LEASE_SECONDS)


class LeaseReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fencing_token: int = Field(gt=0)


class EnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID


class IntegrateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    current_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_version: int = Field(gt=0)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(gt=0)


@router.post("/{project_id}/tasks/{task_id}/subtasks")
async def create_subtask(project_id: uuid.UUID, task_id: uuid.UUID, body: SubtaskRequest,
                         caller: ResolvedPrincipal = Depends(require_principal),
                         session=Depends(team_session, scope="function")):
    return await coordination.create_subtask(session, caller, project_id, task_id, body.model_dump())


@router.get("/{project_id}/tasks/{task_id}/plan")
async def get_plan(project_id: uuid.UUID, task_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                   session=Depends(team_session, scope="function")):
    return await coordination.get_plan(session, caller, project_id, task_id)


@router.put("/{project_id}/tasks/{task_id}/plan")
async def update_plan(project_id: uuid.UUID, task_id: uuid.UUID, body: PlanUpdateRequest,
                      caller: ResolvedPrincipal = Depends(require_principal),
                      session=Depends(team_session, scope="function")):
    return await coordination.update_plan(session, caller, project_id, task_id, body.model_dump())


@router.post("/{project_id}/tasks/{task_id}/dispatch")
async def dispatch(project_id: uuid.UUID, task_id: uuid.UUID, body: DispatchRequest,
                   caller: ResolvedPrincipal = Depends(require_principal),
                   session=Depends(team_session, scope="function")):
    return await coordination.dispatch(session, caller, project_id, task_id, body.model_dump())


@router.get("/{project_id}/tasks/{task_id}/runs/{run_id}/instructions")
async def run_instructions(project_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID,
                           caller: ResolvedPrincipal = Depends(require_principal),
                           session=Depends(team_session, scope="function")):
    return await coordination.get_run_instructions(session, caller, project_id, task_id, run_id)


@router.get("/{project_id}/overlaps")
async def list_overlaps(project_id: uuid.UUID, repository_id: int | None = Query(default=None, gt=0),
                        caller: ResolvedPrincipal = Depends(require_principal),
                        session=Depends(team_session, scope="function")):
    return {"overlaps": await coordination.list_overlaps(session, caller, project_id, repository_id)}


@router.get("/{project_id}/leases")
async def list_leases(project_id: uuid.UUID, repository_id: int | None = Query(default=None, gt=0),
                      include_inactive: bool = False, caller: ResolvedPrincipal = Depends(require_principal),
                      session=Depends(team_session, scope="function")):
    return {"leases": await coordination.list_leases(session, caller, project_id, repository_id, include_inactive)}


@router.post("/{project_id}/leases")
async def acquire_lease(project_id: uuid.UUID, body: LeaseRequest, caller: ResolvedPrincipal = Depends(require_principal),
                        session=Depends(team_session, scope="function")):
    return await coordination.acquire_lease(session, caller, project_id, body.model_dump())


@router.post("/{project_id}/leases/{lease_id}/renew")
async def renew_lease(project_id: uuid.UUID, lease_id: int, body: LeaseRenewRequest,
                      caller: ResolvedPrincipal = Depends(require_principal),
                      session=Depends(team_session, scope="function")):
    return await coordination.renew_lease(session, caller, project_id, lease_id, body.model_dump())


@router.post("/{project_id}/leases/{lease_id}/release")
async def release_lease(project_id: uuid.UUID, lease_id: int, body: LeaseReleaseRequest,
                        caller: ResolvedPrincipal = Depends(require_principal),
                        session=Depends(team_session, scope="function")):
    return await coordination.release_lease(session, caller, project_id, lease_id, body.model_dump())


@router.get("/{project_id}/integration")
async def list_queue(project_id: uuid.UUID, repository_id: int | None = Query(default=None, gt=0),
                     include_done: bool = False, caller: ResolvedPrincipal = Depends(require_principal),
                     session=Depends(team_session, scope="function")):
    return {"entries": await coordination.list_queue(session, caller, project_id, repository_id, include_done)}


@router.post("/{project_id}/integration")
async def enqueue(project_id: uuid.UUID, body: EnqueueRequest, caller: ResolvedPrincipal = Depends(require_principal),
                  session=Depends(team_session, scope="function")):
    return await coordination.enqueue(session, caller, project_id, body.model_dump())


@router.post("/{project_id}/integration/{entry_id}/integrate")
async def integrate(project_id: uuid.UUID, entry_id: int, body: IntegrateRequest,
                    caller: ResolvedPrincipal = Depends(require_principal),
                    session=Depends(team_session, scope="function")):
    return await coordination.integrate(session, caller, project_id, entry_id, body.model_dump())


@router.post("/{project_id}/integration/{entry_id}/reject")
async def reject(project_id: uuid.UUID, entry_id: int, body: RejectRequest,
                 caller: ResolvedPrincipal = Depends(require_principal),
                 session=Depends(team_session, scope="function")):
    return await coordination.reject(session, caller, project_id, entry_id, body.model_dump())
