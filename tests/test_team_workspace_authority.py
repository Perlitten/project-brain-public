"""One repository authority: workspace-registry trust x team membership x solo/team state.

``authorize_repository`` is the single place where the registry (``brain/workspace``:
is this repository trusted?) and the team model (``brain/team``: may this person use
it?) meet. Membership decides who; registry trust caps what anyone may do, team
administrators included. Solo state stays a team of one and ignores the registry.

In team state the registry is the shared ``workspace_registry_entries`` table every
process reads (API, worker, CLI); a legacy registry file is imported into it once and
read, with a warning, only while the table is empty.
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
from sqlalchemy import delete, event, select

from loguru import logger

from apps.api import auth, team_access
from apps.api.routers import workspace
from brain.database.models import Repository
from brain.team import registry as shared
from brain.team.authorization import FULL_AUTHORITY, Permission, TeamDenied, authorize_repository
from brain.team.models import ProjectRepository, TeamActivityEvent
from brain.workers.initiator import JobInitiator
from brain.workers.queue import JobQueue
from brain.workspace import cli as workspace_cli
from brain.workspace.models import RepositoryRecord, TrustLevel, _generate_repository_id
from brain.workspace.registry_store import RegistryStore
from tests.test_team_foundations import caller
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_job_initiator import run_one, worker_against
from tests.test_team_transport import live_team_state, membership_driven, revoke_everyone
from tests.test_worker_queue import _FakeRedis

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


async def put_table(factory, root: str, trust: str) -> None:
    """``put`` into the shared registry table instead of the legacy file."""
    record = _record(root, TrustLevel.TRUSTED_INTERNAL.value if trust == "withdrawn" else trust)
    async with factory() as session, session.begin():
        await shared.insert_entry(session, record, actor="test", source="test")
        if trust == "withdrawn":
            await shared.disable_entry(session, record.repository_id, actor="test", source="test", reason="synthetic")


async def table_ids(factory) -> set[str]:
    async with factory() as session:
        return {record.repository_id for record in await shared.table_records(session)}


async def registry_activity(factory) -> list[tuple[str, int | None, str | None, uuid.UUID | None]]:
    """``registry.*`` rows of the team activity feed: kind, actor principal, actor label, project."""
    async with factory() as session:
        rows = (await session.execute(select(TeamActivityEvent).where(TeamActivityEvent.kind.like("registry.%"))
                                      .order_by(TeamActivityEvent.id))).scalars().all()
    return [(row.kind, row.actor_principal_id, row.actor_label, row.project_id) for row in rows]


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
@pytest.mark.parametrize("source", ["table", "legacy file"])
@pytest.mark.parametrize("trust", list(TRUST))
@pytest.mark.parametrize("principal, repository_id, permission, member_allowed", MEMBERSHIP)
async def test_team_agreement_matrix(world, source, trust, principal, repository_id, permission, member_allowed):
    factory, registry = world
    if trust is not None and source == "table":
        await put_table(factory, ROOTS[repository_id], trust)
    elif trust is not None:
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
async def test_the_table_wins_over_a_stale_legacy_file(world):
    factory, registry = world
    put(registry, ROOT_B, TrustLevel.TRUSTED_INTERNAL.value)
    put(registry, ROOT_A, "withdrawn")
    await put_table(factory, ROOT_B, "withdrawn")
    allowed, reason = await outcome(factory, 4, 2, Permission.READ)
    assert not allowed and "workspace registry" in reason  # the table, not the file's "trusted"
    allowed, _ = await outcome(factory, 1, 1, Permission.READ)
    assert allowed  # A is withdrawn only in the file, which is not read once the table has entries


@pytest.mark.asyncio
async def test_reading_the_legacy_file_is_logged(world):
    factory, registry = world
    put(registry, ROOT_A, TrustLevel.UNTRUSTED_EXTERNAL.value)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        allowed, _ = await outcome(factory, 2, 1, Permission.REQUEST_RUN)
    finally:
        logger.remove(sink)
    assert not allowed
    assert any("table is empty" in message and "legacy registry file" in message for message in messages)


@pytest.mark.asyncio
async def test_the_legacy_file_is_imported_once(world):
    factory, registry = world
    put(registry, ROOT_A, "withdrawn")
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        assert await shared.ensure_legacy_imported(factory, Path(".brain/workspace")) == 1
    finally:
        logger.remove(sink)
    assert any("Imported 1 workspace registry entries" in message for message in messages)
    record_id = _generate_repository_id(ROOT_A)
    assert await table_ids(factory) == {record_id}
    async with factory() as session:
        imported = await shared.table_events(session, record_id)
        assert [event.event_type for event in imported] == [
            event.event_type for event in registry.list_events(repository_id=record_id)]
        assert shared.is_disabled(await shared.table_record(session, record_id))

    # Later edits of the file are not imported again: the table is the source of truth from now on.
    put(registry, ROOT_C, TrustLevel.TRUSTED_INTERNAL.value)
    assert await shared.ensure_legacy_imported(factory, Path(".brain/workspace")) == 0
    async with factory() as session, session.begin():
        assert await shared.import_legacy_registry(session, Path(".brain/workspace")) == 0
    assert await table_ids(factory) == {record_id}


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
    assert await table_ids(factory) == {response.json()["repository"]["repository_id"]}
    assert registry.list_all() == []  # team state writes the shared table, not this process's file
    assert await registry_activity(factory) == [("registry.registered", 4, None, b)]
    # The administrator is a member of B, so the newly registered repository is usable at once.
    allowed, _ = await outcome(factory, 4, 4, Permission.REQUEST_RUN)
    assert allowed


@pytest.mark.asyncio
async def test_team_registration_without_a_project_is_refused(registration):
    factory, registry, client, directory = registration
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory))
    assert response.status_code == 422 and "project_id" in response.json()["detail"]
    assert await table_ids(factory) == set() and await link_of(factory, 4) is None


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
    assert await table_ids(factory) == set()


@pytest.mark.asyncio
async def test_team_registration_requires_a_team_administrator(registration, team_db):
    factory, registry, client, directory = registration
    _, a, _ = team_db
    response = await client.post("/workspace/repositories", headers=CONTRIBUTOR,
                                 json=register_body(directory, project_id=str(a)))
    assert response.status_code == 403
    assert await table_ids(factory) == set() and await link_of(factory, 4) is None


@pytest.mark.asyncio
async def test_team_registration_does_not_move_a_repository_between_projects(registration, team_db):
    factory, registry, client, directory = registration
    _, a, b = team_db
    async with factory() as session, session.begin():
        session.add(ProjectRepository(repository_id=4, project_id=a))
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory, project_id=str(b)))
    assert response.status_code == 409 and "another project" in response.json()["detail"]
    assert await table_ids(factory) == set() and await link_of(factory, 4) == a


@pytest.mark.asyncio
async def test_team_registration_of_an_unknown_project_is_refused(registration):
    factory, registry, client, directory = registration
    response = await client.post("/workspace/repositories", headers=ADMIN,
                                 json=register_body(directory, project_id=str(uuid.uuid4())))
    assert response.status_code == 404
    assert await table_ids(factory) == set()


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
    # The entry sits in the legacy file, so this also proves the file is imported before the write.
    put(registry, str(directory), "withdrawn")
    response = await client.post("/workspace/repositories", headers=ADMIN, json=register_body(directory, project_id=str(a)))
    assert response.status_code == 409
    assert await link_of(factory, 4) is None
    assert await table_ids(factory) == {_generate_repository_id(str(directory))}
    assert await registry_activity(factory) == []


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
    assert await table_ids(factory) == set()


# Re-enabling and inspecting a disabled entry (team administrators, audited).


async def register_d(client, project: uuid.UUID, directory) -> str:
    response = await client.post("/workspace/repositories", headers=ADMIN,
                                 json=register_body(directory, project_id=str(project)))
    assert response.status_code == 200, response.text
    return response.json()["repository"]["repository_id"]


@pytest.mark.asyncio
async def test_administrator_inspects_and_re_enables_a_disabled_repository(registration, team_db):
    factory, _, client, directory = registration
    _, a, _ = team_db
    record_id = await register_d(client, a, directory)
    assert (await client.get(f"/workspace/repositories/{record_id}", headers=CONTRIBUTOR)).status_code == 200

    assert (await client.post(f"/workspace/repositories/{record_id}/disable", headers=ADMIN)).status_code == 200
    assert (await client.get(f"/workspace/repositories/{record_id}", headers=CONTRIBUTOR)).status_code == 403
    inspected = await client.get(f"/workspace/repositories/{record_id}", headers=ADMIN)
    assert inspected.status_code == 200 and inspected.json()["repository"]["disabled"] is True

    enabled = await client.post(f"/workspace/repositories/{record_id}/enable", headers=ADMIN)
    assert enabled.status_code == 200, enabled.text
    assert (enabled.json()["repository"]["disabled"], enabled.json()["repository"]["trust_level"]) == (False, "fixture")
    assert (await client.get(f"/workspace/repositories/{record_id}", headers=CONTRIBUTOR)).status_code == 200
    assert [kind for kind, *_ in await registry_activity(factory)] == [
        "registry.registered", "registry.disabled", "registry.inspected", "registry.enabled"]
    assert {project for *_, project in await registry_activity(factory)} == {a}


@pytest.mark.asyncio
async def test_re_enable_takes_an_explicit_level_and_refuses_what_is_not_disabled(registration, team_db):
    factory, _, client, directory = registration
    _, a, _ = team_db
    record_id = await register_d(client, a, directory)
    enable = f"/workspace/repositories/{record_id}/enable"
    assert (await client.post(enable, headers=ADMIN)).status_code == 409  # not disabled
    await client.post(f"/workspace/repositories/{record_id}/disable", headers=ADMIN)
    assert (await client.post(enable, headers=ADMIN, params={"trust_level": "bogus"})).status_code == 400
    assert (await client.post(enable, headers=CONTRIBUTOR)).status_code == 403
    response = await client.post(enable, headers=ADMIN, params={"trust_level": "untrusted_external"})
    assert response.status_code == 200 and response.json()["repository"]["trust_level"] == "untrusted_external"
    assert (await client.post("/workspace/repositories/repo-unknown/enable", headers=ADMIN)).status_code == 404
    async with factory() as session:
        assert (await shared.table_record(session, record_id)).trust_level == "untrusted_external"


@pytest.mark.asyncio
async def test_disabled_entry_by_id_stays_closed_to_non_administrators(registration, team_db):
    factory, _, client, directory = registration
    _, a, _ = team_db
    record_id = await register_d(client, a, directory)
    await client.post(f"/workspace/repositories/{record_id}/disable", headers=ADMIN)
    denied = await client.get(f"/workspace/repositories/{record_id}", headers=CONTRIBUTOR)
    assert denied.status_code == 403 and "workspace registry" in denied.json()["detail"]
    assert "registry.inspected" not in [kind for kind, *_ in await registry_activity(factory)]


# The worker re-authorizes queued jobs against the same registry the API writes.


@pytest.mark.asyncio
async def test_worker_sees_the_trust_the_api_sets(registration, team_db, tmp_path, monkeypatch):
    factory, _, client, directory = registration
    _, _, b = team_db
    record_id = await register_d(client, b, directory)  # team administrator 4 is a contributor of B

    # The worker runs elsewhere: its own working directory holds a stale file that still trusts D.
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    monkeypatch.chdir(worker_dir)
    stale = _record(str(directory), TrustLevel.TRUSTED_INTERNAL.value)
    RegistryStore(Path(".brain/workspace")).register(stale, actor="test", source="test")

    execute = worker_against(factory, monkeypatch)
    queue = JobQueue(_FakeRedis())
    initiator = JobInitiator.for_principal(caller(4), Permission.REQUEST_RUN)
    job = await queue.enqueue("reindex", {"repo_path": str(directory)}, initiator=initiator)
    assert (await run_one(queue, job))["status"] == "completed"

    assert (await client.post(f"/workspace/repositories/{record_id}/disable", headers=ADMIN)).status_code == 200
    refused = await run_one(queue, await queue.enqueue("reindex", {"repo_path": str(directory)}, initiator=initiator))
    assert refused["status"] == "failed"
    assert refused["error"].startswith("Initiator no longer authorized") and "workspace registry" in refused["error"]

    assert (await client.post(f"/workspace/repositories/{record_id}/enable", headers=ADMIN)).status_code == 200
    job = await queue.enqueue("reindex", {"repo_path": str(directory)}, initiator=initiator)
    assert (await run_one(queue, job))["status"] == "completed"
    assert execute.await_count == 2


# The offline ``brain workspace`` CLI in team state.


def cli_args(*argv: str):
    return workspace_cli.build_parser().parse_args(["--brain-dir", ".brain/workspace", *argv])


@pytest.fixture
def team_cli(registration, monkeypatch):
    factory, registry, _, directory = registration
    monkeypatch.setattr("brain.database.session.async_session_factory", factory)
    return factory, registry, directory


@pytest.mark.asyncio
async def test_team_cli_registration_needs_a_project(team_cli, capsys):
    factory, registry, directory = team_cli
    with pytest.raises(SystemExit) as refused:
        await workspace_cli.run_team(cli_args("repositories", "register", "--path", str(directory), "--trust", "fixture"))
    assert refused.value.code == 2 and "--project" in capsys.readouterr().err
    assert await table_ids(factory) == set() and registry.list_all() == [] and await link_of(factory, 4) is None


@pytest.mark.asyncio
async def test_team_cli_registers_with_a_project_link_in_one_transaction(team_cli, team_db, capsys):
    factory, registry, directory = team_cli
    _, _, b = team_db
    assert await workspace_cli.run_team(cli_args(
        "repositories", "register", "--path", str(directory), "--trust", "fixture", "--project", str(b))) is True
    record_id = _generate_repository_id(str(directory))
    assert await table_ids(factory) == {record_id} and registry.list_all() == []
    assert await link_of(factory, 4) == b
    assert await registry_activity(factory) == [("registry.registered", None, "cli", b)]

    with pytest.raises(SystemExit) as duplicate:  # a refused registration leaves the link alone
        await workspace_cli.run_team(cli_args(
            "repositories", "register", "--path", str(directory), "--trust", "fixture", "--project", str(b)))
    assert duplicate.value.code == 1


@pytest.mark.asyncio
async def test_team_cli_refuses_an_unknown_project_or_an_unindexed_path(team_cli, tmp_path):
    factory, _, directory = team_cli
    with pytest.raises(SystemExit) as unknown:
        await workspace_cli.run_team(cli_args(
            "repositories", "register", "--path", str(directory), "--trust", "fixture", "--project", "no-such-project"))
    assert unknown.value.code == 1
    unindexed = tmp_path / "not-indexed"
    unindexed.mkdir()
    with pytest.raises(SystemExit) as refused:
        await workspace_cli.run_team(cli_args(
            "repositories", "register", "--path", str(unindexed), "--trust", "fixture", "--project", "x"))
    assert refused.value.code == 1
    assert await table_ids(factory) == set() and await link_of(factory, 4) is None


@pytest.mark.asyncio
async def test_team_cli_disables_and_re_enables_through_the_shared_table(team_cli, team_db, capsys):
    factory, _, directory = team_cli
    _, _, b = team_db
    await workspace_cli.run_team(cli_args(
        "repositories", "register", "--path", str(directory), "--trust", "fixture", "--project", str(b)))
    record_id = _generate_repository_id(str(directory))
    await workspace_cli.run_team(cli_args("repositories", "disable", record_id))
    async with factory() as session:
        assert shared.is_disabled(await shared.table_record(session, record_id))
    await workspace_cli.run_team(cli_args("repositories", "enable", record_id))
    capsys.readouterr()
    await workspace_cli.run_team(cli_args("repositories", "show", record_id))
    shown = json.loads(capsys.readouterr().out)
    assert (shown["disabled"], shown["trust_level"]) == (False, "fixture")
    assert [kind for kind, *_ in await registry_activity(factory)] == [
        "registry.registered", "registry.disabled", "registry.enabled"]


@pytest.mark.asyncio
async def test_cli_keeps_the_local_file_in_solo_state(team_cli, monkeypatch):
    factory, _, _ = team_cli
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    assert await workspace_cli.run_team(cli_args("repositories", "list")) is False


@pytest.mark.asyncio
async def test_admin_adds_unindexed_source_with_atomic_project_link(registration, team_db):
    factory, _, client, directory = registration
    _, project, _ = team_db
    async with factory() as session, session.begin():
        await session.execute(delete(Repository).where(Repository.id == 4))
    response = await client.post("/workspace/repositories", headers=ADMIN,
                                 json=register_body(directory, project_id=str(project), create_index_record=True))
    assert response.status_code == 200, response.text
    async with factory() as session:
        repository = (await session.execute(select(Repository).where(Repository.path == str(directory)))).scalar_one()
        assert repository.indexing_status == "pending" and repository.last_indexed_commit is None
        repository_id = repository.id
    assert await link_of(factory, repository_id) == project
    assert len(await registry_activity(factory)) == 1
    again = await client.post("/workspace/repositories", headers=ADMIN,
                              json=register_body(directory, project_id=str(project), create_index_record=True))
    assert again.status_code in {400, 409}
    assert len(await registry_activity(factory)) == 1


@pytest.mark.asyncio
async def test_add_unindexed_source_refusals_leave_no_identity_or_link(registration, team_db):
    factory, _, client, directory = registration
    _, project, _ = team_db
    async with factory() as session, session.begin():
        await session.execute(delete(Repository).where(Repository.id == 4))
    for headers, target, expected in [(CONTRIBUTOR, project, 403), (ADMIN, uuid.uuid4(), 404)]:
        response = await client.post("/workspace/repositories", headers=headers,
                                     json=register_body(directory, project_id=str(target), create_index_record=True))
        assert response.status_code == expected, response.text
        async with factory() as session:
            assert (await session.execute(select(Repository).where(Repository.path == str(directory)))).scalar_one_or_none() is None
        assert await registry_activity(factory) == []
        assert await table_ids(factory) == set()
