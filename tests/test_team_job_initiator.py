"""Queued jobs carry their initiator, and the worker re-authorizes it before every attempt
(ADR docs/adr/team-collaboration.md, "Revocation, concurrency and approval")."""
import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from apps.api.routers import core_retrieval, jobs, scheduler
from apps.api.schemas import DiffReviewRequest, GitMergeWebhook, ReindexJobRequest
from brain.config.settings import settings
from brain.database.models import ApiCredential
from brain.team.authorization import Permission
from brain.team.models import ProjectMember, TeamMember
from brain.workers.initiator import LEGACY_REFUSAL, JobInitiator, current_initiator
from brain.workers.queue import JobQueue
from brain.workers.runtime import current_job
from brain.workers.worker import process_one
from tests.test_team_foundations import caller, team_db as team_fixture
from tests.test_worker_queue import _FakeRedis

team_db = team_fixture
NOW = datetime.now(timezone.utc)
CONTRIBUTOR, READER = 2, 1  # project A roles in the team fixture; repository 1 is /synthetic/A


def worker_against(factory, monkeypatch, outcome=None):
    """A DB-less queue whose worker re-authorizes against the team database; returns the execute mock."""
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)  # team state comes from the members table
    monkeypatch.setattr("brain.database.session.async_session_factory", factory)
    monkeypatch.setattr("brain.workers.worker.try_claim_db_lease", AsyncMock(return_value=True))
    monkeypatch.setattr("brain.workers.worker.release_db_lease_open", AsyncMock())
    monkeypatch.setattr("brain.workers.worker.renew_db_lease_open", AsyncMock(return_value=True))
    execute = AsyncMock(side_effect=outcome or (lambda *a, **k: {"status": "completed"}))
    monkeypatch.setattr("brain.workers.worker.execute_job", execute)
    return execute


async def run_one(queue, job_id):
    assert await process_one(queue) is True
    return await queue.get_job(job_id)


@pytest.mark.asyncio
async def test_enqueue_records_the_initiator_and_legacy_jobs_have_none():
    queue = JobQueue(_FakeRedis())
    attributed = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"},
                                     initiator=JobInitiator.for_principal(caller(CONTRIBUTOR), Permission.REQUEST_RUN))
    job = await queue.get_job(attributed)
    assert {key: job[key] for key in job if key.startswith("initiator_")} == {
        "initiator_origin": "principal", "initiator_principal_id": "2", "initiator_credential_id": "2",
        "initiator_permission": "run.request", "initiator_source": ""}
    assert JobInitiator.from_job(job) == JobInitiator("principal", 2, 2, "run.request")

    system = await queue.get_job(await queue.enqueue("health_check", initiator=JobInitiator.system("scheduler")))
    assert JobInitiator.from_job(system) == JobInitiator("system", source="scheduler")

    legacy = await queue.get_job(await queue.enqueue("health_check"))
    assert not any(key.startswith("initiator_") for key in legacy)
    assert JobInitiator.from_job(legacy) is None


@pytest.mark.asyncio
async def test_api_call_sites_pass_the_callers_principal(monkeypatch):
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="job-1")
    monkeypatch.setattr(jobs, "_queue", lambda *_: queue)
    await jobs.enqueue_reindex(ReindexJobRequest(repo_path="/synthetic/A"), principal=caller(CONTRIBUTOR))
    await jobs.enqueue_health_check(None, principal=caller(CONTRIBUTOR))
    for call in queue.enqueue.await_args_list:
        assert call.kwargs["initiator"] == JobInitiator("principal", 2, 2, "run.request")

    monkeypatch.setattr("brain.workers.queue.queue_for_job", lambda *a, **k: queue)
    monkeypatch.setattr(core_retrieval, "async_session_factory", contextlib.nullcontext)
    monkeypatch.setattr(core_retrieval, "find_repository_id", AsyncMock(return_value=1))
    await core_retrieval.review_git_diff(DiffReviewRequest(repo_path="/synthetic/A", base="main", head="HEAD"),
                                         principal=caller(READER))
    # Diff review is admitted on read, so execution re-checks read, not run.request.
    assert queue.enqueue.await_args.kwargs["initiator"] == JobInitiator("principal", 1, 1, "read")
    # The result is later authorized on the indexed repository id, not the path.
    assert queue.enqueue.await_args.args[1]["repository_id"] == 1


