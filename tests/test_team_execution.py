"""M2 bounded execution: gates, capabilities, revocation checkpoints, fencing and evidence."""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, make_url, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain.auth.principals import ResolvedPrincipal, hash_credential
from brain.config.settings import settings
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import (ApiCredential, Base, Decision, MemoryProposal, OAuthClient, OAuthRefreshToken,
                                   Principal, Repository, Rule)
from brain.execution.bounded import SubprocessBackend
from brain.team import execution, provisioning
from brain.team.authorization import Permission, TeamConflict, TeamDenied, authorize
from brain.team.execution import ExecutionDenied, ExecutionRunner, Lease
from brain.team.models import (AgentRun, AgentRunExecution, ProjectMember, ProjectRepository, Team, TeamAccessFence,
                               TeamActivityEvent, TeamExecutionGrant, TeamIdentity, TeamInvitation, TeamMember,
                               TeamIntegrationEntry, TeamOidcFlow, TeamProject, TeamResourceLease,
                               TeamRunResolution, TeamTaskBinding, TeamTaskPlan, WorkspaceRegistryEntry,
                               WorkspaceRegistryEvent)
from brain.team.store import create_task, request_run

TABLES = [model.__table__ for model in (Principal, ApiCredential, Repository, AgentTask, AgentTaskEvent,
          OAuthClient, OAuthRefreshToken, Team, TeamAccessFence, TeamMember, TeamProject, ProjectMember,
          ProjectRepository, TeamTaskBinding, AgentRun, TeamInvitation, TeamExecutionGrant, AgentRunExecution,
          # Member revocation logs activity and withdraws memory proposals.
          TeamActivityEvent, Decision, Rule, MemoryProposal,
          # SSO tables reference invitations; the shared PostgreSQL database must drop them first.
          TeamIdentity, TeamOidcFlow,
          # Coordination (M3) tables reference task bindings, runs and repositories.
          TeamTaskPlan, TeamResourceLease, TeamRunResolution, TeamIntegrationEntry,
          # Shared workspace registry: repository trust every process reads in team state.
          WorkspaceRegistryEntry, WorkspaceRegistryEvent)]
NOW = datetime.now(timezone.utc)
SECRET_VALUE = "fixture-secret-value-7f3a9c"
PY = sys.executable
TOOLS = {
    "hello": {"argv": [PY, "-c", "print('hello from a bounded step')"]},
    "touch": {"argv": [PY, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('x')"], "allow_args": True},
    "leak": {"argv": [PY, "-c", "import os; print('token=' + os.environ.get('BRAIN_SECRET_TOKEN', 'missing'))"]},
    "slow": {"argv": [PY, "-c", "import pathlib,time; time.sleep(30); pathlib.Path('late.txt').write_text('x')"]},
    "fail": {"argv": [PY, "-c", "import sys; sys.exit(3)"]},
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    (repo / "README.md").write_text("synthetic\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "synthetic")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ROOT", str(tmp_path / "executions"))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", json.dumps(TOOLS))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_SECRETS", json.dumps({"token": "FIXTURE_EXEC_TOKEN"}))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LEASE_SECONDS", 30)
    monkeypatch.setenv("FIXTURE_EXEC_TOKEN", SECRET_VALUE)
    return tmp_path / "executions"


@pytest_asyncio.fixture
async def team_db(tmp_path, monkeypatch, source_repo):
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "full")
    postgres = os.getenv("TEST_TEAM_POSTGRES_URL")
    if postgres:  # the same disposable database the foundations suite uses
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
    repo, _ = source_repo
    async with factory() as session, session.begin():
        session.add(Team(id=1, name="synthetic team"))
        session.add_all([Principal(id=i, name=f"human-{i}", kind="human") for i in range(1, 5)])
        await session.flush()
        for i in range(1, 5):
            session.add(ApiCredential(id=i, principal_id=i, key_hash=hash_credential(f"fixture-{i}"), scopes=["*"]))
            session.add(TeamMember(principal_id=i, team_id=1, verification_issuer="fixture",
                                   verification_subject=str(i), verified_at=NOW, is_admin=i == 4))
        session.add(TeamProject(id=a, team_id=1, name="a", permissions=[str(x) for x in Permission]))
        session.add(TeamProject(id=b, team_id=1, name="b",
                                permissions=[str(x) for x in Permission if x != Permission.EXECUTE]))
        await session.flush()
        session.add_all([ProjectMember(project_id=a, principal_id=i, role=role) for i, role in
                         ((1, "reader"), (2, "contributor"), (3, "reviewer"))])
        session.add_all([ProjectMember(project_id=b, principal_id=2, role="contributor"),
                         ProjectMember(project_id=a, principal_id=4, role="reader")])
        session.add_all([Repository(id=1, name="A", path=str(repo)), Repository(id=2, name="B", path=str(repo) + "-b")])
        await session.flush()
        session.add_all([ProjectRepository(repository_id=1, project_id=a),
                         ProjectRepository(repository_id=2, project_id=b)])
        if postgres:
            await session.flush()
            for table in ("principals", "api_credentials"):
                await session.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                                           f"(SELECT max(id) FROM {table}))"))
    yield factory, a, b
    await engine.dispose()


