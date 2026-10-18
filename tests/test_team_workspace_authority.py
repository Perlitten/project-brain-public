"""One repository authority: workspace-registry trust x team membership x solo/team state.

``authorize_repository`` is the single place where the registry (``brain/workspace``:
is this repository trusted?) and the team model (``brain/team``: may this person use
it?) meet. Membership decides who; registry trust caps what anyone may do, team
administrators included. Solo state stays a team of one and ignores the registry.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, select

from apps.api import auth, team_access
from apps.api.routers import workspace
from brain.database.models import Repository
from brain.team.authorization import FULL_AUTHORITY, Permission, TeamDenied, authorize_repository
from brain.team.models import ProjectRepository
from brain.workspace.models import RepositoryRecord, TrustLevel, _generate_repository_id
from brain.workspace.registry_store import RegistryStore
from tests.test_team_foundations import caller
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import live_team_state, membership_driven, revoke_everyone

team_db = team_fixture
pytestmark = pytest.mark.team_state

ROOT_A, ROOT_B, ROOT_C = "/synthetic/A", "/synthetic/B", "/synthetic/C"
UNATTACHED = 3  # Repository C: indexed, attached to no project.


def _record(root: str, trust: str) -> RepositoryRecord:
    return RepositoryRecord(
        repository_id=_generate_repository_id(root), display_name=root, canonical_root=root,
        normalized_root_identity=root.lower(), repository_type="fixture", default_branch="main",
        current_revision="", trust_level=trust, organization="", owner="",
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """The process registry (``.brain/workspace`` under the working directory), isolated per test."""
    monkeypatch.chdir(tmp_path)
    return RegistryStore(Path(".brain/workspace"))


def put(store: RegistryStore, root: str, trust: str) -> None:
    """Register ``root`` at ``trust``; ``"withdrawn"`` registers it trusted, then disables it."""
    record = _record(root, TrustLevel.TRUSTED_INTERNAL.value if trust == "withdrawn" else trust)
    store.register(record, actor="test", source="test")
    if trust == "withdrawn":
        store.disable(record.repository_id, actor="test", source="test", reason="synthetic")


@pytest_asyncio.fixture
async def world(team_db, registry):
    factory, a, b = team_db
    async with factory() as session, session.begin():
        session.add(Repository(id=UNATTACHED, name="C", path=ROOT_C))
    return factory, registry


async def outcome(factory, principal: int, repository_id: int | None, permission: Permission, **kwargs):
    async with factory() as session:
        try:
            authority = await authorize_repository(session, caller(principal), repository_id, permission, **kwargs)
        except TeamDenied as exc:
            return False, str(exc)
        return True, authority


# Membership alone (no registry restriction): principal, repository, permission -> allowed.
# 1 reader on A, 2 contributor on A, 4 team administrator and contributor on B only.
MEMBERSHIP = [
    (1, 1, Permission.READ, True),             # member, read
    (1, 1, Permission.REQUEST_RUN, False),     # member, role lacks the permission
    (2, 1, Permission.REQUEST_RUN, True),      # member, role grants it
    (1, 2, Permission.READ, False),            # non-member
    (4, 1, Permission.READ, False),            # administrator, not a member of the project
    (4, 2, Permission.REQUEST_RUN, True),      # administrator and member
    (4, UNATTACHED, Permission.READ, True),    # administrator, unattached repository
    (4, UNATTACHED, Permission.REQUEST_RUN, True),
    (2, UNATTACHED, Permission.READ, False),   # non-administrator, unattached repository
]
ROOTS = {1: ROOT_A, 2: ROOT_B, UNATTACHED: ROOT_C}
# Registry trust -> the permissions it leaves open.
TRUST = {
    None: FULL_AUTHORITY,                                    # not registered: no restriction
    TrustLevel.TRUSTED_INTERNAL.value: FULL_AUTHORITY,
    TrustLevel.FIXTURE.value: FULL_AUTHORITY,
    TrustLevel.UNTRUSTED_EXTERNAL.value: frozenset({Permission.READ}),
    TrustLevel.DISABLED.value: frozenset(),
    "withdrawn": frozenset(),                                # registered, then disabled
}


@pytest.mark.asyncio
@pytest.mark.parametrize("trust", list(TRUST))
@pytest.mark.parametrize("principal, repository_id, permission, member_allowed", MEMBERSHIP)
async def test_team_agreement_matrix(world, trust, principal, repository_id, permission, member_allowed):
    factory, registry = world
    if trust is not None:
        put(registry, ROOTS[repository_id], trust)
    allowed, result = await outcome(factory, principal, repository_id, permission)
    assert allowed is (member_allowed and permission in TRUST[trust])
    if allowed:
        assert result.allowed <= TRUST[trust]
    elif not member_allowed:
        # Membership is decided first: a caller without access learns nothing about registry trust.
        assert "registry" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("trust", list(TRUST))
@pytest.mark.parametrize("principal, repository_id, permission, _member_allowed", MEMBERSHIP)
async def test_solo_ignores_the_registry(world, monkeypatch, trust, principal, repository_id, permission,
                                         _member_allowed):
    factory, registry = world
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    if trust is not None:
        put(registry, ROOTS[repository_id], trust)
    allowed, authority = await outcome(factory, principal, repository_id, permission)
    assert allowed and authority.team_state == "solo" and authority.allowed == FULL_AUTHORITY


@pytest.mark.asyncio
async def test_disabled_repository_is_denied_to_its_project_and_to_administrators(world):
    factory, registry = world
    put(registry, ROOT_B, "withdrawn")
    allowed, reason = await outcome(factory, 4, 2, Permission.READ)
    assert not allowed and "workspace registry" in reason
    # The project still lists the repository; only registry trust denies it.
    async with factory() as session:
        assert (await session.execute(select(ProjectRepository).where(ProjectRepository.repository_id == 2))
                ).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_registry_path_that_is_not_indexed_carries_its_trust(world):
    factory, registry = world
    put(registry, "/synthetic/unindexed", "withdrawn")
    allowed, reason = await outcome(factory, 4, None, Permission.READ, repo_path="/synthetic/unindexed")
    assert not allowed and "workspace registry" in reason
    allowed, _ = await outcome(factory, 4, None, Permission.READ, repo_path="/synthetic/other")
    assert allowed


@pytest.mark.asyncio
async def test_unreadable_registry_fails_closed_in_team_state_only(world, monkeypatch):
    factory, registry = world
    put(registry, ROOT_A, TrustLevel.TRUSTED_INTERNAL.value)
    registry_file = Path(".brain/workspace/repository_registry.json")
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    data["schema_version"] = "tampered"
    registry_file.write_text(json.dumps(data), encoding="utf-8")
    allowed, reason = await outcome(factory, 2, 1, Permission.READ)
    assert not allowed and "unreadable" in reason

    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    allowed, _ = await outcome(factory, 2, 1, Permission.READ)
    assert allowed


@pytest.fixture
def access(world, monkeypatch):
    factory, registry = world
    for module in (auth, team_access):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    return factory, registry


@pytest.mark.asyncio
async def test_member_listings_drop_repositories_the_registry_does_not_trust(access):
    _, registry = access
    assert await team_access.readable_repository_ids(caller(1), [1, 2]) == {1}
    put(registry, ROOT_A, TrustLevel.UNTRUSTED_EXTERNAL.value)
    assert await team_access.readable_repository_ids(caller(1), [1, 2]) == {1}  # read stays open
    put(registry, ROOT_B, "withdrawn")
    assert await team_access.readable_repository_ids(caller(4), [1, 2]) is None  # administrators list all


@pytest.mark.asyncio
async def test_member_listing_hides_a_disabled_repository(access):
    _, registry = access
    put(registry, ROOT_A, "withdrawn")
    assert await team_access.readable_repository_ids(caller(1), [1, 2]) == set()


@pytest.mark.asyncio
async def test_personal_routes_answer_403_on_a_disabled_repository(access):
    _, registry = access
    assert (await team_access.repository_authority(caller(2), Permission.READ, repo_path=ROOT_A)).allowed
    put(registry, ROOT_A, "withdrawn")
    with pytest.raises(HTTPException) as denied:
        await team_access.repository_authority(caller(2), Permission.READ, repo_path=ROOT_A)
    assert denied.value.status_code == 403 and "workspace registry" in denied.value.detail


# Registering a repository in the workspace registry (POST /workspace/repositories).

ADMIN, CONTRIBUTOR = {"X-API-Key": "fixture-4"}, {"X-API-Key": "fixture-2"}


@pytest_asyncio.fixture
async def registration(access, tmp_path, monkeypatch):
    """The workspace router over the real registry, with an indexed, unattached repository on disk."""
    factory, registry = access
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    directory = (tmp_path / "repo-d").resolve()
    directory.mkdir()
    async with factory() as session, session.begin():
        session.add(Repository(id=4, name="D", path=str(directory)))
    app = FastAPI()
    app.include_router(workspace.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
    yield factory, registry, client, directory
    await client.aclose()


def register_body(path, **extra):
    return {"display_name": "D", "path": str(path), "trust_level": "fixture", **extra}


async def link_of(factory, repository_id: int) -> uuid.UUID | None:
    async with factory() as session:
        return (await session.execute(select(ProjectRepository.project_id)
                                      .where(ProjectRepository.repository_id == repository_id))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_team_registration_links_the_repository_to_a_project(registration, team_db):
    factory, registry, client, directory = registration
    _, _, b = team_db
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory, project_id=str(b)))
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == str(b)
    assert await link_of(factory, 4) == b
    assert len(registry.list_all()) == 1
    # The administrator is a member of B, so the newly registered repository is usable at once.
    allowed, _ = await outcome(factory, 4, 4, Permission.REQUEST_RUN)
    assert allowed


@pytest.mark.asyncio
async def test_team_registration_without_a_project_is_refused(registration):
    factory, registry, client, directory = registration
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory))
    assert response.status_code == 422 and "project_id" in response.json()["detail"]
    assert registry.list_all() == [] and await link_of(factory, 4) is None


@pytest.mark.asyncio
async def test_team_registration_reuses_an_existing_link(registration, team_db):
    factory, registry, client, directory = registration
    _, _, b = team_db
    async with factory() as session, session.begin():
        session.add(ProjectRepository(repository_id=4, project_id=b))
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory))
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == str(b)


@pytest.mark.asyncio
async def test_team_registration_of_an_unindexed_repository_is_refused(registration, tmp_path, team_db):
    factory, registry, client, _ = registration
    _, _, b = team_db
    directory = tmp_path / "not-indexed"
    directory.mkdir()
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory, project_id=str(b)))
    assert response.status_code == 409 and "indexed" in response.json()["detail"]
    assert registry.list_all() == []


@pytest.mark.asyncio
async def test_team_registration_requires_a_team_administrator(registration, team_db):
    factory, registry, client, directory = registration
    _, a, _ = team_db
    response = await client.post("/workspace/repositories", headers=CONTRIBUTOR,
                                 json=register_body(directory, project_id=str(a)))
    assert response.status_code == 403
    assert registry.list_all() == [] and await link_of(factory, 4) is None


@pytest.mark.asyncio
async def test_team_registration_does_not_move_a_repository_between_projects(registration, team_db):
    factory, registry, client, directory = registration
    _, a, b = team_db
    async with factory() as session, session.begin():
        session.add(ProjectRepository(repository_id=4, project_id=a))
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory, project_id=str(b)))
    assert response.status_code == 409 and "another project" in response.json()["detail"]
    assert registry.list_all() == [] and await link_of(factory, 4) == a


@pytest.mark.asyncio
async def test_team_registration_of_an_unknown_project_is_refused(registration):
    factory, registry, client, directory = registration
    response = await client.post("/workspace/repositories", headers=ADMIN,
                                 json=register_body(directory, project_id=str(uuid.uuid4())))
    assert response.status_code == 404
    assert registry.list_all() == []


@pytest.mark.asyncio
async def test_duplicate_registration_rolls_back_the_link(registration, team_db):
    factory, registry, client, directory = registration
    _, a, _ = team_db
    engine = factory.kw["bind"]
    if engine.dialect.name == "sqlite":
        # As in brain/database/session.py: without an explicit BEGIN, pysqlite commits on RELEASE SAVEPOINT,
        # so the link attach_repository adds in a savepoint would outlive the outer rollback.
        await engine.dispose()
        event.listen(engine.sync_engine, "connect", lambda connection, _record: setattr(
            connection, "isolation_level", None))
        event.listen(engine.sync_engine, "begin", lambda connection: connection.exec_driver_sql("BEGIN"))
    # Registered earlier and disabled: the path sandbox lets it through, the registry refuses the duplicate id.
    put(registry, str(directory), "withdrawn")
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory, project_id=str(a)))
    assert response.status_code == 409
    assert await link_of(factory, 4) is None


@pytest.mark.asyncio
async def test_solo_registration_is_unchanged(registration, monkeypatch):
    factory, registry, client, directory = registration
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    response = await client.post("/workspace/repositories", headers=ADMIN,
                                 json=register_body(directory, project_id=str(uuid.uuid4())))
    assert response.status_code == 200, response.text
    assert "project_id" not in response.json()
    assert len(registry.list_all()) == 1 and await link_of(factory, 4) is None
