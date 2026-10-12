from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import brain.alerts.telegram as telegram
from apps.api.routers import telegram_bridge
from brain.alerts.telegram import ALERT_STATE_KEY, deliver_diagnosis_alert
from brain.workers.tasks import run_self_diagnosis


def _FakeRequest():
    return SimpleNamespace(state=SimpleNamespace())


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.pending = []

    def set(self, key, value, ex=None):
        self.pending.append((key, value))
        return self

    async def execute(self):
        for key, value in self.pending:
            self.redis.values[key] = value
        return [True] * len(self.pending)


class _Redis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def pipeline(self):
        return _Pipeline(self)


def _finding(key="service_health:core_unhealthy", summary="Postgres is unavailable", severity="critical", **extra):
    return {
        "severity": severity,
        "status": "new",
        "title": "Core service requires attention",
        "summary": summary,
        "recommended_action": "Restore the datastore.",
        "dedupe_key": key,
        "evidence": [{"label": "postgres", "value": "connection refused"}],
        **extra,
    }


def _failed_job(job_id="j-42", job_type="reindex", error="source revision mismatch"):
    return _finding(
        f"worker_health:failed_job:{job_id}",
        f"A recent {job_type} job failed",
        severity="warning",
        title="Recent background job failed",
        evidence=[
            {"label": "job_id", "value": job_id},
            {"label": "job_type", "value": job_type},
            {"label": "error", "value": error},
        ],
    )


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, hours):
        self.now += timedelta(hours=hours)


@contextmanager
def _env(redis, clock, send, cooldown=3600):
    with (
        patch.object(telegram, "redis_client", redis),
        patch.object(telegram, "_now", clock),
        patch.object(telegram, "_send", send),
        patch.object(telegram.settings, "TELEGRAM_ALERTS_ENABLED", True),
        patch.object(telegram.settings, "TELEGRAM_ALERT_COOLDOWN_SECONDS", cooldown),
        patch.object(telegram.settings, "BRAIN_WEB_URL", "https://brain.example"),
    ):
        yield


async def _deliver(insights, clock):
    return await deliver_diagnosis_alert(insights, generated_at=clock.now.isoformat(), llm={"status": "used"})


@pytest.mark.asyncio
async def test_telegram_alert_is_deduplicated_then_sends_recovery():
    redis, clock = _Redis(), _Clock()
    send = AsyncMock(return_value={"status": "sent", "http_status": 200})

    with _env(redis, clock, send):
        first = await _deliver([_finding()], clock)
        duplicate = await _deliver([_finding(summary="Postgres is down (reworded)")], clock)
        recovery = await _deliver([], clock)
        quiet = await _deliver([], clock)

    assert first["status"] == "sent"
    assert duplicate["status"] == "suppressed"
    assert recovery["status"] == "sent"
    assert recovery["recovery"] is True
    assert quiet["status"] == "quiet"
    assert send.await_count == 2
    alert, fixed = (call.args[0] for call in send.await_args_list)
    assert "1 new problem" in alert
    assert "Down: postgres" in alert
    assert "→ " in alert and "https://brain.example/logs" in alert
    # No noise the owner can't act on.
    assert "LLM" not in alert and "Build" not in alert
    assert "all clear again" in fixed and "Down: postgres" in fixed
    state = json.loads(redis.values[ALERT_STATE_KEY])
    assert state["active"] == {}


@pytest.mark.asyncio
async def test_changed_finding_bypasses_cooldown():
    redis, clock = _Redis(), _Clock()
    send = AsyncMock(return_value={"status": "sent", "http_status": 200})

    with _env(redis, clock, send):
        await _deliver([_finding()], clock)
        changed = await _deliver(
            [_finding(), _finding("retrieval_quality:evaluation_missing", severity="warning")], clock
        )

    assert changed["status"] == "sent"
    assert changed["new"] == 1
    assert send.await_count == 2
    text = send.await_args_list[1].args[0]
    assert "Search quality not measured" in text
    assert "Down:" not in text  # already announced
    assert "Still open, already reported: 1" in text


@pytest.mark.asyncio
async def test_only_lasting_critical_findings_are_repeated_after_cooldown():
    redis, clock = _Redis(), _Clock()
    send = AsyncMock(return_value={"status": "sent", "http_status": 200})
    warning = _finding("retrieval_quality:evaluation_missing", severity="warning")

    with _env(redis, clock, send, cooldown=3600):
        await _deliver([_finding(), warning, _failed_job()], clock)
        clock.advance(2)
        reminder = await _deliver([_finding(), warning, _failed_job()], clock)
        clock.advance(0.5)
        quiet = await _deliver([_finding(), warning, _failed_job()], clock)

    assert reminder["status"] == "sent"
    assert reminder["reminded"] == 1
    text = send.await_args_list[1].args[0]
    assert "critical problems still unresolved" in text
    assert "⏰ open 2 h" in text and "Down: postgres" in text
    assert "Search quality" not in text and "Job failed" not in text
    assert quiet["status"] == "suppressed"
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_llm_observations_and_duplicate_job_triggers_are_not_pushed():
    redis, clock = _Redis(), _Clock()
    send = AsyncMock(return_value={"status": "sent", "http_status": 200})
    precision = _finding("llm:precision-a", "Precision 0.36", severity="warning", source="llm", title="Low Precision")
    trigger = _finding(
        "automation_trigger:job_failure:reindex:j-42",
        "source revision mismatch",
        severity="warning",
        evidence=[{"label": "source", "value": "job_failure"}, {"label": "reference", "value": "reindex:j-42"}],
    )

    with _env(redis, clock, send):
        first = await _deliver([precision, trigger, _failed_job()], clock)
        # The LLM rewords itself and the failed job ages out of the recent list:
        # neither is news, and a job that happened is never "fixed".
        reworded = _finding("llm:precision-b", "Low precision in eval", severity="warning", source="llm")
        later = await _deliver([reworded], clock)

    assert first["finding_count"] == 1
    text = send.await_args_list[0].args[0]
    assert "Job failed: reindex" in text
    assert "Error: source revision mismatch (job j-42)" in text
    assert "Precision" not in text and "Low" not in text
    assert later["status"] == "quiet"
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_failed_send_is_retried_on_the_next_run():
    redis, clock = _Redis(), _Clock()
    send = AsyncMock(side_effect=[{"status": "failed", "error_type": "ConnectError"}, {"status": "sent"}])

    with _env(redis, clock, send):
        failed = await _deliver([_finding()], clock)
        retried = await _deliver([_finding()], clock)

    assert failed["status"] == "failed"
    assert retried["status"] == "sent" and retried["new"] == 1


