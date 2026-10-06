"""In-process scheduler, job outcome ledger, /health staleness, /scheduler/jobs and the git-merge webhook.

No Postgres or Redis: a small in-memory Redis with a controllable clock stands in.
"""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

from brain.config.settings import settings
from brain.workers import job_outcomes, scheduler
from brain.workers.queue import JobStatus
from brain.workers.worker import process_one

client = TestClient(app, raise_server_exceptions=False)
HEADERS = {"X-API-Key": "test-secret-key"}
UTC = timezone.utc


class _ClockRedis:
    """SET NX EX / GET / DELETE / HSET / HGETALL with an injectable clock."""

    def __init__(self):
        self.now = 0.0
        self.values: dict[str, tuple[str, float | None]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def _live(self, key):
        item = self.values.get(key)
        if item and item[1] is not None and item[1] <= self.now:
            self.values.pop(key, None)
            return None
        return item

    async def set(self, key, value, nx=False, ex=None):
        if nx and self._live(key):
            return False
        self.values[key] = (value, self.now + ex if ex else None)
        return True

    async def get(self, key):
        item = self._live(key)
        return item[0] if item else None

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


@pytest.fixture
def fake_queue(monkeypatch):
    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=lambda job_type, *a, **k: f"job-{job_type}"))
    calls = []

    def fake_queue_for_job(redis, prefix, job_type, *, pools_enabled):
        calls.append(job_type)
        return queue

    monkeypatch.setattr(scheduler, "queue_for_job", fake_queue_for_job)
    monkeypatch.setattr(job_outcomes, "queue_for_job", fake_queue_for_job)
    queue.pool_calls = calls
    return queue


def _fired_types(queue):
    return [c.args[0] for c in queue.enqueue.await_args_list]


# --------------------------------------------------------------------------- scheduler


