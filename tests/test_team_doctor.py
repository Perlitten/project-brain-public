"""``brain team doctor`` / ``GET /team/doctor``: each read-only check on its pass and failure path.

The fixture is SQLite only, never the shared PostgreSQL test database: it creates
``repository_origin_state`` (a child of ``repositories``), which the other team
fixtures do not drop. Foreign keys stay off so execution rows need no task chain.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from apps.api import auth, team_access
from apps.api.routers import team_doctor
from apps.cli.commands import team as team_commands
from brain.auth.principals import hash_credential
from brain.config.settings import settings
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import ApiCredential, Base, Principal, Repository, RepositoryOriginState
from brain.team import doctor
from brain.team.authorization import team_state
from brain.team.models import (AgentRun, AgentRunExecution, Team, TeamAccessFence, TeamIdentity, TeamInvitation,
                               TeamMember, TeamProject, TeamTaskBinding)

pytestmark = pytest.mark.team_state

TABLES = [model.__table__ for model in (Principal, ApiCredential, Repository, RepositoryOriginState, AgentTask,
          AgentTaskEvent, Team, TeamAccessFence, TeamMember, TeamProject, TeamTaskBinding, AgentRun,
          AgentRunExecution, TeamInvitation, TeamIdentity)]
NOW = datetime.now(timezone.utc)
SHA = "a" * 40
OTHER_SHA = "b" * 40
ADMIN, MEMBER = {"X-API-Key": "fixture-1"}, {"X-API-Key": "fixture-2"}


@pytest_asyncio.fixture
async def factory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "full")
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)  # team state follows live membership
    for name, value in (("PROJECT_BRAIN_API_KEY", None), ("BRAIN_OIDC_ENABLED", False),
                        ("BRAIN_OIDC_ISSUER", None), ("BRAIN_OIDC_CLIENT_ID", None),
                        ("BRAIN_OIDC_REDIRECT_URI", None), ("BRAIN_OIDC_REQUIRE_FOR_INVITATIONS", False),
                        ("BRAIN_WEB_URL", None), ("BRAIN_EXECUTION_ENABLED", False), ("BRAIN_EXECUTION_ROOT", None),
                        ("BRAIN_EXECUTION_TOOLS", "{}"), ("BRAIN_EXECUTION_SECRETS", "{}"),
                        ("BRAIN_EXECUTION_EGRESS", ""), ("BRAIN_ORIGIN_REMOTES", ""),
                        ("BRAIN_ORIGIN_POLL_INTERVAL_S", 300), ("BRAIN_BACKUP_DIR", ""),
                        ("BRAIN_BACKUP_MAX_AGE_HOURS", 48)):
        monkeypatch.setattr(settings, name, value)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/doctor.db",
                                 execution_options={"schema_translate_map": {"harness": None}})
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    made = async_sessionmaker(engine, expire_on_commit=False)
    async with made() as session, session.begin():
        session.add(Team(id=1, name="synthetic team"))
        session.add_all([Principal(id=i, name=f"human-{i}", kind="human") for i in (1, 2)])
        await session.flush()
        for i in (1, 2):
            session.add(ApiCredential(id=i, principal_id=i, key_hash=hash_credential(f"fixture-{i}"), scopes=["*"]))
            session.add(TeamMember(principal_id=i, team_id=1, verification_issuer="fixture",
                                   verification_subject=str(i), verified_at=NOW, is_admin=i == 1))
        session.add(Repository(id=1, name="A", path="/synthetic/A", last_indexed_commit=SHA))
    yield made
    await engine.dispose()


async def run(made, check, *args):
    async with made() as session:
        return await check(session, *args)


async def to_solo(made):
    async with made() as session, session.begin():
        await session.execute(delete(TeamMember))


def by_name(checks):
    return {check.name: check for check in checks}


# --- team state, administrators, shared key, member credentials, revocations -----------------------------------


@pytest.mark.asyncio
async def test_team_state_reports_solo_and_team(factory):
    assert (await run(factory, doctor.check_team_state)).message.startswith("team: 2 live member(s)")
    await to_solo(factory)
    check = await run(factory, doctor.check_team_state)
    assert check.status == "pass" and check.message.startswith("solo")


@pytest.mark.asyncio
async def test_administrators_pass_with_a_live_admin_key_and_fail_without_one(factory):
    assert (await run(factory, doctor.check_administrators, "team", NOW)).status == "pass"
    async with factory() as session, session.begin():
        await session.execute(update(ApiCredential).where(ApiCredential.id == 1).values(scopes=["core:read"]))
    failed = await run(factory, doctor.check_administrators, "team", NOW)
    assert failed.status == "fail" and "--admin --mint-key" in failed.hint
    assert (await run(factory, doctor.check_administrators, "solo", NOW)).status == "pass"


@pytest.mark.asyncio
async def test_shared_key_reports_refusal_in_team_state(monkeypatch):
    assert "No shared server key" in (await doctor.check_shared_key("team")).message
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-shared")
    team = await doctor.check_shared_key("team")
    assert team.status == "pass" and "refused" in team.message
    solo = await doctor.check_shared_key("solo")
    assert solo.status == "pass" and "accepted" in solo.message


@pytest.mark.asyncio
async def test_members_without_credentials_warn(factory, monkeypatch):
    assert (await run(factory, doctor.check_member_credentials, "team", NOW)).status == "pass"
    async with factory() as session, session.begin():
        await session.execute(update(ApiCredential).where(ApiCredential.id == 2).values(revoked_at=NOW))
    warned = await run(factory, doctor.check_member_credentials, "team", NOW)
    assert warned.status == "warn" and "human-2" in warned.message and "--mint-key" in warned.hint
    # With SSO on, a bound identity is a way in.
    monkeypatch.setattr(settings, "BRAIN_OIDC_ENABLED", True)
    async with factory() as session, session.begin():
        session.add(TeamIdentity(team_id=1, principal_id=2, issuer="https://idp.example.invalid", subject="s-2",
                                  bound_by="operator"))
    assert (await run(factory, doctor.check_member_credentials, "team", NOW)).status == "pass"
    assert (await run(factory, doctor.check_member_credentials, "solo", NOW)).status == "pass"


@pytest.mark.asyncio
async def test_revoked_member_with_live_access_fails(factory):
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 2).values(revoked_at=NOW))
    failed = await run(factory, doctor.check_revocations, NOW)
    assert failed.status == "fail" and "human-2" in failed.message and "member revoke" in failed.hint
    async with factory() as session, session.begin():
        await session.execute(update(ApiCredential).where(ApiCredential.id == 2).values(revoked_at=NOW))
    assert (await run(factory, doctor.check_revocations, NOW)).status == "pass"


# --- restore fence, invitations, execution leases ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_fence_fails_until_cleared(factory):
    assert (await run(factory, doctor.check_restore_fence)).status == "pass"
    async with factory() as session, session.begin():
        session.add(TeamAccessFence(id=1, reason="restored from a team snapshot"))
    failed = await run(factory, doctor.check_restore_fence)
    assert failed.status == "fail" and "brain team reconcile" in failed.hint
    async with factory() as session, session.begin():
        await session.execute(update(TeamAccessFence).values(cleared_at=NOW, cleared_by="operator"))
    assert (await run(factory, doctor.check_restore_fence)).status == "pass"


@pytest.mark.asyncio
async def test_restore_fence_without_table_is_open(factory):
    async with factory() as session:
        engine = session.bind
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: TeamAccessFence.__table__.drop(sync))
    check = await run(factory, doctor.check_restore_fence)
    assert check.status == "pass" and "never restored" in check.message


def invitation(expires_at, **changes):
    return TeamInvitation(team_id=1, token_hash=uuid.uuid4().hex, principal_name="invitee", project_ids=[],
                          role="reader", expires_at=expires_at, invited_by="human-1", **changes)


@pytest.mark.asyncio
async def test_expired_pending_invitations_warn(factory):
    async with factory() as session, session.begin():
        session.add(invitation(NOW + timedelta(days=1)))
        session.add(invitation(NOW - timedelta(days=1), revoked_at=NOW))  # already handled
    passed = await run(factory, doctor.check_invitations, NOW)
    assert passed.status == "pass" and passed.message.startswith("1 pending")
    stale = invitation(NOW - timedelta(hours=1))
    async with factory() as session, session.begin():
        session.add(stale)
    warned = await run(factory, doctor.check_invitations, NOW)
    assert warned.status == "warn" and str(stale.id) in warned.message and "invite revoke" in warned.hint


async def add_execution(made, n, **lease):
    run_id = uuid.uuid4()
    async with made() as session, session.begin():
        session.add(AgentRun(id=run_id, task_id=uuid.uuid4(), principal_id=100 + n, credential_id=100 + n,
                             initiator_id=1, initiator_credential_id=1, permissions=["read"],
                             executor_capabilities=[], branch="main", base_sha=SHA, request_key=f"k{n}",
                             request_digest="d" * 64, requested_by=1))
        session.add(AgentRunExecution(run_id=run_id, request_key=f"k{n}", request_digest="d" * 64, requested_by=1,
                                      requested_credential_id=1, steps=[{"tool": "hello"}], tools=["hello"],
                                      secret_refs=[], egress=[], budget={}, backend="subprocess", **lease))
    return run_id


@pytest.mark.asyncio
async def test_execution_leases_pass_warn_and_fail(factory):
    await add_execution(factory, 1, started_at=NOW, lease_owner="w1", lease_expires_at=NOW + timedelta(minutes=1))
    await add_execution(factory, 2, created_at=NOW - timedelta(minutes=5))  # just requested
    assert (await run(factory, doctor.check_execution_leases, NOW)).status == "pass"
    unclaimed = await add_execution(factory, 3, created_at=NOW - timedelta(hours=2))
    warned = await run(factory, doctor.check_execution_leases, NOW)
    assert warned.status == "warn" and str(unclaimed) in warned.message and "executor" in warned.hint
    orphan = await add_execution(factory, 4, started_at=NOW - timedelta(minutes=10), lease_owner="w2",
                                 lease_expires_at=NOW - timedelta(minutes=1))
    failed = await run(factory, doctor.check_execution_leases, NOW)
    assert failed.status == "fail" and str(orphan) in failed.message
    assert "brain team execution recover" in failed.hint


# --- source freshness -------------------------------------------------------------------------------------------


async def set_origin(made, **values):
    async with made() as session, session.begin():
        row = await session.get(RepositoryOriginState, 1) or RepositoryOriginState(repository_id=1)
        for name, value in values.items():
            setattr(row, name, value)
        session.add(row)


@pytest.mark.asyncio
async def test_source_freshness_paths(factory):
    (unpolled,) = await run(factory, doctor.check_sources, NOW)
    assert unpolled.name == "source_freshness:A" and unpolled.status == "unknown"
    await set_origin(factory, polled_at=NOW, poll_status="ok", remote_head=SHA, tracked_branch="main")
    (fresh,) = await run(factory, doctor.check_sources, NOW)
    assert fresh.status == "pass" and SHA[:12] in fresh.message
    await set_origin(factory, remote_head=OTHER_SHA, commits_behind=3)
    (behind,) = await run(factory, doctor.check_sources, NOW)
    assert behind.status == "warn" and "3 commit(s) behind" in behind.message
    await set_origin(factory, poll_status="error", poll_error="fatal: repository not found")
    (error,) = await run(factory, doctor.check_sources, NOW)
    assert error.status == "warn" and "repository not found" in error.message
    await set_origin(factory, poll_status="unconfigured", poll_error=None)
    (unconfigured,) = await run(factory, doctor.check_sources, NOW)
    assert unconfigured.status == "warn" and "BRAIN_ORIGIN_REMOTES" in unconfigured.hint
    await set_origin(factory, poll_status="ok", remote_head=SHA, polled_at=NOW - timedelta(days=1))
    (stale,) = await run(factory, doctor.check_sources, NOW)
    assert stale.status == "warn" and "stale" in stale.message


@pytest.mark.asyncio
async def test_source_freshness_without_repositories_passes(factory):
    async with factory() as session, session.begin():
        await session.execute(delete(Repository))
    (check,) = await run(factory, doctor.check_sources, NOW)
    assert check.status == "pass" and check.name == "source_freshness"


# --- settings-only checks ---------------------------------------------------------------------------------------


def test_oidc_settings_consistency(factory, monkeypatch):
    assert doctor.check_oidc().status == "pass"
    monkeypatch.setattr(settings, "BRAIN_OIDC_REQUIRE_FOR_INVITATIONS", True)
    assert doctor.check_oidc().status == "warn"
    monkeypatch.setattr(settings, "BRAIN_OIDC_REQUIRE_FOR_INVITATIONS", False)
    monkeypatch.setattr(settings, "BRAIN_OIDC_ISSUER", "https://idp.example.invalid")
    assert doctor.check_oidc().status == "warn"  # configured but not enabled
    monkeypatch.setattr(settings, "BRAIN_OIDC_ENABLED", True)
    failed = doctor.check_oidc()
    assert failed.status == "fail" and "BRAIN_OIDC_CLIENT_ID" in failed.message
    monkeypatch.setattr(settings, "BRAIN_OIDC_CLIENT_ID", "brain")
    monkeypatch.setattr(settings, "BRAIN_WEB_URL", "https://brain.example.invalid")
    monkeypatch.setattr(settings, "BRAIN_OIDC_REDIRECT_URI", "https://other.example.invalid/auth/oidc/callback")
    assert doctor.check_oidc().status == "warn"
    monkeypatch.setattr(settings, "BRAIN_OIDC_REDIRECT_URI", "https://brain.example.invalid/auth/oidc/callback")
    assert doctor.check_oidc().status == "pass"


def test_execution_config_paths(factory, monkeypatch, tmp_path):
    assert doctor.check_execution_config().message == "Execution is disabled on this process"
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", "not json")
    assert doctor.check_execution_config().status == "fail"
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", json.dumps({"py": {"argv": [sys.executable, "-V"]}}))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENABLED", True)
    assert "BRAIN_EXECUTION_ROOT is not set" in doctor.check_execution_config().message
    root = tmp_path / "executions"
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ROOT", str(root))
    assert "does not exist" in doctor.check_execution_config().message
    root.mkdir()
    # The subprocess backend works but is no boundary between steps and the server (execution_config_health).
    subprocess_backend = doctor.check_execution_config()
    assert subprocess_backend.status == "warn" and "Subprocess backend" in subprocess_backend.message
    assert "BRAIN_EXECUTION_BACKEND=container" in subprocess_backend.hint
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS",
                        json.dumps({"ghost": {"argv": ["definitely-not-installed-tool-7f3a"]}}))
    missing_tool = doctor.check_execution_config()
    assert missing_tool.status == "fail" and "'ghost'" in missing_tool.message
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", json.dumps({"py": {"argv": [sys.executable, "-V"]}}))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_SECRETS", json.dumps({"token": "FIXTURE_DOCTOR_TOKEN"}))
    monkeypatch.delenv("FIXTURE_DOCTOR_TOKEN", raising=False)
    missing_secret = doctor.check_execution_config()
    assert missing_secret.status == "fail" and "$FIXTURE_DOCTOR_TOKEN" in missing_secret.message
    monkeypatch.setenv("FIXTURE_DOCTOR_TOKEN", "fixture-value")
    assert doctor.check_execution_config().status == "warn"
    assert "fixture-value" not in doctor.check_execution_config().message
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "api.example.invalid")
    egress = doctor.check_execution_config()
    assert egress.status == "warn" and "cannot enforce" in egress.message
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "")
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", "{}")
    assert doctor.check_execution_config().status == "warn"


def test_execution_config_container_backend(factory, monkeypatch, tmp_path):
    """The container backend is covered through execution_config_health: environments, egress proxy, docker CLI."""
    root = tmp_path / "executions"
    root.mkdir()
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ROOT", str(root))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "container")
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_DOCKER", sys.executable)
    # Tools run inside the image, so a tool absent from this host is fine with the container backend.
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS",
                        json.dumps({"pytest": {"argv": ["definitely-not-installed-tool-7f3a"]}}))
    no_environments = doctor.check_execution_config()
    assert no_environments.status == "fail" and "at least one execution environment" in no_environments.message
    assert "BRAIN_EXECUTION_ENVIRONMENTS" in no_environments.hint
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENVIRONMENTS",
                        json.dumps({"py": {"image": "python:3.12-slim", "tools": ["pytest"]}}))
    healthy = doctor.check_execution_config()
    assert healthy.status == "pass", healthy.message
    assert "container backend, environments py" in healthy.message and "egress denied" in healthy.message
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "pypi.org")
    unenforced = doctor.check_execution_config()
    assert unenforced.status == "warn" and "BRAIN_EXECUTION_EGRESS_PROXY_IMAGE" in unenforced.hint
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS_PROXY_IMAGE", "brain-api:latest")
    enforced = doctor.check_execution_config()
    assert enforced.status == "pass" and "enforced by the proxy" in enforced.message
    # An LLM API host in the egress allowlist must belong to a configured LLM provider.
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "pypi.org,api.openai.com")
    unowned = doctor.check_execution_config()
    assert unowned.status == "warn" and "api.openai.com" in unowned.message
    assert "BRAIN_EXECUTION_LLM_PROVIDERS" in unowned.hint
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LLM_PROVIDERS",
                        json.dumps({"openai": {"hosts": ["api.openai.com"]}}))
    owned = doctor.check_execution_config()
    assert owned.status == "pass" and "1 LLM provider(s)" in owned.message
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "pypi.org")
    assert doctor.check_execution_config().status == "warn"  # the provider's host is not in the allowlist
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LLM_PROVIDERS", "not json")
    assert doctor.check_execution_config().status == "fail"
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LLM_PROVIDERS", "{}")
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_DOCKER", "definitely-not-installed-docker-7f3a")
    no_docker = doctor.check_execution_config()
    assert no_docker.status == "fail" and "docker CLI" in no_docker.message


def test_origin_settings_paths(factory, monkeypatch):
    assert doctor.check_origin_settings().status == "pass"
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES", "{not json")
    assert doctor.check_origin_settings().status == "fail"
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES", '["/app"]')
    assert doctor.check_origin_settings().status == "fail"
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES", json.dumps({"/app": {"branch": "main"}}))
    assert doctor.check_origin_settings().status == "warn"
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES", json.dumps({"/app": "https://git.example.invalid/r.git"}))
    assert doctor.check_origin_settings().message == "BRAIN_ORIGIN_REMOTES names 1 source(s)"
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_POLL_INTERVAL_S", 0)
    assert doctor.check_origin_settings().status == "warn"


def test_backup_status_paths(factory, monkeypatch, tmp_path):
    assert doctor.check_backup(NOW).status == "unknown"
    backups = tmp_path / "backups"
    monkeypatch.setattr(settings, "BRAIN_BACKUP_DIR", str(backups))
    assert doctor.check_backup(NOW).status == "unknown"  # not mounted here
    backups.mkdir()
    assert doctor.check_backup(NOW).status == "warn"
    # deploy/server_up.sh layout: <dir>/<UTC stamp>/brain_db.sql.gz, judged by modification time.
    dump = backups / "20261017T020000Z" / "brain_db.sql.gz"
    dump.parent.mkdir()
    dump.write_bytes(b"\x1f\x8b synthetic")  # what a failed pg_dump | gzip leaves behind
    empty = doctor.check_backup(NOW)
    assert empty.status == "fail" and "'empty'" in empty.message
    dump.write_bytes(b"\x1f\x8b" + b"synthetic dump " * 100)
    assert doctor.check_backup(NOW).status == "pass"
    old = (NOW - timedelta(days=5)).timestamp()
    os.utime(dump, (old, old))
    assert doctor.check_backup(NOW).status == "warn"
    # scripts/backup_runner.py layout: a manifest with its own timestamp and status.
    manifest = backups / "run-1" / "backup_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"backup_version": 1, "timestamp": (NOW - timedelta(hours=1)).isoformat(),
                                    "status": "failed"}))
    failed = doctor.check_backup(NOW)
    assert failed.status == "fail" and "'failed'" in failed.message
    manifest.write_text(json.dumps({"backup_version": 1, "timestamp": (NOW - timedelta(hours=2)).isoformat(),
                                    "status": "ok"}))
    passed = doctor.check_backup(NOW)
    assert passed.status == "pass" and "backup_manifest.json" in passed.message


# --- whole report, CLI and API ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_checks_reports_every_check_and_survives_a_missing_table(factory):
    async with factory() as session:
        checks = by_name(await doctor.run_checks(session, NOW))
    assert {"team_state", "oidc", "shared_key", "administrators", "member_credentials", "revocations",
            "restore_fence", "invitations", "execution_leases", "execution_config", "origin_settings",
            "source_freshness:A", "backup"} == set(checks)
    report = doctor.summary(list(checks.values()))
    assert report["status"] == "unknown" and report["counts"] == {"pass": 11, "warn": 0, "fail": 0, "unknown": 2}
    async with factory() as session:
        engine = session.bind
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: RepositoryOriginState.__table__.drop(sync))
    async with factory() as session:
        checks = by_name(await doctor.run_checks(session, NOW))
    assert checks["source_freshness"].status == "unknown" and "Could not read" in checks["source_freshness"].message
    assert checks["execution_leases"].status == "pass"  # the failed probe rolled back only its savepoint


@pytest.mark.asyncio
async def test_lite_storage_runs_settings_checks_only(factory, monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "lite")
    async with factory() as session:
        names = [check.name for check in await doctor.run_checks(session, NOW)]
    assert names == ["team_state", "oidc", "shared_key", "execution_config", "origin_settings", "backup"]


def test_cli_prints_hints_and_exits_nonzero_on_failure(factory, monkeypatch):
    monkeypatch.setattr(team_commands, "async_session_factory", factory)
    runner = CliRunner()
    ok = runner.invoke(team_commands.team_cli, ["doctor"])
    assert ok.exit_code == 0, ok.output
    assert "[PASS] team_state: team: 2 live member(s)" in ok.output and "    fix: Set BRAIN_BACKUP_DIR" in ok.output
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", "not json")
    failed = runner.invoke(team_commands.team_cli, ["doctor", "--json"])
    assert failed.exit_code == 1
    report = json.loads(failed.output)
    assert report["status"] == "fail"
    assert {"name", "status", "message", "hint"} == set(report["checks"][0])


def doctor_app(made, monkeypatch):
    async def live_team_state():
        async with made() as session:
            return await team_state(session)

    for module in (auth, team_access, team_doctor):
        monkeypatch.setattr(module, "async_session_factory", made)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    app = FastAPI()
    app.include_router(team_doctor.router)
    return app


@pytest.mark.asyncio
async def test_api_is_admin_only_in_team_state_and_open_to_the_owner_in_solo(factory, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-shared")
    app = doctor_app(factory, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        report = (await client.get("/team/doctor", headers=ADMIN)).json()
        assert report["status"] == "unknown" and "refused" in by_name(
            doctor.Check(**item) for item in report["checks"])["shared_key"].message
        denied = await client.get("/team/doctor", headers=MEMBER)
        assert denied.status_code == 403 and "Team administrator required" in denied.json()["detail"]
        shared = await client.get("/team/doctor", headers={"X-API-Key": "fixture-shared"})
        assert shared.status_code in (401, 403) and "Named credential required" in shared.json()["detail"]
        assert (await client.get("/team/doctor")).status_code == 401
        await to_solo(factory)
        solo = await client.get("/team/doctor", headers={"X-API-Key": "fixture-shared"})
        assert solo.status_code == 200 and solo.json()["checks"][0]["message"].startswith("solo")


# --- one-team packaging -----------------------------------------------------------------------------------------


def test_team_compose_overlay_and_env_template_parse():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "deploy", "team", "docker-compose.team.yml"), encoding="utf-8") as handle:
        overlay = yaml.safe_load(handle)
    services = overlay["services"]
    assert {"caddy", "web", "worker-executor", "api"} <= set(services)
    assert services["worker-executor"]["profiles"] == ["executor"]
    assert set(services["caddy"]["ports"]) == {"80:80", "443:443"}
    executor_env = services["worker-executor"]["environment"]
    assert executor_env["BRAIN_EXECUTION_ENABLED"] == "true"
    with open(os.path.join(root, "deploy", "team", ".env.team.example"), encoding="utf-8") as handle:
        template = handle.read()
    names = {line.split("=", 1)[0] for line in template.splitlines() if "=" in line and not line.startswith("#")}
    for required in ("BRAIN_WEB_DOMAIN", "BRAIN_API_DOMAIN", "BRAIN_ORIGIN_REMOTES", "BRAIN_OIDC_ENABLED",
                     "BRAIN_OIDC_ISSUER", "BRAIN_EXECUTION_ENABLED", "BRAIN_EXECUTION_TOOLS",
                     "BRAIN_BACKUP_DIR", "POSTGRES_PASSWORD", "PROJECT_BRAIN_API_KEY"):
        assert required in names, required
    # A template, not a secret store: every secret is an obvious placeholder.
    for line in template.splitlines():
        if line.split("=", 1)[0].endswith(("PASSWORD", "API_KEY", "SECRET")) and "=" in line:
            assert line.split("=", 1)[1] in ("", "change-me"), line
