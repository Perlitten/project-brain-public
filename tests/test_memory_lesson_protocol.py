import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.routers import harness


class _Result:
    def __init__(self, value=None, rows=()):
        self.value, self.rows = value, rows
    def scalar_one_or_none(self): return self.value
    def scalars(self): return self
    def all(self): return list(self.rows)


class _Session:
    def __init__(self, task, validation, artifacts):
        self.calls = 0
        self.task = task
        self.validation = validation
        self.artifacts = artifacts
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False
    async def execute(self, _stmt):
        self.calls += 1
        if self.calls == 1:
            return _Result(self.task)
        if self.calls == 2:
            return _Result(self.validation)
        return _Result(rows=self.artifacts)


def body(**overrides):
    data = {"statement": "Use the verified fixture", "category": "test", "validation_id": 4,
            "artifact_ids": [8], "source_refs": ["test://case"], "idempotency_key": "lesson-0001"}
    data.update(overrides)
    return harness.LessonCreateRequest(**data)


@pytest.mark.asyncio
async def test_pass_creates_learning_and_fail_creates_failure_lesson(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, repo_path="/repo", version=0)
    event = SimpleNamespace(id=1, task_id=task_id, event_type="validated_lesson", actor="client", classification="learning", payload_json={},
                            idempotency_key="lesson-0001", causal_parent_id=None, expected_task_version=0,
                            task_version=1, created_at=None)
    store = AsyncMock(return_value=event)
    monkeypatch.setattr(harness, "_parse_task_id", lambda _: task_id)
    artifacts = [SimpleNamespace(id=8, path_or_uri="test://case")]
    monkeypatch.setattr(harness, "async_session_factory", lambda: _Session(task, SimpleNamespace(id=4, task_id=task_id, status="pass", artifact_id=8, exit_code=0), artifacts))
    monkeypatch.setattr(harness.HarnessStore, "add_event", store)
    await harness.add_validated_lesson(str(task_id), body())
    assert store.await_args.kwargs["classification"] == "learning"
    store.reset_mock()
    monkeypatch.setattr(harness, "async_session_factory", lambda: _Session(task, SimpleNamespace(id=4, task_id=task_id, status="error", artifact_id=8, exit_code=1), artifacts))
    await harness.add_validated_lesson(str(task_id), body(idempotency_key="lesson-0002"))
    assert store.await_args.kwargs["classification"] == "failure_lesson"


@pytest.mark.asyncio
async def test_wrong_validation_or_artifact_is_rejected(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, repo_path="/repo", version=0)
    monkeypatch.setattr(harness, "_parse_task_id", lambda _: task_id)
    monkeypatch.setattr(harness, "async_session_factory", lambda: _Session(task, None, []))
    with pytest.raises(Exception) as exc:
        await harness.add_validated_lesson(str(task_id), body())
    assert getattr(exc.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_missing_evidence_is_rejected_by_schema():
    with pytest.raises(ValueError):
        body(source_refs=[])
    with pytest.raises(ValueError):
        body(artifact_ids=[])


@pytest.mark.asyncio
async def test_idempotency_conflict_is_exposed_as_409(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, repo_path="/repo", version=7)
    validation = SimpleNamespace(id=4, task_id=task_id, status="pass", artifact_id=8, exit_code=0)
    monkeypatch.setattr(harness, "_parse_task_id", lambda _: task_id)
    monkeypatch.setattr(harness, "async_session_factory", lambda: _Session(task, validation, [SimpleNamespace(id=8, path_or_uri="test://case")]))
    from brain.memory.harness_store import IdempotencyConflict
    monkeypatch.setattr(harness.HarnessStore, "add_event", AsyncMock(side_effect=IdempotencyConflict("conflict")))
    with pytest.raises(Exception) as exc:
        await harness.add_validated_lesson(str(task_id), body())
    assert exc.value.status_code == 409