@pytest.mark.asyncio
async def test_git_webhook_attributes_keyed_callers_and_keeps_the_signed_path_a_system_job(monkeypatch):
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="job-1")
    monkeypatch.setattr(scheduler, "queue_for_job", lambda *a, **k: queue)
    monkeypatch.setattr(scheduler, "repository_authority", AsyncMock())
    monkeypatch.setattr(scheduler, "_api_key_principal", AsyncMock(return_value=caller(CONTRIBUTOR)))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_REF", "refs/heads/main")
    body = GitMergeWebhook(ref="refs/heads/main", sha="abc", repo_path="/synthetic/A")
    request = SimpleNamespace(body=AsyncMock(return_value=b"{}"), headers={})

    for signed, expected, key in (
        (False, JobInitiator("principal", 2, 2, "run.request"), "git-reindex:principal:2:2:abc"),
        (True, JobInitiator.system("git_webhook"), "git-reindex:signed:abc"),
    ):
        monkeypatch.setattr(scheduler, "_github_signature_valid", lambda *_, ok=signed: ok)
        await scheduler.git_merge_webhook(request, body)
        assert queue.enqueue.await_args.kwargs["initiator"] == expected
        assert queue.enqueue.await_args.kwargs["idempotency_key"] == key


@pytest.mark.asyncio
async def test_solo_state_runs_principal_legacy_and_unscoped_jobs_unchanged(monkeypatch):
    """Without the team_state marker the probe is the solo stub: nothing is re-authorized."""
    monkeypatch.setattr("brain.workers.worker.try_claim_db_lease", AsyncMock(return_value=True))
    monkeypatch.setattr("brain.workers.worker.release_db_lease_open", AsyncMock())
    monkeypatch.setattr("brain.workers.worker.renew_db_lease_open", AsyncMock(return_value=True))
    execute = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr("brain.workers.worker.execute_job", execute)
    authorize = AsyncMock()
    monkeypatch.setattr("brain.workers.initiator.authorize_repository", authorize)
    queue = JobQueue(_FakeRedis())
    revoked_shape = JobInitiator("principal", 0, None, "run.request")  # shared legacy key: no named credential
    for initiator in (revoked_shape, None):
        job_id = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"}, initiator=initiator)
        assert (await run_one(queue, job_id))["status"] == "completed"
    assert execute.await_count == 2
    authorize.assert_not_awaited()


@pytest.mark.team_state
@pytest.mark.asyncio
async def test_live_member_runs_and_revoked_members_queued_job_is_refused(team_db, monkeypatch):
    factory, project_a, _ = team_db
    execute = worker_against(factory, monkeypatch)
    queue = JobQueue(_FakeRedis())
    initiator = JobInitiator.for_principal(caller(CONTRIBUTOR), Permission.REQUEST_RUN)

    live = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"}, initiator=initiator)
    assert (await run_one(queue, live))["status"] == "completed"

    queued = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"}, initiator=initiator)
    async with factory() as session, session.begin():
        await session.execute(update(ProjectMember).where(ProjectMember.project_id == project_a,
                                                          ProjectMember.principal_id == CONTRIBUTOR)
                              .values(revoked_at=NOW))
    job = await run_one(queue, queued)
    assert job["status"] == "failed"
    assert job["error"] == "Initiator no longer authorized: Project access denied"
    assert execute.await_count == 1  # the revoked member's job never executed
    assert await queue.redis.lrange(queue.processing_key, 0, -1) == []  # refused, not left for redelivery