def caller(i):
    return ResolvedPrincipal(i, f"human-{i}", "human", scopes=frozenset({"*"}), credential_id=i)


async def make_run(factory, project, base_sha, *, who=2, repository_id=1, permissions=("read", "run.request")):
    async with factory() as session, session.begin():
        task = await create_task(session, caller(who), project, dict(
            repository_id=repository_id, title="synthetic", goal="bounded", branch="main", base_sha=base_sha,
            permissions=["read", "run.request"], idempotency_key=f"task-{uuid.uuid4().hex}"))
        task_id = uuid.UUID(task["id"])
        run = await request_run(session, caller(who), project, task_id, dict(
            branch=f"brain/run-{uuid.uuid4().hex[:8]}", permissions=list(permissions),
            idempotency_key=f"run-{uuid.uuid4().hex}"))
    return task_id, uuid.UUID(run["id"]), run


async def grant(factory, project, principal=2):
    async with factory() as session, session.begin():
        await execution.set_grant(session, project, principal, "operator:test")


def payload(*steps, key=None, **extra):
    return {"idempotency_key": key or f"exec-{uuid.uuid4().hex}",
            "steps": [step if isinstance(step, dict) else {"tool": step} for step in steps], **extra}


async def request(factory, project, task_id, run_id, body, who=2, **kwargs):
    async with factory() as session, session.begin():
        return await execution.request_execution(session, caller(who), project, task_id, run_id, body, **kwargs)


@dataclass
class HookBackend(SubprocessBackend):
    """The real subprocess backend with a hook after a chosen step (simulates a concurrent revocation)."""
    hook_after: int = -1
    hook: object = None

    async def run_step(self, copy, step, should_stop):
        result = await super().run_step(copy, step, should_stop)
        if step.index == self.hook_after and self.hook is not None:
            await self.hook()
        return result


# --------------------------------------------------------------------------- default denial and gates


@pytest.mark.asyncio
async def test_execution_is_denied_by_default(team_db, source_repo):
    factory, a, _ = team_db
    _, sha = source_repo
    task_id, run_id, run = await make_run(factory, a, sha)
    assert run["execution_available"] is False
    assert settings.BRAIN_EXECUTION_ENABLED is False
    with pytest.raises(ExecutionDenied, match="disabled"):
        await request(factory, a, task_id, run_id, payload("hello"))
    async with factory() as session:
        for who in (2, 3):  # no role implies execute, enabled or not
            with pytest.raises(TeamDenied):
                await authorize(session, caller(who), a, Permission.EXECUTE)
        assert (await session.execute(select(AgentRunExecution))).first() is None


