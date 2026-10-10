"""Unvalidated L1 intake: POST /tasks/{id}/observations and the handoff → L1 mirror.

Both feed memory consolidation with ``reported_validation=false`` so the gates
apply the stricter unvalidated evidence tier instead of trusting the claim.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.routers import harness
from brain.database.harness_models import AgentTaskEvent
from brain.memory.harness_store import HarnessStore


def body(**overrides):
    data = {"statement": "Worker needs a restart after settings change", "category": "workflow",
            "idempotency_key": "observation-0001"}
    data.update(overrides)
    return harness.ObservationCreateRequest(**data)


def _event(task_id, **overrides):
    data = dict(id=1, task_id=task_id, event_type="observed_lesson", actor="client", classification="learning",
                payload_json={}, idempotency_key="observation-0001", causal_parent_id=None,
                expected_task_version=0, task_version=1, created_at=None)
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_observation_is_recorded_as_unvalidated_l1_event(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, repo_path="/repo", version=0)
    store = AsyncMock(return_value=_event(task_id))
    monkeypatch.setattr(harness, "_parse_task_id", lambda _: task_id)
    monkeypatch.setattr(harness.HarnessStore, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(harness.HarnessStore, "add_event", store)

    result = await harness.add_task_observation(str(task_id), body())

    args, kwargs = store.await_args
    assert args[:3] == (task_id, "observed_lesson", "client")
    payload = args[3]
    assert payload["reported_validation"] is False
    assert payload["evidence_tier"] == "unvalidated"
    assert payload["repo_scope"] == "/repo"
    assert payload["statement"] == "Worker needs a restart after settings change"
    assert kwargs["classification"] == "learning"
    assert kwargs["idempotency_key"] == "observation-0001"
    assert result["event_type"] == "observed_lesson"


@pytest.mark.asyncio
async def test_observation_outcome_selects_failure_lesson(monkeypatch):
    task_id = uuid.uuid4()
    store = AsyncMock(return_value=_event(task_id, classification="failure_lesson"))
    monkeypatch.setattr(harness, "_parse_task_id", lambda _: task_id)
    monkeypatch.setattr(harness.HarnessStore, "get_task", AsyncMock(return_value=SimpleNamespace(repo_path=None)))
    monkeypatch.setattr(harness.HarnessStore, "add_event", store)
    await harness.add_task_observation(str(task_id), body(outcome="failure_lesson"))
    assert store.await_args.kwargs["classification"] == "failure_lesson"
    assert store.await_args.args[3]["repo_scope"] is None


@pytest.mark.asyncio
async def test_observation_missing_task_is_404(monkeypatch):
    task_id = uuid.uuid4()
    monkeypatch.setattr(harness, "_parse_task_id", lambda _: task_id)
    monkeypatch.setattr(harness.HarnessStore, "get_task", AsyncMock(return_value=None))
    with pytest.raises(Exception) as exc:
        await harness.add_task_observation(str(task_id), body())
    assert getattr(exc.value, "status_code", None) == 404


def test_observation_schema_rejects_weak_input():
    with pytest.raises(ValueError):
        body(idempotency_key="short")
    with pytest.raises(ValueError):
        body(outcome="validated")
    with pytest.raises(ValueError):
        body(statement="   ")


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    """First execute → task row, second → existing event id (idempotency check)."""

    def __init__(self, task, existing_event_id=None):
        self.task, self.existing = task, existing_event_id
        self.added, self.calls = [], 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def execute(self, _stmt):
        self.calls += 1
        return _Result(self.task if self.calls == 1 else self.existing)


def _handoff(task_id, summary="Deploy needs reports/ excluded from the tar", session_id="sess-1"):
    return SimpleNamespace(task_id=task_id, session_id=session_id, summary=summary, memory_ref="mem://1")


@pytest.mark.asyncio
async def test_handoff_summary_is_mirrored_as_unvalidated_learning_event():
    task_id = uuid.uuid4()
    session = _Session(SimpleNamespace(id=task_id, repo_path="/repo", version=3))
    event = await HarnessStore._mirror_handoff_as_l1_event(session, _handoff(task_id))
    assert isinstance(event, AgentTaskEvent)
    assert session.added == [event]
    assert event.event_type == "handoff_summary" and event.actor == "handoff"
    assert event.classification == "learning"
    assert event.task_version == 3
    assert event.idempotency_key.startswith("handoff:") and len(event.idempotency_key) == len("handoff:") + 48
    payload = event.payload_json
    assert payload["reported_validation"] is False and payload["evidence_tier"] == "unvalidated"
    assert payload["repo_scope"] == "/repo" and payload["source"] == "handoff"
    assert payload["summary"] == "Deploy needs reports/ excluded from the tar"


@pytest.mark.asyncio
async def test_handoff_mirror_is_idempotent_and_skips_blank_or_orphan():
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, repo_path="/repo", version=0)
    first = await HarnessStore._mirror_handoff_as_l1_event(_Session(task), _handoff(task_id))
    second = await HarnessStore._mirror_handoff_as_l1_event(_Session(task), _handoff(task_id))
    assert first.idempotency_key == second.idempotency_key  # same session+summary → same key
    other = await HarnessStore._mirror_handoff_as_l1_event(_Session(task), _handoff(task_id, session_id="sess-2"))
    assert other.idempotency_key != first.idempotency_key

    dup_session = _Session(task, existing_event_id=42)
    assert await HarnessStore._mirror_handoff_as_l1_event(dup_session, _handoff(task_id)) is None
    assert dup_session.added == []

    blank = _Session(task)
    assert await HarnessStore._mirror_handoff_as_l1_event(blank, _handoff(task_id, summary="   ")) is None
    assert blank.calls == 0

    orphan = _Session(None)
    assert await HarnessStore._mirror_handoff_as_l1_event(orphan, _handoff(task_id)) is None
    assert orphan.added == []
