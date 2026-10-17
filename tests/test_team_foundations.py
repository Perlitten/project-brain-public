import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain.auth.principals import ResolvedPrincipal, hash_credential
from brain.config.settings import settings
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import ApiCredential, Base, OAuthClient, OAuthRefreshToken, Principal, Repository
from brain.team.authorization import Permission, TeamConflict, TeamDenied, authorize, change_policy, change_role
from brain.team.models import (AgentRun, ProjectMember, ProjectRepository, Team, TeamAccessFence, TeamActivityEvent,
                               TeamIdentity, TeamInvitation, TeamMember, TeamOidcFlow, TeamProject, TeamTaskBinding)
from brain.team.store import create_task, list_tasks, request_run

TABLES = [model.__table__ for model in (Principal, ApiCredential, Repository, AgentTask, AgentTaskEvent,
          OAuthClient, OAuthRefreshToken,
          Team, TeamAccessFence, TeamMember, TeamProject, ProjectMember, ProjectRepository, TeamTaskBinding,
          AgentRun, TeamInvitation, TeamActivityEvent, TeamIdentity, TeamOidcFlow)]
NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def team_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", True)
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "full")
    postgres = os.getenv("TEST_TEAM_POSTGRES_URL")
    if postgres:
        url = make_url(postgres)
        assert url.host == "127.0.0.1" and url.database == "brain_team_test" and url.username == "fixture"
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS harness"))
            await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES))
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/team.db",
                                     execution_options={"schema_translate_map": {"harness": None}})

        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    a, b = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(Team(id=1, name="synthetic team"))
        session.add_all([Principal(id=i, name=f"human-{i}", kind="human") for i in range(1, 5)])
        await session.flush()
        for i in range(1, 5):
            session.add(ApiCredential(id=i, principal_id=i, key_hash=hash_credential(f"fixture-{i}"), scopes=["*"]))
            session.add(TeamMember(principal_id=i, team_id=1, verification_issuer="fixture", verification_subject=str(i),
                                   verified_at=NOW, is_admin=i == 4))
        session.add_all([TeamProject(id=p, team_id=1, name=str(p), permissions=[str(x) for x in Permission]) for p in (a, b)])
        await session.flush()
        session.add_all([ProjectMember(project_id=a, principal_id=i, role=role) for i, role in
                        ((1, "reader"), (2, "contributor"), (3, "reviewer"))])
        session.add(ProjectMember(project_id=b, principal_id=4, role="contributor"))
        session.add_all([Repository(id=1, name="A", path="/synthetic/A"), Repository(id=2, name="B", path="/synthetic/B")])
        await session.flush()
        session.add_all([ProjectRepository(repository_id=1, project_id=a), ProjectRepository(repository_id=2, project_id=b)])
        if postgres:
            await session.flush()
            for table in ("principals", "api_credentials"):
                await session.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                                           f"(SELECT max(id) FROM {table}))"))
    yield factory, a, b
    await engine.dispose()


def caller(i):
    return ResolvedPrincipal(i, f"human-{i}", "human", scopes=frozenset({"*"}), credential_id=i)


def task_payload(key="task-key-1", **changes):
    return dict(repository_id=1, title="synthetic", goal="no provider calls", branch="main", base_sha="a" * 40,
                permissions=["read", "run.request"], idempotency_key=key, **changes)


def run_payload(key="run-key-1", allowed=None):
    return dict(branch="isolated/run", permissions=allowed or ["read"], idempotency_key=key)


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [Permission.CREATE_TASK, Permission.REQUEST_RUN, Permission.APPROVE,
                                       Permission.EXECUTE, Permission.MERGE, Permission.TRANSFER])
async def test_reader_denials_even_with_wildcard(team_db, permission):
    factory, a, _ = team_db
    async with factory() as session:
        with pytest.raises(TeamDenied):
            await authorize(session, caller(1), a, permission, author_id=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [Permission.READ, Permission.CREATE_TASK])
async def test_project_boundary(team_db, permission):
    factory, _, b = team_db
    async with factory() as session:
        with pytest.raises(TeamDenied):
            await authorize(session, caller(2), b, permission)