@pytest.mark.asyncio
async def test_enabled_without_root_is_still_denied(team_db, source_repo, monkeypatch, enabled):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ROOT", None)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    with pytest.raises(ExecutionDenied, match="BRAIN_EXECUTION_ROOT"):
        await request(factory, a, task_id, run_id, payload("hello"))


@pytest.mark.asyncio
async def test_denied_without_grant_and_grants_follow_roles(team_db, source_repo, enabled):
    factory, a, b = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    with pytest.raises(ExecutionDenied, match="no execution grant"):
        await request(factory, a, task_id, run_id, payload("hello"))
    async with factory() as session, session.begin():
        with pytest.raises(TeamConflict, match="Contributor or Reviewer"):
            await execution.set_grant(session, a, 1, "operator:test")  # reader
        with pytest.raises(TeamConflict, match="Contributor or Reviewer"):
            await execution.set_grant(session, a, 4, "operator:test")  # admin, but only a reader here
        with pytest.raises(TeamDenied):  # project administration needs a team admin, not a contributor
            await execution.grant_execution(session, caller(2), a, 2)
    # A grant on another project does not carry over.
    await grant(factory, b)
    with pytest.raises(ExecutionDenied, match="no execution grant"):
        await request(factory, a, task_id, run_id, payload("hello"))
    # A project whose policy omits execute denies even with a grant.
    task_b, run_b, _ = await make_run(factory, b, source_repo[1], repository_id=2)
    with pytest.raises(ExecutionDenied, match="policy"):
        await request(factory, b, task_b, run_b, payload("hello"))