def test_schedule_matches_the_retired_n8n_cron_times():
    assert {j.job_type: j.cron for j in scheduler.SCHEDULES} == {
        "nightly_maintenance": "30 0 * * *",
        "health_check": "0 2 * * *",
        "self_diagnosis": "30 3 * * *",
        "benchmark": "0 6 * * 1",
    }
    cron = scheduler.Cron.parse("0 6 * * 1")
    assert cron.next_after(datetime(2026, 10, 6, 14, 0, tzinfo=UTC)) == datetime(2026, 10, 12, 6, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_slot_lock_prevents_double_run_across_workers(fake_queue, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_CATCHUP_WINDOW_S", 60)
    redis = _ClockRedis()
    now = datetime(2026, 10, 6, 2, 0, 10, tzinfo=UTC)

    first_worker = await scheduler.tick(redis, now)
    second_worker = await scheduler.tick(redis, now)
    restarted = await scheduler.tick(redis, now + timedelta(seconds=30))

    assert [f["job_type"] for f in first_worker] == ["health_check"]
    assert second_worker == [] and restarted == []
    assert _fired_types(fake_queue) == ["health_check"]
    kwargs = fake_queue.enqueue.await_args.kwargs
    assert kwargs["idempotency_key"] == "scheduler:health_check:2026-10-06T02:00:00+00:00"
    assert kwargs["max_attempts"] == settings.SCHEDULER_JOB_MAX_ATTEMPTS
    assert fake_queue.pool_calls == ["health_check"]


@pytest.mark.asyncio
async def test_catch_up_fires_a_missed_slot_once_within_the_window(fake_queue, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_CATCHUP_WINDOW_S", 6 * 3600)
    redis = _ClockRedis()
    # Worker was down from before 00:30 until 02:40: two nightly slots missed.
    boot = datetime(2026, 10, 6, 2, 40, tzinfo=UTC)

    fired = await scheduler.tick(redis, boot)
    again = await scheduler.tick(redis, boot + timedelta(minutes=1))

    assert sorted(f["job_type"] for f in fired) == ["health_check", "nightly_maintenance"]
    assert all(f["catch_up"] for f in fired)
    assert again == []
    # Monday 06:00 benchmark is ~21h old: outside the window, not replayed.
    assert "benchmark" not in _fired_types(fake_queue)


@pytest.mark.asyncio
async def test_concurrent_replicas_fire_each_slot_once(fake_queue, monkeypatch):
    """Several worker replicas ticking at once enqueue each due slot at most once
    (the SET NX slot lock decides the winner; everyone else skips it)."""
    monkeypatch.setattr(settings, "SCHEDULER_CATCHUP_WINDOW_S", 60)
    redis = _ClockRedis()
    now = datetime(2026, 10, 6, 2, 0, 10, tzinfo=UTC)

    batches = await asyncio.gather(*(scheduler.tick(redis, now) for _ in range(4)))

    fired = [entry for batch in batches for entry in batch]
    assert [f["job_type"] for f in fired] == ["health_check"]
    assert _fired_types(fake_queue) == ["health_check"]


@pytest.mark.asyncio
async def test_failed_enqueue_releases_the_slot_for_the_next_tick(fake_queue, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_CATCHUP_WINDOW_S", 60)
    fake_queue.enqueue = AsyncMock(side_effect=[RuntimeError("redis blip"), "job-1"])
    redis = _ClockRedis()
    now = datetime(2026, 10, 6, 2, 0, 5, tzinfo=UTC)

    assert await scheduler.tick(redis, now) == []
    retried = await scheduler.tick(redis, now + timedelta(seconds=30))
    assert [f["job_id"] for f in retried] == ["job-1"]


@pytest.mark.asyncio
async def test_disabled_scheduler_never_ticks(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    tick = AsyncMock()
    monkeypatch.setattr(scheduler, "tick", tick)
    await scheduler.run_scheduler(_ClockRedis(), SimpleNamespace(is_set=lambda: False))
    tick.assert_not_awaited()


# --------------------------------------------------------------------------- retries + failure routing


def _failing_queue(attempt, max_attempts=3):
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value="job-1")
    queue.get_job = AsyncMock(
        return_value={"id": "job-1", "type": "health_check", "params": {}, "max_attempts": max_attempts}
    )
    queue.start_attempt = AsyncMock(return_value=attempt)
    queue.schedule_retry = AsyncMock()
    queue.update_status = AsyncMock()
    queue.ack = AsyncMock()
    return queue


@pytest.mark.asyncio
async def test_retry_backoff_is_exponential_and_stops_at_max_attempts(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RETRY_BASE_DELAY_S", 5)
    monkeypatch.setattr(settings, "WORKER_RETRY_MAX_DELAY_S", 300)
    route = AsyncMock(return_value="diag-1")
    record = AsyncMock()
    delays = []
    with (
        patch("brain.workers.worker.execute_job", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("brain.workers.worker.route_final_failure", route),
        patch("brain.workers.worker.record_failure", record),
    ):
        for attempt in (1, 2, 3):
            queue = _failing_queue(attempt)
            assert await process_one(queue) is True
            if queue.schedule_retry.await_count:
                delays.append(queue.schedule_retry.await_args.args[2])
            else:
                queue.update_status.assert_awaited_once_with("job-1", JobStatus.FAILED, error="boom")

    assert delays == [5, 10]
    route.assert_awaited_once()
    assert route.await_args.args[1:] == ("health_check", "job-1", "boom")
    record.assert_awaited_once()
    assert job_outcomes.retry_delay_seconds(10) == 300


@pytest.mark.asyncio
async def test_final_failure_routes_to_self_diagnosis(fake_queue):
    job_id = await job_outcomes.route_final_failure(
        _ClockRedis(), "nightly_maintenance", "job-9", "index lock timeout", now=600.0
    )

    assert job_id == "job-self_diagnosis"
    args, kwargs = fake_queue.enqueue.await_args
    assert args[0] == "self_diagnosis"
    assert args[1]["trigger_context"] == {
        "source": "job_failure",
        "summary": "index lock timeout",
        "reference": "nightly_maintenance:job-9",
    }
    assert kwargs["idempotency_key"] == "job-failure:nightly_maintenance:2"
    assert fake_queue.pool_calls == ["self_diagnosis"]


@pytest.mark.asyncio
async def test_self_diagnosis_failure_is_not_routed_to_itself(fake_queue):
    assert await job_outcomes.route_final_failure(_ClockRedis(), "self_diagnosis", "job-1", "x") is None
    fake_queue.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_records_outcome_and_pings_deadman(monkeypatch):
    monkeypatch.setattr(settings, "DEADMAN_URL_HEALTH_CHECK", "https://hc-ping.example/abc")
    seen = []

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            seen.append(url)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(job_outcomes.httpx, "AsyncClient", _Client)
    redis = _ClockRedis()
    await job_outcomes.record_success(redis, "health_check", "job-1", 2)

    stats = redis.hashes[scheduler.job_stats_key("health_check")]
    assert stats["last_success_job_id"] == "job-1" and stats["last_attempts"] == "2"
    assert seen == ["https://hc-ping.example/abc"]


# --------------------------------------------------------------------------- /health + /scheduler/jobs


_HEALTHY = {"postgres": {"status": "healthy"}, "redis": {"status": "healthy"}, "neo4j": {"status": "healthy"}}


def _redis_with_successes(age: timedelta) -> _ClockRedis:
    redis = _ClockRedis()
    stamp = (datetime.now(UTC) - age).isoformat()
    redis.values[scheduler.started_at_key()] = (stamp, None)
    for job in scheduler.SCHEDULES:
        redis.hashes[scheduler.job_stats_key(job.job_type)] = {"last_success_at": stamp}
    return redis


@pytest.mark.parametrize(
    ("age", "health_status", "stale"),
    [
        (timedelta(hours=1), "ok", []),
        # Daily jobs: 24h interval + 2h grace exceeded; the weekly benchmark is not stale yet.
        (timedelta(hours=27), "degraded", ["nightly_maintenance", "health_check", "self_diagnosis"]),
    ],
)
def test_staleness_flips_health_to_degraded(monkeypatch, age, health_status, stale):
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEDULER_STALE_GRACE_S", 2 * 3600)
    with (
        patch("apps.api.routers.core.check_health", AsyncMock(return_value=dict(_HEALTHY))),
        patch("apps.api.routers.core.redis_client", _redis_with_successes(age)),
    ):
        body = client.get("/health").json()
    assert body["status"] == health_status
    assert body["scheduler"]["stale"] == stale
    assert body["scheduler"]["jobs"]["benchmark"] == "ok"


def test_datastore_failure_outranks_scheduler_staleness(monkeypatch):
    unhealthy = {**_HEALTHY, "redis": {"status": "unhealthy"}}
    with (
        patch("apps.api.routers.core.check_health", AsyncMock(return_value=unhealthy)),
        patch("apps.api.routers.core.redis_client", _redis_with_successes(timedelta(days=3))),
    ):
        assert client.get("/health").json()["status"] == "error"


@pytest.fixture
def api_key_env():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        yield mock_settings


def test_scheduler_jobs_endpoint_lists_every_scheduled_job(api_key_env, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)
    redis = _redis_with_successes(timedelta(hours=1))
    redis.hashes[scheduler.job_stats_key("benchmark")].update(
        {"last_failure_at": datetime.now(UTC).isoformat(), "last_error": "golden file missing", "last_attempts": "3"}
    )
    with patch("apps.api.routers.scheduler.redis_client", redis):
        response = client.get("/scheduler/jobs", headers=HEADERS)
    assert response.status_code == 200, response.text
    jobs = {j["job_type"]: j for j in response.json()["jobs"]}
    assert set(jobs) == {"nightly_maintenance", "health_check", "self_diagnosis", "benchmark"}
    assert jobs["benchmark"]["status"] == "degraded"
    assert jobs["benchmark"]["last_error"] == "golden file missing"
    assert jobs["benchmark"]["last_attempts"] == 3
    assert jobs["nightly_maintenance"]["pool"] == "deep"
    assert jobs["health_check"]["trigger_path"] == "/jobs/health-check"
    assert all(j["next_run_at"] for j in jobs.values())


def test_scheduler_jobs_requires_api_key(api_key_env):
    assert client.get("/scheduler/jobs").status_code == 401


# --------------------------------------------------------------------------- webhook


def _raw(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def _github_sig(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _signed_post(payload: dict, secret: str = "s3cret", headers: dict | None = None):
    raw = _raw(payload)
    all_headers = {"Content-Type": "application/json", "X-Hub-Signature-256": _github_sig(secret, raw)}
    all_headers.update(headers or {})
    return client.post("/webhooks/git-merge", content=raw, headers=all_headers)


@pytest.fixture
def webhook_env(monkeypatch, fake_queue):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_TOKEN", "s3cret")
    monkeypatch.setattr("apps.api.routers.scheduler.queue_for_job", lambda *a, **k: fake_queue)
    return fake_queue


def test_git_merge_webhook_enqueues_reindex_on_a_valid_github_signature(webhook_env):
    response = _signed_post({"ref": "refs/heads/main", "sha": "abc123"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    args, kwargs = webhook_env.enqueue.await_args
    assert args[0] == "reindex"
    assert args[1]["source_revision"] == "abc123" and args[1]["benchmark_after"] is True
    assert kwargs["idempotency_key"] == "git-reindex:abc123"


def test_git_merge_webhook_accepts_the_api_key(webhook_env, api_key_env):
    response = client.post(
        "/webhooks/git-merge",
        json={"ref": "refs/heads/main", "sha": "abc123"},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200, response.text
    assert webhook_env.enqueue.await_args.args[0] == "reindex"


def test_git_merge_webhook_rejects_everything_unauthenticated(webhook_env):
    payload = {"ref": "refs/heads/main", "sha": "abc"}
    assert _signed_post(payload, secret="wrong-secret").status_code == 401
    bad = client.post(
        "/webhooks/git-merge",
        content=_raw(payload),
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert bad.status_code == 401
    no_auth = client.post(
        "/webhooks/git-merge", content=_raw(payload), headers={"Content-Type": "application/json"}
    )
    assert no_auth.status_code == 401
    # The retired shared-header scheme no longer authenticates on its own.
    legacy = client.post(
        "/webhooks/git-merge",
        content=_raw(payload),
        headers={"Content-Type": "application/json", "X-Project-Brain-Token": "s3cret"},
    )
    assert legacy.status_code == 401
    webhook_env.enqueue.assert_not_awaited()


def test_git_merge_webhook_ignores_other_refs(webhook_env):
    response = _signed_post({"ref": "refs/heads/feature", "sha": "abc"})
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    webhook_env.enqueue.assert_not_awaited()


def test_git_merge_webhook_ref_is_configurable(webhook_env, monkeypatch):
    """PROJECT_BRAIN_WEBHOOK_REF picks the accepted ref; the default matches
    the branch project-brain-reindex.yml triggers on."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_REF", "refs/heads/staging")

    queued = _signed_post({"ref": "refs/heads/staging", "sha": "abc"})
    assert queued.status_code == 200 and queued.json()["status"] == "queued"
    assert webhook_env.enqueue.await_count == 1

    ignored = _signed_post({"ref": "refs/heads/main", "sha": "def"})
    assert ignored.status_code == 200 and ignored.json()["status"] == "ignored"
    assert webhook_env.enqueue.await_count == 1


def test_git_merge_webhook_fails_closed_without_any_configured_credential(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_TOKEN", None)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", None)
    response = client.post(
        "/webhooks/git-merge",
        content=_raw({"ref": "refs/heads/main"}),
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert response.status_code == 503


class _DedupQueue:
    """Honors ``JobQueue.enqueue``'s idempotency_key contract: same key -> same job id."""

    def __init__(self):
        self.claims: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def enqueue(self, job_type, params=None, *, idempotency_key=None, **_kwargs):
        if idempotency_key and idempotency_key in self.claims:
            return self.claims[idempotency_key]
        job_id = f"job-{len(self.calls)}"
        if idempotency_key:
            self.claims[idempotency_key] = job_id
        self.calls.append((job_type, params, idempotency_key))
        return job_id


def test_redelivered_merge_webhook_does_not_enqueue_a_second_reindex(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_TOKEN", "s3cret")
    queue = _DedupQueue()
    monkeypatch.setattr("apps.api.routers.scheduler.queue_for_job", lambda *a, **k: queue)

    responses = [_signed_post({"ref": "refs/heads/main", "sha": "deadbeef"}) for _ in range(2)]

    assert all(r.status_code == 200 for r in responses)
    assert [call[0] for call in queue.calls] == ["reindex"]
    assert responses[0].json()["job_id"] == responses[1].json()["job_id"]
    # The durable dedup lives in JobQueue.enqueue's SET NX claim keyed by
    # git-reindex:{sha}; tests/test_worker_queue.py covers that contract.