@pytest.mark.asyncio
async def test_state_from_the_previous_format_does_not_repeat_old_findings():
    redis, clock = _Redis(), _Clock()
    redis.values[ALERT_STATE_KEY] = json.dumps(
        {
            "active": True,
            "fingerprint": "x",
            "active_keys": ["service_health:core_unhealthy"],
            "last_sent_at": clock.now.isoformat(),
        }
    )
    send = AsyncMock(return_value={"status": "sent"})

    with _env(redis, clock, send):
        result = await _deliver([_finding(severity="warning")], clock)

    assert result["status"] == "suppressed"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_healthy_nightly_maintenance_is_silent():
    send = AsyncMock(return_value={"status": "sent"})
    healthy = {"total_eligible_chunks": 8360, "chunks_with_current_embeddings": 8360, "pgvector_coverage_pct": 100.0}
    with (
        patch.object(telegram, "_send", send),
        patch.object(telegram.settings, "TELEGRAM_ALERTS_ENABLED", True),
        patch.object(telegram.settings, "BRAIN_WEB_URL", None),
    ):
        quiet = await telegram.deliver_maintenance_alert(
            repo_path="/app",
            dense_verification=healthy,
            late_sync={},
            quality_probes=[{"passed": True}] * 3,
            repaired_embeddings=4,
        )
        degraded = await telegram.deliver_maintenance_alert(
            repo_path="/srv/repos/shop",
            dense_verification={**healthy, "chunks_with_current_embeddings": 8300},
            late_sync={},
            quality_probes=[{"passed": True}, {"passed": False}],
            repaired_embeddings=0,
        )

    assert quiet["status"] == "quiet"
    assert degraded["status"] == "sent"
    text = send.await_args.args[0]
    assert "Nightly maintenance of shop" in text
    assert "60 of 8360 chunks lack current embeddings" in text
    assert "1 of 2 passed" in text
    assert "Build" not in text


@pytest.mark.asyncio
async def test_self_diagnosis_delivery_failure_fails_job_for_retry():
    diagnosis = {
        "generated_at": "2026-07-28T12:00:00+00:00",
        "persisted": {"insights": [_finding()]},
        "llm": {"status": "used", "model": "test"},
    }
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)

    with (
        patch("brain.workers.tasks.init_db", new=AsyncMock()),
        patch(
            "brain.workers.tasks.generate_proactive_insights",
            new=AsyncMock(return_value=diagnosis),
        ),
        patch(
            "brain.workers.tasks.deliver_diagnosis_alert",
            new=AsyncMock(return_value={"status": "failed"}),
        ),
        patch("brain.workers.tasks.redis_client", redis),
    ):
        with pytest.raises(RuntimeError, match="Telegram delivery"):
            await run_self_diagnosis({"use_llm": True})

    redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_bridge_fails_closed_when_production_api_key_is_missing():
    with (
        patch.object(telegram_bridge.settings, "ENVIRONMENT", "production"),
        patch.object(telegram_bridge.settings, "PROJECT_BRAIN_API_KEY", None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await telegram_bridge._authorize(_FakeRequest(), None, None)
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_telegram_bridge_allows_keyless_local_development():
    with (
        patch.object(telegram_bridge.settings, "ENVIRONMENT", "local"),
        patch.object(telegram_bridge.settings, "PROJECT_BRAIN_API_KEY", None),
    ):
        await telegram_bridge._authorize(_FakeRequest(), None, None)


@pytest.mark.asyncio
async def test_self_diagnosis_still_alerts_when_schema_initialization_is_down():
    diagnosis = {
        "generated_at": "2026-07-28T12:00:00+00:00",
        "persisted": {"insights": [_finding()]},
        "llm": {"status": "used", "model": "test"},
    }
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    with (
        patch("brain.workers.tasks.init_db", new=AsyncMock(side_effect=RuntimeError("postgres down"))),
        patch(
            "brain.workers.tasks.generate_proactive_insights",
            new=AsyncMock(return_value=diagnosis),
        ),
        patch(
            "brain.workers.tasks.deliver_diagnosis_alert",
            new=AsyncMock(return_value={"status": "sent"}),
        ) as deliver,
        patch("brain.workers.tasks.redis_client", redis),
    ):
        result = await run_self_diagnosis({"use_llm": True})

    assert result["status"] == "completed"
    deliver.assert_awaited_once()
