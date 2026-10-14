import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import require_principal
from brain.auth.principals import ResolvedPrincipal
from brain.database.session import async_session_factory
from brain.team import store
from brain.team.authorization import TeamConflict, TeamDenied, require_team_mode

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


async def team_session():
    try:
        require_team_mode()
        async with async_session_factory() as session, session.begin():
            yield session
    except TeamDenied as exc:
        raise HTTPException(403, "Team authority denied") from exc
    except TeamConflict as exc:
        raise HTTPException(409, str(exc)) from exc


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
