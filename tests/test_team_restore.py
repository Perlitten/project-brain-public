import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain.auth.principals import ResolvedPrincipal, hash_credential, resolve_credential
from brain.database.harness_models import AgentTask
from brain.database.models import ApiCredential
from brain.team import provisioning, restore_fence
from brain.team.authorization import (Permission, TeamConflict, TeamDenied, authorize, change_policy, change_role,
                                      team_state)
from brain.team.models import AgentRun, TeamAccessFence, TeamMember
from brain.team.store import create_task, list_tasks, request_run
from scripts.backup_runner import run_backup
from scripts.restore_database import restore_database
from tests.test_team_foundations import NOW, TABLES, caller, run_payload, task_payload
from tests.test_team_foundations import team_db as foundation_db

team_db = foundation_db
RESTORE_DATABASE = "brain_team_restore_test"


async def snapshot(factory):
    async with factory() as session:
        result = {}
        for table in TABLES:
            if table is TeamAccessFence.__table__:
                continue  # The restore adds its fence; every other row must match the snapshot.
            rows = (await session.execute(select(table).order_by(*table.primary_key.columns))).mappings().all()
            result[table.fullname] = json.loads(json.dumps([dict(row) for row in rows], default=str, sort_keys=True))
        return result


def run_caller(run, credential_id):
    return ResolvedPrincipal(run["principal_id"], "untrusted label", "run", credential_id=credential_id)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="disposable PostgreSQL restore requires opt-in")