@pytest.mark.asyncio
async def test_attributed_idempotent_task_and_stable_repository(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        first = await create_task(session, caller(2), a, task_payload())
        assert await create_task(session, caller(2), a, task_payload()) == first
        with pytest.raises(TeamConflict):
            await create_task(session, caller(2), a, dict(task_payload(), goal="different"))
        events = (await session.execute(select(AgentTaskEvent))).scalars().all()
        assert len(events) == 1 and events[0].actor == "principal:2"
        await session.execute(update(Repository).where(Repository.id == 1).values(path="/relocated/A"))
        assert (await list_tasks(session, caller(1), a))[0]["repository_id"] == 1
        assert first["author_id"] == 2 and first["declared_base_sha"] == "a" * 40


@pytest.mark.asyncio
async def test_run_idempotency_narrowing_and_task_bound_read(team_db):
    factory, a, b = team_db
    async with factory() as session, session.begin():
        task = await create_task(session, caller(2), a, task_payload())
        task_id = uuid.UUID(task["id"])
        await create_task(session, caller(2), a, task_payload("other-key"))
        run = await request_run(session, caller(2), a, task_id, run_payload(allowed=["read", "run.request"]))
        repeat = await request_run(session, caller(2), a, task_id, run_payload(allowed=["read", "run.request"]))
        assert repeat["id"] == run["id"] and repeat["credential"] is None
        assert run["credential"] and not run["execution_available"] and run["worktree_path"] is None
        grant = await session.get(AgentRun, uuid.UUID(run["id"]))
        child_caller = ResolvedPrincipal(grant.principal_id, "untrusted-label", "human", credential_id=grant.credential_id)
        assert [row["id"] for row in await list_tasks(session, child_caller, a)] == [task["id"]]
        with pytest.raises(TeamDenied):
            await list_tasks(session, child_caller, b)
        with pytest.raises(TeamDenied):
            await request_run(session, child_caller, a, task_id, run_payload("escalate", ["execute"]))
        child = await request_run(session, child_caller, a, task_id, run_payload("narrow-child", ["read"]))
        assert child["parent_id"] == run["id"] and child["initiator_id"] == 2
        saved = await session.get(ApiCredential, grant.credential_id)
        assert saved.key_hash == hash_credential(run["credential"]) and saved.key_hash != run["credential"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["membership", "credential", "principal", "policy", "verification"])
async def test_live_revocation_blocks_next_operation_not_cached_identity(team_db, target):
    factory, a, _ = team_db
    model, identity = {
        "membership": (ProjectMember, {"project_id": a, "principal_id": 2}),
        "credential": (ApiCredential, 2),
        "principal": (Principal, 2),
        "policy": (TeamProject, a),
        "verification": (TeamMember, 2),
    }[target]
    async with factory() as reused:
        async with reused.begin():
            held = await reused.get(model, identity)
            assert held is not None
            await authorize(reused, caller(2), a, Permission.READ)
        # Another request changes authority after the first transaction ends.
        # The caller keeps its session and loaded entity, as a long-lived
        # service can; opening a fresh session here would hide stale reads.
        async with factory() as writer, writer.begin():
            if target == "membership":
                await writer.execute(update(ProjectMember).where(ProjectMember.principal_id == 2).values(revoked_at=NOW))
            elif target == "credential":
                await writer.execute(update(ApiCredential).where(ApiCredential.id == 2).values(revoked_at=NOW))
            elif target == "principal":
                await writer.execute(update(Principal).where(Principal.id == 2).values(disabled_at=NOW))
            elif target == "policy":
                await writer.execute(update(TeamProject).where(TeamProject.id == a).values(permissions=[]))
            else:
                await writer.execute(update(TeamMember).where(TeamMember.principal_id == 2).values(verified_at=None))
        with pytest.raises(TeamDenied):
            async with reused.begin():
                await create_task(reused, caller(2), a, task_payload())
        assert held is not None  # Keep the loaded entity alive across both operations.


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="requires a disposable PostgreSQL team database")
async def test_concurrent_retries_create_one_task_and_one_run(team_db):
    import asyncio

    factory, a, _ = team_db

    async def attempt(operation):
        async with factory() as session, session.begin():
            return await operation(session)

    tasks = await asyncio.gather(*(attempt(lambda s: create_task(s, caller(2), a, task_payload("race-key")))
                                   for _ in range(4)))
    assert len({task["id"] for task in tasks}) == 1
    task_id = uuid.UUID(tasks[0]["id"])
    runs = await asyncio.gather(*(attempt(lambda s: request_run(s, caller(2), a, task_id, run_payload("race-run")))
                                  for _ in range(4)))
    assert len({run["id"] for run in runs}) == 1 and sum(bool(run["credential"]) for run in runs) == 1
    async with factory() as session:
        assert len((await session.execute(select(AgentTaskEvent))).scalars().all()) == 1
        assert len((await session.execute(select(AgentRun))).scalars().all()) == 1
        assert len((await session.execute(select(Principal).where(Principal.kind == "run"))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_revoked_parent_invalidates_delegated_run(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        task = await create_task(session, caller(2), a, task_payload())
        run = await request_run(session, caller(2), a, uuid.UUID(task["id"]), run_payload(allowed=["read", "run.request"]))
        grant = await session.get(AgentRun, uuid.UUID(run["id"]))
        child_caller = ResolvedPrincipal(grant.principal_id, "run", "run", credential_id=grant.credential_id)
        child = await request_run(session, child_caller, a, uuid.UUID(task["id"]), run_payload("child"))
        child_grant = await session.get(AgentRun, uuid.UUID(child["id"]))
        await session.execute(update(AgentRun).where(AgentRun.id == grant.id).values(revoked_at=NOW))
    leaf = ResolvedPrincipal(child_grant.principal_id, "run", "run", credential_id=child_grant.credential_id)
    async with factory() as session:
        with pytest.raises(TeamDenied):
            await list_tasks(session, leaf, a)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="requires a disposable PostgreSQL team database")
async def test_concurrent_role_changes_have_one_winner(team_db):
    import asyncio

    factory, a, _ = team_db

    async def attempt(role):
        try:
            async with factory() as session, session.begin():
                await change_role(session, caller(4), a, 2, role, 0)
            return "ok"
        except TeamConflict:
            return "conflict"

    assert sorted(await asyncio.gather(attempt("reader"), attempt("reviewer"))) == ["conflict", "ok"]
    async with factory() as session:
        assert (await session.get(ProjectMember, (a, 2))).version == 1


@pytest.mark.asyncio
async def test_accepted_cancellation_fences_later_authority_creation(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        task = await create_task(session, caller(2), a, task_payload())
        run = await request_run(session, caller(2), a, uuid.UUID(task["id"]), run_payload(allowed=["read", "run.request"]))
        assert run["execution_available"] is False
        await session.execute(update(AgentTask).where(AgentTask.id == uuid.UUID(task["id"]))
                              .values(cancellation_requested_at=NOW))
    async with factory() as session, session.begin():
        with pytest.raises(TeamDenied, match="further run requests"):
            await request_run(session, caller(2), a, uuid.UUID(task["id"]), run_payload("after-cancel"))
        assert len((await session.execute(select(AgentRun))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_independent_review_and_admin_are_separate(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        with pytest.raises(TeamDenied):
            await authorize(session, caller(3), a, Permission.APPROVE, author_id=3)
        with pytest.raises(TeamDenied):
            await authorize(session, caller(3), a, Permission.APPROVE)
        await authorize(session, caller(3), a, Permission.APPROVE, author_id=2)
        with pytest.raises(TeamDenied):
            await authorize(session, caller(4), a, Permission.READ)
        await change_role(session, caller(4), a, 2, "reader", 0)
        with pytest.raises(TeamConflict):
            await change_role(session, caller(4), a, 2, "reviewer", 0)
        await change_policy(session, caller(4), a, ["read"], 0)
        with pytest.raises(TeamConflict):
            await change_policy(session, caller(4), a, ["read", "execute"], 0)


@pytest.mark.asyncio
async def test_project_role_cannot_restore_permissions_removed_from_a_live_credential(team_db):
    factory, project, _ = team_db
    async with factory() as session, session.begin():
        await authorize(session, caller(2), project, Permission.READ)
    async with factory() as session, session.begin():
        credential = await session.get(ApiCredential, 2)
        credential.scopes = []
    # The caller still claims wildcard scopes and has a valid contributor role;
    # authority must come from the live stored credential, not either claim.
    async with factory() as session:
        with pytest.raises(TeamDenied):
            await authorize(session, caller(2), project, Permission.READ)