@pytest.mark.team_state
@pytest.mark.asyncio
async def test_revoked_credential_and_insufficient_role_are_refused_per_attempt(team_db, monkeypatch):
    factory, _, _ = team_db
    execute = worker_against(factory, monkeypatch)
    queue = JobQueue(_FakeRedis())
    reader_run = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"},
                                     initiator=JobInitiator("principal", READER, READER, "run.request"))
    assert (await run_one(queue, reader_run))["error"] == "Initiator no longer authorized: Permission denied"

    contributor = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"},
                                      initiator=JobInitiator.for_principal(caller(CONTRIBUTOR), Permission.REQUEST_RUN))
    async with factory() as session, session.begin():
        await session.execute(update(ApiCredential).where(ApiCredential.id == CONTRIBUTOR).values(revoked_at=NOW))
    assert (await run_one(queue, contributor))["error"] == "Initiator no longer authorized: Credential is no longer authorized"

    shared_key = await queue.enqueue("health_check", initiator=JobInitiator("principal", 0, None, "run.request"))
    assert (await run_one(queue, shared_key))["error"] == (
        "Initiator no longer authorized: A named live credential is required")
    execute.assert_not_awaited()


@pytest.mark.team_state
@pytest.mark.asyncio
async def test_team_state_refuses_legacy_jobs_and_runs_system_and_read_jobs(team_db, monkeypatch):
    factory, _, _ = team_db
    execute = worker_against(factory, monkeypatch)
    queue = JobQueue(_FakeRedis())

    legacy = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"})
    job = await run_one(queue, legacy)
    assert (job["status"], job["error"]) == ("failed", LEGACY_REFUSAL)
    execute.assert_not_awaited()

    system = await queue.enqueue("health_check", initiator=JobInitiator.system("scheduler"))
    assert (await run_one(queue, system))["status"] == "completed"

    review = await queue.enqueue("diff_review", {"repo_path": "/synthetic/A", "base": "main", "head": "HEAD"},
                                 initiator=JobInitiator.for_principal(caller(READER), Permission.READ))
    assert (await run_one(queue, review))["status"] == "completed"
    assert execute.await_count == 2


@pytest.mark.team_state
@pytest.mark.asyncio
async def test_legacy_job_runs_again_once_the_team_is_back_to_solo(team_db, monkeypatch):
    factory, _, _ = team_db
    execute = worker_against(factory, monkeypatch)
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).values(revoked_at=NOW))
    queue = JobQueue(_FakeRedis())
    legacy = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"})
    assert (await run_one(queue, legacy))["status"] == "completed"
    execute.assert_awaited_once()


@pytest.mark.team_state
@pytest.mark.asyncio
async def test_follow_up_jobs_inherit_the_initiator_so_revocation_reaches_them(team_db, monkeypatch):
    factory, _, _ = team_db
    initiator = JobInitiator.for_principal(caller(CONTRIBUTOR), Permission.REQUEST_RUN)
    spawned = {}

    async def reindex(*_args, **_kwargs):
        assert current_initiator.get() == initiator
        assert JobInitiator.inherited("summary_enrichment") == initiator
        spawned["repair"] = await current_job.get().enqueue_repair({"repo_path": "/synthetic/A"})
        return {"status": "completed"}

    worker_against(factory, monkeypatch, outcome=reindex)
    queue = JobQueue(_FakeRedis())
    parent = await queue.enqueue("reindex", {"repo_path": "/synthetic/A"}, initiator=initiator)
    assert (await run_one(queue, parent))["status"] == "completed"
    assert JobInitiator.from_job(await queue.get_job(spawned["repair"])) == initiator
    assert JobInitiator.inherited("auto_reindex") == JobInitiator.system("auto_reindex")  # outside any job


@pytest.mark.team_state
@pytest.mark.asyncio
async def test_check_failure_keeps_the_delivery_recoverable(team_db, monkeypatch):
    factory, _, _ = team_db
    execute = worker_against(factory, monkeypatch)
    monkeypatch.setattr("brain.workers.worker.initiator_refusal", AsyncMock(side_effect=ConnectionError("db down")))
    queue = JobQueue(_FakeRedis())
    job_id = await queue.enqueue("reindex", initiator=JobInitiator.for_principal(caller(CONTRIBUTOR),
                                                                                 Permission.REQUEST_RUN))
    job = await run_one(queue, job_id)
    assert (job["status"], job["attempt"]) == ("queued", 0)  # no attempt burned, nothing executed
    assert job_id in await queue.redis.lrange(queue.processing_key, 0, -1)
    execute.assert_not_awaited()
