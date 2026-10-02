from __future__ import annotations

import json
from datetime import datetime, timezone
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


def _finding(key="service:postgres", summary="Postgres is unavailable"):
    return {
        "severity": "critical",
        "status": "new",
        "title": "Core service requires attention",
        "summary": summary,
        "recommended_action": "Restore the datastore.",
        "dedupe_key": key,
    }


@pytest.mark.asyncio
async def test_telegram_alert_is_deduplicated_then_sends_recovery():
    redis = _Redis()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    send = AsyncMock(return_value={"status": "sent", "http_status": 200})

    with (
        patch.object(telegram, "redis_client", redis),
        patch.object(telegram, "_now", return_value=now),
        patch.object(telegram, "_send", send),
        patch.object(telegram.settings, "TELEGRAM_ALERTS_ENABLED", True),
        patch.object(telegram.settings, "TELEGRAM_ALERT_COOLDOWN_SECONDS", 3600),
    ):
        first = await deliver_diagnosis_alert([_finding()], generated_at=now.isoformat(), llm={"status": "used"})
        duplicate = await deliver_diagnosis_alert([_finding()], generated_at=now.isoformat(), llm={"status": "used"})
        recovery = await deliver_diagnosis_alert([], generated_at=now.isoformat(), llm={"status": "used"})

    assert first["status"] == "sent"
    assert duplicate["status"] == "suppressed"
    assert recovery["status"] == "sent"
    assert recovery["recovery"] is True
    assert send.await_count == 2
    state = json.loads(redis.values[ALERT_STATE_KEY])
    assert state["active"] is False


@pytest.mark.asyncio
async def test_changed_finding_bypasses_cooldown():
    redis = _Redis()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    send = AsyncMock(return_value={"status": "sent", "http_status": 200})

    with (
        patch.object(telegram, "redis_client", redis),
        patch.object(telegram, "_now", return_value=now),
        patch.object(telegram, "_send", send),
        patch.object(telegram.settings, "TELEGRAM_ALERTS_ENABLED", True),
        patch.object(telegram.settings, "TELEGRAM_ALERT_COOLDOWN_SECONDS", 3600),
    ):
        await deliver_diagnosis_alert([_finding()], generated_at=now.isoformat(), llm={"status": "used"})
        changed = await deliver_diagnosis_alert(
            [_finding("service:neo4j", "Neo4j is unavailable")],
            generated_at=now.isoformat(),
            llm={"status": "used"},
        )

    assert changed["status"] == "sent"
    assert send.await_count == 2


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