async def test_team_backup_restore_preserves_authority_and_retry(team_db, tmp_path, monkeypatch):
    source, project_a, project_b = team_db
    url = make_url(os.environ["TEST_TEAM_POSTGRES_URL"])
    assert url.host == "127.0.0.1" and url.database == "brain_team_test" and url.username == "fixture"
    assert not url.query
    assert shutil.which("pg_dump") and shutil.which("psql"), "PostgreSQL recovery tools are required"
    for key in ("DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
                "POSTGRES_DB", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "BRAIN_STORAGE_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("scripts.restore_verification_drill.PROJECT_ROOT", tmp_path)

    def configuration(name):
        path = tmp_path / f"{name}.env"
        dsn = url.set(database=name).render_as_string(hide_password=False)
        path.write_text(f"DATABASE_URL={dsn}\nNEO4J_URI=bolt://127.0.0.1:1\n"
                        "NEO4J_USER=fixture\nNEO4J_PASSWORD=fixture_local_only\nBRAIN_STORAGE_MODE=full\n")
        return str(path)

    async with source() as session, session.begin():
        assert (await session.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user"))).scalar_one() is False
        task = await create_task(session, caller(2), project_a, task_payload())
        task_id = uuid.UUID(task["id"])
        root = await request_run(session, caller(2), project_a, task_id, run_payload(allowed=["read", "run.request"]))
        grant = await session.get(AgentRun, uuid.UUID(root["id"]))
        root_caller = run_caller(root, grant.credential_id)
        child = await request_run(session, root_caller, project_a, task_id, run_payload("child"))
        child_grant = await session.get(AgentRun, uuid.UUID(child["id"]))
        child_caller = run_caller(child, child_grant.credential_id)
        revoked = await request_run(session, caller(2), project_a, task_id, run_payload("revoked"))
        revoked_grant = await session.get(AgentRun, uuid.UUID(revoked["id"]))
        revoked_caller = run_caller(revoked, revoked_grant.credential_id)
        await session.execute(update(ApiCredential).where(ApiCredential.id == revoked_grant.credential_id)
                              .values(revoked_at=NOW))
        cancelled = await create_task(session, caller(2), project_a, task_payload("cancelled"))
        await session.execute(update(AgentTask).where(AgentTask.id == uuid.UUID(cancelled["id"]))
                              .values(cancellation_requested_at=NOW, status="cancelled"))
        other = await create_task(session, caller(4), project_b, dict(task_payload("other"), repository_id=2))
        session.add(AgentTask(id=uuid.uuid4(), title="legacy personal task", goal="must not be adopted",
                              repo_path="/synthetic/personal"))
        await session.execute(update(ApiCredential).where(ApiCredential.id == 3).values(revoked_at=NOW))
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 3).values(revoked_at=NOW))
        await change_role(session, caller(4), project_a, 2, "contributor", 0)
        await change_policy(session, caller(4), project_a, ["read", "task.create", "run.request"], 0)

    before = await snapshot(source)
    backup_dir = tmp_path / "backup"
    manifest = await asyncio.to_thread(run_backup, str(backup_dir), config_path=configuration(url.database),
                                       allow_missing=("neo4j",))
    assert manifest["status"] == "ok"
    assert {item["name"]: item["status"] for item in manifest["artifacts"]} == {"postgres": "ok", "neo4j": "skipped"}
    dump = backup_dir / "postgres_dump.sql"
    assert root["credential"] not in dump.read_text()
    assert child["credential"] not in dump.read_text()
    # Revoked after the snapshot: the dump still holds member 1 and their key as live.
    async with source() as session, session.begin():
        await provisioning.revoke_member(session, principal=await provisioning.resolve_principal(session, 1))
        await session.execute(update(ApiCredential).where(ApiCredential.id == 1).values(revoked_at=NOW))
    config = configuration(RESTORE_DATABASE)
    engine = create_async_engine(url.set(database=RESTORE_DATABASE), poolclass=NullPool)
    restored = async_sessionmaker(engine, expire_on_commit=False)
    try:
        assert await asyncio.to_thread(restore_database, backup_dir, config, RESTORE_DATABASE) == "pending_reconciliation"
        assert await snapshot(restored) == before
        async with restored() as session, session.begin():
            assert (await session.get(ApiCredential, 1)).revoked_at is None
            assert await restore_fence.access_fenced(session) and await team_state(session) == "team"
            for raw in ("fixture-1", "fixture-2", "fixture-4"):
                assert await resolve_credential(session, raw) is None
            with pytest.raises(TeamDenied, match="fenced"):
                await authorize(session, child_caller, project_a, Permission.READ, task_id=task_id)
            reviewed = await restore_fence.review(session)
            assert {m["principal_id"]: m["revoked"] for m in reviewed.state["members"]}[1] is False
            # The operator replays the post-snapshot revocation, then attests the corrected state.
            await provisioning.revoke_member(session, principal=await provisioning.resolve_principal(session, 1))
            with pytest.raises(TeamConflict, match="attested digest"):
                await restore_fence.reconcile(session, attest=reviewed.digest)
        async with restored() as session, session.begin():
            reconciled = await restore_fence.reconcile(session, attest=(await restore_fence.review(session)).digest)
            assert reconciled["credentials"] == "rotated" and reconciled["revoked"]["credentials"] > 0
        async with restored() as session, session.begin():
            assert not await restore_fence.access_fenced(session)
            for raw in ("fixture-1", "fixture-2", "fixture-4"):
                assert await resolve_credential(session, raw) is None
            with pytest.raises(TeamDenied):
                await authorize(session, child_caller, project_a, Permission.READ, task_id=task_id)
            rekeyed = {}
            for name in ("human-1", "human-2"):
                raw, _ = await provisioning.create_human_principal(session, name)
                rekeyed[name] = await resolve_credential(session, raw)
            with pytest.raises(TeamDenied):
                await authorize(session, rekeyed["human-1"], project_a, Permission.READ)
            await authorize(session, rekeyed["human-2"], project_a, Permission.READ)

        # A snapshot known to be current (rollback with the API stopped) may keep its credentials.
        assert await asyncio.to_thread(restore_database, backup_dir, config, RESTORE_DATABASE) == "pending_reconciliation"
        assert await snapshot(restored) == before
        async with restored() as session, session.begin():
            kept = await restore_fence.reconcile(session, attest=(await restore_fence.review(session)).digest,
                                                 keep_credentials=True)
            assert kept["credentials"] == "kept" and set(kept["revoked"].values()) == {0}
        assert await snapshot(restored) == before
        async with restored() as session, session.begin():
            assert await create_task(session, caller(2), project_a, task_payload()) == task
            retry = await request_run(session, caller(2), project_a, task_id, run_payload(allowed=["read", "run.request"]))
            assert retry["id"] == root["id"] and retry["credential"] is None
            for denied, project, permission, bound_task in (
                (caller(1), project_a, Permission.CREATE_TASK, None),
                (caller(2), project_b, Permission.READ, None),
                (caller(3), project_a, Permission.READ, None),
                (revoked_caller, project_a, Permission.READ, task_id),
                (child_caller, project_a, Permission.REQUEST_RUN, task_id),
                (root_caller, project_a, Permission.EXECUTE, task_id),
                (caller(2), project_a, Permission.REQUEST_RUN, uuid.UUID(cancelled["id"])),
            ):
                with pytest.raises(TeamDenied):
                    await authorize(session, denied, project, permission, task_id=bound_task)
            await authorize(session, child_caller, project_a, Permission.READ, task_id=task_id)
            with pytest.raises(TeamConflict):
                await change_role(session, caller(4), project_a, 2, "reader", 0)
            with pytest.raises(TeamConflict):
                await change_policy(session, caller(4), project_a, ["read"], 0)
            assert {item["id"] for item in await list_tasks(session, caller(1), project_a)} == {task["id"], cancelled["id"]}
            assert [item["id"] for item in await list_tasks(session, caller(4), project_b)] == [other["id"]]
            assert [item["id"] for item in await list_tasks(session, child_caller, project_a)] == [task["id"]]
        assert await snapshot(restored) == before

        with pytest.raises(ValueError, match="confirm-database"):
            await asyncio.to_thread(restore_database, backup_dir, config, url.database)
        assert await snapshot(restored) == before
        with dump.open("a") as stream:
            stream.write("\n-- synthetic checksum corruption\n")
        with pytest.raises(ValueError, match="verification failed"):
            await asyncio.to_thread(restore_database, backup_dir, config, RESTORE_DATABASE)
        assert await snapshot(restored) == before

        max_identity = max(row["id"] for row in before["principals"])
        max_credential = max(row["id"] for row in before["api_credentials"])
        async with restored() as session, session.begin():
            new_task = await create_task(session, caller(2), project_a, task_payload("after-restore"))
            new_run = await request_run(session, caller(2), project_a, uuid.UUID(new_task["id"]), run_payload("after-restore"))
            new_grant = await session.get(AgentRun, uuid.UUID(new_run["id"]))
            assert new_grant.principal_id > max_identity and new_grant.credential_id > max_credential
            assert new_run["credential"] and new_task["author_id"] == 2 and not new_run["execution_available"]
            assert (await session.get(ApiCredential, new_grant.credential_id)).key_hash == hash_credential(new_run["credential"])
        print("TEAM_RESTORE_RESULT=" + json.dumps({
            "tables": len(before), "rows": sum(map(len, before.values())),
            "snapshot_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
            "role_superuser": False, "postgres_restore": "passed", "neo4j_restore": "not tested",
            "restore_fence": "post-snapshot revocation denied until reconcile; rotation and keep paths",
            "authority_denials": 7, "stale_version_conflicts": 2,
            "task_run_retry": "no duplicate or raw credential replay", "target_integrity_checks": "passed",
            "new_request_sequences": "passed", "execution_available": False,
        }, sort_keys=True))
    finally:
        await engine.dispose()
        admin = url.set(drivername="postgresql", database="postgres", password=None).render_as_string(hide_password=False)
        await asyncio.to_thread(subprocess.run, [shutil.which("psql"), "--dbname", admin, "-X", "-v", "ON_ERROR_STOP=1",
                                                "-c", f'DROP DATABASE IF EXISTS "{RESTORE_DATABASE}"'],
                                check=True, capture_output=True, timeout=60,
                                env={**os.environ, "PGPASSWORD": url.password or ""})
