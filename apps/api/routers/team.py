from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.api.auth import require_principal
from brain.auth.principals import ResolvedPrincipal
from brain.database.session import async_session_factory
from brain.team import provisioning, store
from brain.team.authorization import TeamConflict, TeamDenied, administrator, require_team_storage, team_administrator
from brain.team.provisioning import TeamNotFound

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
async def list_projects(caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session)):
    member = await team_administrator(session, caller)
    return await provisioning.list_projects(session, member.team_id)


@router.post("/")
async def create_project(body: ProjectCreateRequest, caller: ResolvedPrincipal = Depends(require_principal),
                         session=Depends(team_session)):
    member = await team_administrator(session, caller)
    project = await provisioning.create_project(session, name=body.name, allowed=body.permissions,
                                                team_id=member.team_id)
    return provisioning.project_dict(project)


@router.get("/members")
async def list_team_members(caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session)):
    member = await team_administrator(session, caller)
    return await provisioning.list_members(session, member.team_id)


@router.post("/members")
async def add_team_member(body: TeamMemberRequest, caller: ResolvedPrincipal = Depends(require_principal),
                          session=Depends(team_session)):
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
                                           team_id=admin.team_id)
    return provisioning.member_dict(member, principal)


@router.delete("/members/{principal_id}")
async def revoke_team_member(principal_id: int, caller: ResolvedPrincipal = Depends(require_principal),
                             session=Depends(team_session)):
    admin = await team_administrator(session, caller)
    if principal_id == admin.principal_id:
        raise TeamConflict("Administrators cannot revoke their own membership")
    principal = await provisioning.resolve_principal(session, principal_id)
    member = await provisioning.revoke_member(session, principal=principal)
    return provisioning.member_dict(member, principal)


# Project-scoped administration (``administrator``: team admin, locked project).

@router.get("/{project_id}/members")
async def list_project_members(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                               session=Depends(team_session)):
    await administrator(session, caller, project_id)
    return await provisioning.list_project_members(session, project_id)


@router.put("/{project_id}/members/{principal_id}")
async def assign_project_member(project_id: uuid.UUID, principal_id: int, body: ProjectRoleRequest,
                                caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session)):
    await administrator(session, caller, project_id)
    membership = await provisioning.assign_project_member(session, project_id=project_id, principal_id=principal_id,
                                                          role=body.role)
    return provisioning.project_member_dict(membership)


@router.delete("/{project_id}/members/{principal_id}")
async def revoke_project_member(project_id: uuid.UUID, principal_id: int,
                                caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session)):
    await administrator(session, caller, project_id)
    membership = await provisioning.revoke_project_member(session, project_id=project_id, principal_id=principal_id)
    return provisioning.project_member_dict(membership)


@router.get("/{project_id}/repositories")
async def list_project_repositories(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                                    session=Depends(team_session)):
    await administrator(session, caller, project_id)
    return await provisioning.list_project_repositories(session, project_id)


@router.post("/{project_id}/repositories")
async def attach_project_repository(project_id: uuid.UUID, body: RepositoryAttachRequest,
                                    caller: ResolvedPrincipal = Depends(require_principal),
                                    session=Depends(team_session)):
    await administrator(session, caller, project_id)
    repository = await provisioning.resolve_repository(session, body.repository_id)
    link = await provisioning.attach_repository(session, project_id=project_id, repository=repository)
    return provisioning.repository_dict(link, repository)


# Task and run surface (live membership / project policy).

@router.get("/{project_id}/tasks")
async def list_tasks(project_id: uuid.UUID, caller: ResolvedPrincipal = Depends(require_principal),
                     session=Depends(team_session)):
    return await store.list_tasks(session, caller, project_id)


@router.post("/{project_id}/tasks")
async def create_task(project_id: uuid.UUID, body: TaskRequest, caller: ResolvedPrincipal = Depends(require_principal),
                      session=Depends(team_session)):
    return await store.create_task(session, caller, project_id, body.model_dump())


@router.post("/{project_id}/tasks/{task_id}/runs")
async def request_run(project_id: uuid.UUID, task_id: uuid.UUID, body: RunRequest,
                      caller: ResolvedPrincipal = Depends(require_principal), session=Depends(team_session)):
    return await store.request_run(session, caller, project_id, task_id, body.model_dump())