@pytest.mark.asyncio
async def test_only_the_requester_may_execute_a_run(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a, 2)
    await grant(factory, a, 3)
    with pytest.raises(ExecutionDenied, match="requested the run"):
        await request(factory, a, task_id, run_id, payload("hello"), who=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("body, message", [
    (payload("rmrf"), "Tool not allowed"),
    (payload("hello", tools=["hello", "unknown"]), "Tool not allowed"),
    (payload("hello", egress=["example.com"]), "Egress is not available"),
    (payload({"tool": "hello", "args": ["x"]}), "does not accept arguments"),
    (payload({"tool": "touch", "args": ["../outside.txt"]}), "must stay inside the working copy"),
    (payload({"tool": "hello", "secrets": ["other"]}), "Secret reference not allowed"),
    (payload("hello", budget={"wall_seconds": 10 ** 6}), "exceeds the allowed ceiling"),
])
async def test_capabilities_outside_the_allowlists_are_denied(team_db, source_repo, enabled, body, message):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    with pytest.raises(ExecutionDenied, match=message):
        await request(factory, a, task_id, run_id, body)


@pytest.mark.asyncio
async def test_egress_outside_the_allowlist_is_denied_even_on_an_enforcing_backend(team_db, source_repo, enabled,
                                                                                   monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "pypi.org")
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    enforcing = SubprocessBackend()
    object.__setattr__(enforcing, "enforces_egress", True)
    with pytest.raises(ExecutionDenied, match="Egress destination not allowed"):
        await request(factory, a, task_id, run_id, payload("hello", egress=["example.com"]), backend=enforcing)


@pytest.mark.asyncio
async def test_idempotency_is_bound_to_content_and_run(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    body = payload("hello", key="exec-key-0001")
    first = await request(factory, a, task_id, run_id, body)
    assert first["state"] == "requested" and first["tools"] == ["hello"] and first["egress"] == []
    assert await request(factory, a, task_id, run_id, body) == first
    with pytest.raises(TeamConflict):
        await request(factory, a, task_id, run_id, payload("hello", "hello", key="exec-key-0001"))
    with pytest.raises(TeamConflict):
        await request(factory, a, task_id, run_id, payload("hello", key="exec-key-0002"))
    # The same key on another run is a different request.
    task_2, run_2, _ = await make_run(factory, a, source_repo[1])
    assert (await request(factory, a, task_2, run_2, body))["run_id"] == str(run_2)


@pytest.mark.asyncio
async def test_child_runs_narrow_to_parent_capabilities(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, parent_id, parent = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    async with factory() as session:
        row = await session.get(AgentRun, parent_id)
    parent_caller = ResolvedPrincipal(row.principal_id, "run", "run", scopes=frozenset({"team:read", "team:run.request"}),
                                      credential_id=row.credential_id)
    async with factory() as session, session.begin():
        child = await request_run(session, parent_caller, a, task_id, dict(
            branch="brain/child", permissions=["read"], idempotency_key="child-run-key"))
    child_id = uuid.UUID(child["id"])

    async def child_request(body):
        async with factory() as session, session.begin():
            return await execution.request_execution(session, parent_caller, a, task_id, child_id, body)

    with pytest.raises(ExecutionDenied, match="parent run holds no executor capabilities"):
        await child_request(payload("hello"))
    await request(factory, a, task_id, parent_id, payload("hello", budget={"wall_seconds": 60}))
    with pytest.raises(ExecutionDenied, match="Tool not allowed"):
        await child_request(payload("leak"))
    with pytest.raises(ExecutionDenied, match="exceeds the allowed ceiling"):
        await child_request(payload("hello", budget={"wall_seconds": 61}))
    state = await child_request(payload("hello"))
    assert state["budget"]["wall_seconds"] == 60


# --------------------------------------------------------------------------- execution, evidence, secrets


async def _all_rows_text(factory) -> str:
    chunks = []
    async with factory() as session:
        for table in TABLES:
            for row in (await session.execute(select(table))).all():
                chunks.append(repr(tuple(row)))
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_execution_records_evidence_and_never_stores_secrets(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    requested = await request(factory, a, task_id, run_id, payload(
        "hello", {"tool": "leak", "secrets": ["token"]}, {"tool": "touch", "args": ["made-by-run.txt"]}))
    assert requested["secret_refs"] == ["token"]
    state = await execution.execute_run(run_id, factory=factory)
    assert state["state"] == "completed", state["events"]
    steps = [item["payload"] for item in state["events"] if item["type"] == "run.execution.step"]
    assert [step["exit_code"] for step in steps] == [0, 0, 0]
    assert all(len(step["output_sha256"]) == 64 and step["argv"][0] for step in steps)
    assert "[REDACTED]" in steps[1]["excerpt"]
    completed = state["events"][-1]["payload"]
    assert completed["base_sha"] == source_repo[1] and len(completed["head_sha"]) == 40 and completed["dirty"]
    copy = Path(completed["worktree_path"])
    assert (copy / "made-by-run.txt").is_file() and not (source_repo[0] / "made-by-run.txt").exists()
    assert copy.resolve().is_relative_to(enabled.resolve())
    # The secret value is nowhere: no database row, no API state, no stored log.
    assert SECRET_VALUE not in await _all_rows_text(factory)
    assert SECRET_VALUE not in json.dumps(state)
    for log in enabled.rglob("*.log"):
        assert SECRET_VALUE not in log.read_text(encoding="utf-8", errors="replace")
    # One status machine: the run's ledger events, no extra status column.
    assert not hasattr(AgentRunExecution, "status")
    async with factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run.head_sha == completed["head_sha"]
        events = (await session.execute(select(AgentTaskEvent.event_type).where(AgentTaskEvent.task_id == task_id)
                                        .order_by(AgentTaskEvent.id))).scalars().all()
    assert [e for e in events if e.startswith("run.execution.")] == [
        "run.execution.requested", "run.execution.started", "run.execution.step", "run.execution.step",
        "run.execution.step", "run.execution.completed"]
    # A finished execution never starts again.
    assert (await execution.execute_run(run_id, factory=factory))["state"] == "completed"


@pytest.mark.asyncio
async def test_failing_step_stops_the_run(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("fail", {"tool": "touch", "args": ["after.txt"]}))
    state = await execution.execute_run(run_id, factory=factory)
    assert state["state"] == "failed" and state["events"][-1]["payload"]["failed_step"] == 0
    assert not list(enabled.rglob("after.txt"))


async def _revoke(factory, project, kind, run_id):
    async with factory() as session, session.begin():
        if kind == "grant":
            await execution.revoke_grant(session, project, 2, "operator:test")
        elif kind == "credential":
            await session.execute(update(ApiCredential).where(ApiCredential.id == 2).values(revoked_at=NOW))
        elif kind == "project_member":
            await provisioning.revoke_project_member(session, project_id=project, principal_id=2)
        elif kind == "run":
            await session.execute(update(AgentRun).where(AgentRun.id == run_id).values(revoked_at=NOW))
        elif kind == "policy":
            await session.execute(update(TeamProject).where(TeamProject.id == project).values(
                permissions=[str(x) for x in Permission if x != Permission.EXECUTE]))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grant", "credential", "project_member", "run", "policy"])
async def test_revocation_between_steps_stops_the_next_step(team_db, source_repo, enabled, kind):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload({"tool": "touch", "args": ["first.txt"]},
                                                       {"tool": "touch", "args": ["second.txt"]}))

    async def revoke():
        await _revoke(factory, a, kind, run_id)

    backend = HookBackend(hook_after=0, hook=revoke)
    state = await execution.execute_run(run_id, factory=factory, backend=backend)
    assert state["state"] == "revoked", state["events"]
    final = state["events"][-1]["payload"]
    # Honest about the step that ran but could no longer be recorded.
    assert final["in_flight"]["state"] == "finished_unrecorded" and final["in_flight"]["step"] == 0
    assert state["cancel_requested_at"] is not None
    assert list(enabled.rglob("first.txt")) and not list(enabled.rglob("second.txt"))


@pytest.mark.asyncio
async def test_revocation_mid_step_terminates_the_process(team_db, source_repo, enabled, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LEASE_SECONDS", 1)  # checks every ~0.33 s
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("slow", {"tool": "touch", "args": ["never.txt"]}))

    async def revoke_soon():
        await asyncio.sleep(1.5)
        await _revoke(factory, a, "grant", run_id)

    started = asyncio.get_running_loop().time()
    revoker = asyncio.create_task(revoke_soon())
    state = await execution.execute_run(run_id, factory=factory)
    await revoker
    assert asyncio.get_running_loop().time() - started < 20
    assert state["state"] == "revoked", state["events"]
    in_flight = state["events"][-1]["payload"]["in_flight"]
    assert in_flight["state"] == "terminated" and in_flight["evidence"]["stopped"] == "revoked"
    assert not list(enabled.rglob("late.txt")) and not list(enabled.rglob("never.txt"))


@pytest.mark.asyncio
async def test_cancel_before_start_and_during_a_run(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("hello"))
    async with factory() as session, session.begin():
        with pytest.raises(TeamDenied):  # a reader on the project may not cancel someone else's run
            await execution.cancel_execution(session, caller(1), a, task_id, run_id)
        state = await execution.cancel_execution(session, caller(2), a, task_id, run_id)
    assert state["state"] == "cancelled"
    assert (await execution.execute_run(run_id, factory=factory))["state"] == "cancelled"

    task_2, run_2, _ = await make_run(factory, a, source_repo[1])
    await request(factory, a, task_2, run_2, payload({"tool": "touch", "args": ["one.txt"]},
                                                     {"tool": "touch", "args": ["two.txt"]}))

    async def cancel():
        async with factory() as session, session.begin():
            await execution.cancel_execution(session, caller(2), a, task_2, run_2)

    state = await execution.execute_run(run_2, factory=factory, backend=HookBackend(hook_after=0, hook=cancel))
    assert state["state"] == "cancelled" and not list(enabled.rglob("two.txt"))


# --------------------------------------------------------------------------- lease, fencing, crash recovery


async def _queued(factory, a, source_repo):
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("hello", "hello"))
    return task_id, run_id


async def _expire(factory, run_id):
    async with factory() as session, session.begin():
        await session.execute(update(AgentRunExecution).where(AgentRunExecution.run_id == run_id)
                              .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)))


