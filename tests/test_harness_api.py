"""Tests for the harness task-ledger API endpoints (store layer mocked)."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

from brain.database.harness_models import AgentTask
from brain.memory.harness_store import (
    InvalidTransition,
    LeaseConflict,
    TaskNotFound,
    VersionConflict,
)

client = TestClient(app, raise_server_exceptions=False)

TASK_ID = uuid.uuid4()


def _sample_task(status="created"):
    return AgentTask(
        id=TASK_ID,
        title="demo",
        goal="demo goal",
        status=status,
        repo_path="/tmp/repo",
        priority="P2",
        timeout_seconds=900,
        retry_budget=1,
    )


@pytest.fixture
def api_key_env():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        yield mock_settings


AUTH = {"X-API-Key": "test-secret-key"}


def test_create_task_requires_api_key(api_key_env):
    response = client.post("/harness/tasks", json={"title": "t", "goal": "g", "repo_path": "/tmp"})
    assert response.status_code == 401


def test_create_task(api_key_env):
    with patch("apps.api.routers.harness.HarnessStore.create_task", AsyncMock(return_value=_sample_task())):
        response = client.post(
            "/harness/tasks",
            json={"title": "demo", "goal": "demo goal", "repo_path": "/tmp/repo"},
            headers=AUTH,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created"
    assert body["repo_path"] == "/tmp/repo"


def test_get_task_not_found(api_key_env):
    with patch("apps.api.routers.harness.HarnessStore.get_task", AsyncMock(return_value=None)):
        response = client.get(f"/harness/tasks/{uuid.uuid4()}", headers=AUTH)
    assert response.status_code == 404


def test_get_task_bad_uuid(api_key_env):
    response = client.get("/harness/tasks/not-a-uuid", headers=AUTH)
    assert response.status_code == 422


def test_update_status_invalid_transition_returns_409(api_key_env):
    with patch(
        "apps.api.routers.harness.HarnessStore.update_status",
        AsyncMock(side_effect=InvalidTransition("bad transition")),
    ):
        response = client.patch(
            f"/harness/tasks/{TASK_ID}",
            json={
                "status": "running",
                "actor": "claude",
                "expected_task_version": 0,
            },
            headers=AUTH,
        )
    assert response.status_code == 409


def test_update_status_task_not_found_returns_404(api_key_env):
    with patch(
        "apps.api.routers.harness.HarnessStore.update_status",
        AsyncMock(side_effect=TaskNotFound(str(TASK_ID))),
    ):
        response = client.patch(
            f"/harness/tasks/{TASK_ID}",
            json={
                "status": "routed",
                "actor": "claude",
                "expected_task_version": 0,
            },
            headers=AUTH,
        )
    assert response.status_code == 404


def test_update_status_success(api_key_env):
    with patch(
        "apps.api.routers.harness.HarnessStore.update_status",
        AsyncMock(return_value=_sample_task(status="routed")),
    ):
        response = client.patch(
            f"/harness/tasks/{TASK_ID}",
            json={
                "status": "routed",
                "actor": "claude",
                "expected_task_version": 0,
            },
            headers=AUTH,
        )
    assert response.status_code == 200
    assert response.json()["status"] == "routed"


def test_list_tasks(api_key_env):
    with (
        patch("apps.api.routers.harness.HarnessStore.list_tasks", AsyncMock(return_value=[_sample_task()])),
        patch("apps.api.routers.harness.HarnessStore.count_tasks", AsyncMock(return_value=1)),
        patch("apps.api.routers.harness.HarnessStore.task_status_counts", AsyncMock(return_value={"created": 1})),
    ):
        response = client.get("/harness/tasks", headers=AUTH)
    assert response.status_code == 200
    assert len(response.json()["tasks"]) == 1


def test_tasks_search_pagination_and_scope_preserve_full_history_totals(api_key_env):
    with (
        patch("apps.api.routers.harness.HarnessStore.list_tasks", AsyncMock(return_value=[_sample_task()])) as rows,
        patch("apps.api.routers.harness.HarnessStore.count_tasks", AsyncMock(return_value=250)) as count,
        patch("apps.api.routers.harness.HarnessStore.task_status_counts", AsyncMock(return_value={"created": 200, "completed": 50})) as facets,
    ):
        response = client.get("/harness/tasks?repo_path=%2Fapp&q=old%20task&status=created&page=11&page_size=20", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["total_pages"], body["page"]) == (250, 13, 11)
    assert body["facets"]["status"] == {"created": 200, "completed": 50}
    rows.assert_awaited_once_with(status="created", repo_path="/app", query="old task", offset=200, limit=20)
    count.assert_awaited_once_with(status="created", repo_path="/app", query="old task")
    facets.assert_awaited_once_with(repo_path="/app", query="old task")


def test_tasks_count_failure_does_not_fabricate_page_length_as_total(api_key_env):
    with (
        patch("apps.api.routers.harness.HarnessStore.list_tasks", AsyncMock(return_value=[_sample_task()])),
        patch("apps.api.routers.harness.HarnessStore.count_tasks", AsyncMock(side_effect=RuntimeError("count unavailable"))),
    ):
        response = client.get("/harness/tasks", headers=AUTH)
    assert response.status_code == 500


def test_list_validations(api_key_env):
    from brain.database.harness_models import AgentValidationResult

    v = AgentValidationResult(id=1, task_id=TASK_ID, validator="pytest", status="pass", exit_code=0)
    with patch("apps.api.routers.harness.HarnessStore.list_validations", AsyncMock(return_value=[v])):
        response = client.get(f"/harness/tasks/{TASK_ID}/validations", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["validations"][0]["validator"] == "pytest"


def test_list_handoffs(api_key_env):
    from brain.database.harness_models import MemoryHandoff

    handoff = MemoryHandoff(id=1, task_id=None, session_id="s1", summary="did stuff")
    with patch("apps.api.routers.harness.HarnessStore.list_handoffs", AsyncMock(return_value=[handoff])):
        response = client.get("/harness/handoffs?session_id=s1", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["handoffs"][0]["summary"] == "did stuff"


def test_add_event_forwards_idempotency_and_cas(api_key_env):
    from brain.database.harness_models import AgentTaskEvent

    event = AgentTaskEvent(
        id=7,
        task_id=TASK_ID,
        event_type="agent_completed_unaccepted",
        actor="claude",
        classification="claim",
        idempotency_key="NOSTIA-TASK:completed:attempt-1",
        causal_parent_id=6,
        expected_task_version=3,
        task_version=4,
        payload_json={"status": "completed_unaccepted"},
    )
    with patch(
        "apps.api.routers.harness.HarnessStore.add_event",
        AsyncMock(return_value=event),
    ) as add_event:
        response = client.post(
            f"/harness/tasks/{TASK_ID}/events",
            json={
                "event_type": event.event_type,
                "actor": event.actor,
                "classification": event.classification,
                "idempotency_key": event.idempotency_key,
                "causal_parent_id": event.causal_parent_id,
                "expected_task_version": event.expected_task_version,
                "payload": event.payload_json,
            },
            headers=AUTH,
        )
    assert response.status_code == 200
    assert response.json()["task_version"] == 4
    assert response.json()["idempotency_key"] == event.idempotency_key
    assert add_event.await_args.kwargs["expected_task_version"] == 3


def test_add_event_version_conflict_returns_409(api_key_env):
    with patch(
        "apps.api.routers.harness.HarnessStore.add_event",
        AsyncMock(side_effect=VersionConflict("stale version")),
    ):
        response = client.post(
            f"/harness/tasks/{TASK_ID}/events",
            json={
                "event_type": "x",
                "actor": "codex",
                "idempotency_key": "stable-key",
                "expected_task_version": 1,
            },
            headers=AUTH,
        )
    assert response.status_code == 409


def test_add_event_requires_idempotency_and_expected_version(api_key_env):
    response = client.post(
        f"/harness/tasks/{TASK_ID}/events",
        json={"event_type": "worker_started", "actor": "claude"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_passed_status_requires_protected_acceptance_endpoint(api_key_env):
    with patch(
        "apps.api.routers.harness.HarnessStore.update_status",
        AsyncMock(),
    ) as update_status:
        response = client.patch(
            f"/harness/tasks/{TASK_ID}",
            json={
                "status": "passed",
                "actor": "codex",
                "expected_task_version": 1,
            },
            headers=AUTH,
        )
    assert response.status_code == 403
    update_status.assert_not_awaited()


def test_generic_event_endpoint_rejects_protected_decision(api_key_env):
    with patch(
        "apps.api.routers.harness.HarnessStore.add_event",
        AsyncMock(),
    ) as add_event:
        response = client.post(
            f"/harness/tasks/{TASK_ID}/events",
            json={
                "event_type": "accepted",
                "actor": "codex",
                "classification": "decision",
                "idempotency_key": "acceptance-record-1",
                "expected_task_version": 1,
            },
            headers=AUTH,
        )
    assert response.status_code == 403
    add_event.assert_not_awaited()


def test_acceptance_checkpoint_forwards_cas_and_evidence(api_key_env):
    from brain.database.harness_models import AgentTaskEvent

    event = AgentTaskEvent(
        id=10,
        task_id=TASK_ID,
        event_type="acceptance_checkpoint",
        actor="codex",
        classification="decision",
        idempotency_key="acceptance-checkpoint-attempt-1",
        expected_task_version=4,
        task_version=5,
        payload_json={"attempt_id": "attempt-1"},
    )
    with patch(
        "apps.api.routers.harness.HarnessStore.add_acceptance_checkpoint",
        AsyncMock(return_value=event),
    ) as checkpoint:
        response = client.post(
            f"/harness/tasks/{TASK_ID}/acceptance-checkpoints",
            json={
                "attempt_id": "attempt-1",
                "external_task_id": "NOSTIA-TEST",
                "authority": "codex",
                "idempotency_key": "acceptance-checkpoint-attempt-1",
                "expected_task_version": 4,
                "contract_sha256": "1" * 64,
                "gate_report_sha256": "2" * 64,
                "ledger_sha256": "3" * 64,
                "brain_context_sha256": "4" * 64,
                "result_commit": "5" * 40,
                "repository_fingerprint": "6" * 64,
                "fencing_token": 7,
            },
            headers=AUTH,
        )
    assert response.status_code == 200
    assert response.json()["task_version"] == 5
    assert checkpoint.await_args.kwargs["expected_task_version"] == 4
    assert checkpoint.await_args.kwargs["evidence"]["fencing_token"] == 7


def test_acceptance_finalize_forwards_signed_record(api_key_env):
    from brain.database.harness_models import AgentTaskEvent

    event = AgentTaskEvent(
        id=11,
        task_id=TASK_ID,
        event_type="acceptance_finalized",
        actor="codex",
        classification="decision",
        expected_task_version=5,
        task_version=6,
        causal_parent_id=10,
        payload_json={"checkpoint_id": 10},
    )
    with patch(
        "apps.api.routers.harness.HarnessStore.finalize_acceptance_checkpoint",
        AsyncMock(return_value=event),
    ) as finalize:
        response = client.post(
            f"/harness/tasks/{TASK_ID}/acceptance-checkpoints/10/finalize",
            json={
                "expected_task_version": 5,
                "acceptance_record": {
                    "task_id": "NOSTIA-TEST",
                    "attempt_id": "attempt-1",
                    "contract_sha256": "1" * 64,
                    "gate_report_sha256": "2" * 64,
                    "brain_context_sha256": "4" * 64,
                    "result_commit": "5" * 40,
                    "repository_fingerprint": "6" * 64,
                    "brain_checkpoint_id": 10,
                    "brain_checkpoint_version": 5,
                    "ledger_sha256": "3" * 64,
                    "fencing_token": 9,
                    "accepted_at": "2026-07-17T08:00:00+00:00",
                    "authority": "codex",
                    "record_sha256": "a" * 64,
                    "signature": "b" * 128,
                },
            },
            headers=AUTH,
        )
    assert response.status_code == 200
    assert response.json()["event_type"] == "acceptance_finalized"
    assert finalize.await_args.kwargs["checkpoint_id"] == 10
    assert finalize.await_args.kwargs["expected_task_version"] == 5


def test_acquire_lease_conflict_returns_409(api_key_env):
    with patch(
        "apps.api.routers.harness.HarnessStore.acquire_lease",
        AsyncMock(side_effect=LeaseConflict("owned")),
    ):
        response = client.post(
            f"/harness/tasks/{TASK_ID}/leases",
            json={"worker_name": "devin", "ttl_seconds": 60},
            headers=AUTH,
        )
    assert response.status_code == 409