@pytest.mark.asyncio
async def test_expired_lease_owner_cannot_commit(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, run_id = await _queued(factory, a, source_repo)
    policy = execution.operator_policy()
    lease = await execution.acquire_lease(factory, run_id, "owner-a", lease_seconds=30)
    assert lease is not None and lease.token == 1
    assert await execution.acquire_lease(factory, run_id, "owner-b", lease_seconds=30) is None  # single owner
    runner = ExecutionRunner(factory, lease, a, task_id, policy, SubprocessBackend())
    assert (await runner.checkpoint())[0] == "ok"
    await _expire(factory, run_id)

    async def write(session, row, run):
        row.steps_done = 99

    assert (await runner.checkpoint(write))[0] == "fenced"
    assert await runner.conclude("completed", {}) is False
    async with factory() as session:
        row = await session.get(AgentRunExecution, run_id)
        assert row.steps_done == 0 and row.released_at is None
    # A stale token is fenced even before expiry.
    stale = ExecutionRunner(factory, Lease(run_id, "owner-a", 0), a, task_id, policy, SubprocessBackend())
    assert (await stale.checkpoint())[0] == "fenced"


@pytest.mark.asyncio
async def test_restart_fences_orphaned_runs_and_never_resumes_them(team_db, source_repo, enabled):
    factory, a, _ = team_db
    task_id, run_id = await _queued(factory, a, source_repo)
    policy = execution.operator_policy()
    # A worker took the lease, started, then died without releasing it.
    lease = await execution.acquire_lease(factory, run_id, "crashed-owner", lease_seconds=30)
    crashed = ExecutionRunner(factory, lease, a, task_id, policy, SubprocessBackend())

    async def started(session, row, run):
        execution._event(session, run, "started", f"{lease.token}:started", crashed.actor, {})

    assert (await crashed.checkpoint(started))[0] == "ok"
    await _expire(factory, run_id)
    # The next worker start (execute_run also recovers first) fences it.
    state = await execution.execute_run(run_id, factory=factory)
    assert state["state"] == "fenced"
    fenced = state["events"][-1]["payload"]
    assert fenced["previous_owner"] == "crashed-owner" and fenced["steps_recorded"] == 0
    assert fenced["in_flight"]["state"] == "unknown" and fenced["fencing_token"] == 2
    assert await execution.recover_orphaned_runs(factory) == []
    assert await execution.acquire_lease(factory, run_id, "late", lease_seconds=30) is None
    assert await crashed.conclude("completed", {}) is False
    assert (await execution.execution_state(factory, run_id))["state"] == "fenced"


# --------------------------------------------------------------------------- grant lifecycle


@pytest.mark.asyncio
async def test_member_revocation_revokes_execution_grants(team_db, source_repo):
    factory, a, b = team_db
    await grant(factory, a, 2)
    await grant(factory, b, 2)
    await grant(factory, a, 3)
    async with factory() as session, session.begin():
        await provisioning.revoke_project_member(session, project_id=a, principal_id=3)
        principal = await session.get(Principal, 2)
        await provisioning.revoke_member(session, principal=principal)
    async with factory() as session:
        grants = (await session.execute(select(TeamExecutionGrant))).scalars().all()
    assert grants and all(g.revoked_at is not None for g in grants)
    # Re-adding the role does not revive the grant.
    async with factory() as session, session.begin():
        await provisioning.assign_project_member(session, project_id=a, principal_id=3, role="reviewer")
        assert all(row["active"] is False for row in await execution.list_grants(session, a))
        renewed = await execution.set_grant(session, a, 3, "operator:test")
        assert renewed.revoked_at is None and renewed.version == 3
        assert (await execution.set_grant(session, a, 3, "operator:test")).version == 3  # no-op renewal
        await execution.revoke_grant(session, a, 3, "principal:4")
    async with factory() as session:
        events = (await session.execute(select(TeamActivityEvent).where(TeamActivityEvent.kind.like("execution.%"))
                                        .order_by(TeamActivityEvent.id))).scalars().all()
    # Effective grant changes reach the admin activity feed; the cascade is covered by the membership events.
    assert [(e.kind, e.project_id, e.subject_principal_id, e.actor_principal_id, e.actor_label, e.audience)
            for e in events] == [
        ("execution.granted", a, 2, None, "operator", "admin"), ("execution.granted", b, 2, None, "operator", "admin"),
        ("execution.granted", a, 3, None, "operator", "admin"), ("execution.granted", a, 3, None, "operator", "admin"),
        ("execution.revoked", a, 3, 4, None, "admin")]
    async with factory() as session:
        assert {e.kind for e in (await session.execute(select(TeamActivityEvent))).scalars()} >= {
            "membership.revoked", "project_role.revoked"}


# --------------------------------------------------------------------------- HTTP surface


@pytest.mark.asyncio
async def test_http_execution_routes_sit_behind_every_gate(team_db, source_repo, monkeypatch):
    from unittest.mock import AsyncMock

    from apps.api.routers import team as team_router
    from tests.test_team_transport import ADMIN, CONTRIBUTOR, READER, client_for, membership_driven, personal_app

    factory, a, _ = team_db
    membership_driven(monkeypatch)
    app = personal_app(factory, monkeypatch)
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="job-exec-1")
    monkeypatch.setattr(team_router, "_execution_queue", lambda: queue)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    base = f"/team/projects/{a}/tasks/{task_id}/runs/{run_id}/execution"
    body = payload("hello", key="http-exec-0001")
    async with client_for(app) as client:
        disabled = await client.post(base, headers=CONTRIBUTOR, json=body)
        assert disabled.status_code == 403 and "disabled" in disabled.json()["detail"]
        monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENABLED", True)
        monkeypatch.setattr(settings, "BRAIN_EXECUTION_ROOT", str(source_repo[0].parent / "executions"))
        monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", json.dumps(TOOLS))
        no_grant = await client.post(base, headers=CONTRIBUTOR, json=body)
        assert no_grant.status_code == 403 and "no execution grant" in no_grant.json()["detail"]
        grant_url = f"/team/projects/{a}/members/2/execution"
        assert (await client.put(grant_url, headers=CONTRIBUTOR)).status_code == 403
        granted = await client.put(grant_url, headers=ADMIN)
        assert granted.status_code == 200 and granted.json()["active"] is True
        listed = await client.get(f"/team/projects/{a}/execution-grants", headers=ADMIN)
        assert [row["principal_id"] for row in listed.json()] == [2]
        assert (await client.post(base, headers=CONTRIBUTOR, json=dict(body, extra=1))).status_code == 422
        accepted = await client.post(base, headers=CONTRIBUTOR, json=body)
        assert accepted.status_code == 200 and accepted.json()["state"] == "requested"
        assert accepted.json()["job_id"] == "job-exec-1"
        job_type, params = queue.enqueue.await_args.args
        assert job_type == "team_run_execution" and params == {"run_id": str(run_id), "repo_path": str(source_repo[0])}
        assert queue.enqueue.await_args.kwargs["idempotency_key"] == f"team-run-execution:{run_id}"
        assert (await client.post(base, headers=CONTRIBUTOR, json=dict(body, steps=[{"tool": "touch", "args": [
            "x.txt"]}]))).status_code == 409
        assert (await client.get(base, headers=READER)).json()["state"] == "requested"
        assert (await client.post(f"{base}/cancel", headers=READER)).status_code == 403
        cancelled = await client.post(f"{base}/cancel", headers=CONTRIBUTOR)
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
        missing = f"/team/projects/{a}/tasks/{task_id}/runs/{uuid.uuid4()}/execution"
        assert (await client.get(missing, headers=CONTRIBUTOR)).status_code in {403, 404}
        assert (await client.delete(grant_url, headers=ADMIN)).json()["active"] is False
