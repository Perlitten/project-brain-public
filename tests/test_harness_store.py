import datetime
import hashlib
import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from brain.database.harness_models import AgentTask, AgentTaskEvent, WorkerLease
from brain.memory.harness_store import (
    AcceptanceLocked,
    ActiveBlockers,
    HarnessStore,
    IdempotencyConflict,
    InvalidTransition,
    LeaseConflict,
    TaskNotFound,
    VersionConflict,
)

TEST_ACCEPTANCE_PRIVATE = Ed25519PrivateKey.from_private_bytes(
    hashlib.sha256(b"test-only-acceptance-key-32-bytes!!").digest()
)
TEST_ACCEPTANCE_PUBLIC_HEX = (
    TEST_ACCEPTANCE_PRIVATE.public_key()
    .public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    .hex()
)


@pytest.fixture(autouse=True)
def _configured_acceptance_public_key(monkeypatch):
    monkeypatch.setenv(
        "NOSTIA_HARNESS_ACCEPTANCE_PUBLIC_KEY",
        TEST_ACCEPTANCE_PUBLIC_HEX,
    )


def _signed_acceptance_record() -> dict:
    payload = {
        "schema_version": 1,
        "task_id": "NOSTIA-TEST",
        "attempt_id": "attempt-1",
        "contract_sha256": "1" * 64,
        "gate_report_sha256": "2" * 64,
        "brain_context_sha256": "4" * 64,
        "result_commit": "5" * 40,
        "repository_fingerprint": "6" * 64,
        "brain_checkpoint_id": 99,
        "brain_checkpoint_version": 5,
        "ledger_sha256": "3" * 64,
        "fencing_token": 9,
        "accepted_at": "2026-07-17T08:00:00+00:00",
        "authority": "codex",
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    message = json.dumps(
        payload | {"record_sha256": digest},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {key: value for key, value in payload.items() if key != "schema_version"} | {
        "record_sha256": digest,
        "signature": TEST_ACCEPTANCE_PRIVATE.sign(message).hex(),
    }


def _mock_session_factory():
    mock_session = AsyncMock()
    # session.add is synchronous on AsyncSession; a bare AsyncMock would return
    # an unawaited coroutine and emit RuntimeWarning.
    mock_session.add = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session
    return mock_session, mock_session_factory


@pytest.mark.asyncio
async def test_create_task_sets_defaults():
    mock_session, mock_session_factory = _mock_session_factory()
    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        task = await HarnessStore.create_task(
            title="fix flaky test",
            goal="stabilize CI",
            repo_path="/tmp/repo",
            owner_agent="claude",
        )
    assert task.status == "created"
    assert task.repo_path == "/tmp/repo"
    assert mock_session.add.call_count == 2  # AgentTask + creation event
    mock_session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_update_status_rejects_invalid_transition():
    task_id = uuid.uuid4()
    existing = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="created",
        repo_path="/tmp",
        version=0,
    )

    mock_session, mock_session_factory = _mock_session_factory()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(InvalidTransition):
            # "created" cannot jump straight to "passed"
            await HarnessStore.update_status(
                task_id,
                "passed",
                actor="claude",
                expected_task_version=0,
            )


@pytest.mark.asyncio
async def test_update_status_allows_valid_transition():
    task_id = uuid.uuid4()
    existing = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="created",
        repo_path="/tmp",
        version=0,
    )

    mock_session, mock_session_factory = _mock_session_factory()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    no_checkpoint = MagicMock()
    no_checkpoint.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(side_effect=[mock_result, no_checkpoint])

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        task = await HarnessStore.update_status(
            task_id,
            "routed",
            actor="claude",
            expected_task_version=0,
        )
    assert task.status == "routed"
    mock_session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_update_status_raises_task_not_found():
    mock_session, mock_session_factory = _mock_session_factory()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(TaskNotFound):
            await HarnessStore.update_status(
                uuid.uuid4(),
                "routed",
                actor="claude",
                expected_task_version=0,
            )


@pytest.mark.asyncio
async def test_add_artifact_requires_existing_task():
    mock_session, mock_session_factory = _mock_session_factory()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # task lookup misses
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(TaskNotFound):
            await HarnessStore.add_artifact(uuid.uuid4(), kind="log", path_or_uri="/tmp/x.log")


@pytest.mark.asyncio
async def test_add_event_returns_existing_idempotent_operation():
    task_id = uuid.uuid4()
    existing = AgentTaskEvent(
        id=1,
        task_id=task_id,
        event_type="completed",
        actor="claude",
        classification="observation",
        idempotency_key="stable-operation-key",
        expected_task_version=1,
        task_version=2,
    )
    mock_session, mock_session_factory = _mock_session_factory()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        result = await HarnessStore.add_event(
            task_id,
            "completed",
            "claude",
            idempotency_key="stable-operation-key",
            expected_task_version=1,
        )
    assert result is existing
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_event_rejects_idempotency_key_reuse_with_different_content():
    task_id = uuid.uuid4()
    existing = AgentTaskEvent(
        id=1,
        task_id=task_id,
        event_type="completed",
        actor="claude",
        classification="claim",
        idempotency_key="stable-operation-key",
        expected_task_version=1,
        task_version=2,
        payload_json={"status": "completed"},
    )
    mock_session, mock_session_factory = _mock_session_factory()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(IdempotencyConflict):
            await HarnessStore.add_event(
                task_id,
                "completed",
                "attacker",
                payload={"status": "different"},
                classification="claim",
                idempotency_key="stable-operation-key",
                expected_task_version=1,
            )
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_event_idempotency_binds_causal_parent():
    task_id = uuid.uuid4()
    existing = AgentTaskEvent(
        id=1,
        task_id=task_id,
        event_type="completed",
        actor="claude",
        classification="observation",
        idempotency_key="stable-operation-key",
        causal_parent_id=7,
        expected_task_version=1,
        task_version=2,
    )
    mock_session, mock_session_factory = _mock_session_factory()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(IdempotencyConflict):
            await HarnessStore.add_event(
                task_id,
                "completed",
                "claude",
                idempotency_key="stable-operation-key",
                causal_parent_id=8,
                expected_task_version=1,
            )
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_event_rejects_stale_expected_version():
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="created",
        repo_path="/tmp",
        version=4,
    )
    no_existing = MagicMock()
    no_existing.scalar_one_or_none.return_value = None
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    no_checkpoint = MagicMock()
    no_checkpoint.scalar_one_or_none.return_value = None
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(side_effect=[no_existing, task_result, no_checkpoint])

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(VersionConflict):
            await HarnessStore.add_event(
                task_id,
                "completed",
                "claude",
                idempotency_key="stable-operation-key",
                expected_task_version=3,
            )
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_acceptance_checkpoint_rejects_active_codex_blocker():
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="validating",
        repo_path="/tmp",
        version=4,
    )
    blocker = AgentTaskEvent(
        id=7,
        task_id=task_id,
        event_type="remediation_required",
        actor="codex",
        classification="decision",
        payload_json={"blocks_progress": True},
    )
    no_existing = MagicMock()
    no_existing.scalar_one_or_none.return_value = None
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    events_result = MagicMock()
    events_result.scalars.return_value.all.return_value = [blocker]
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(side_effect=[no_existing, task_result, events_result])

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(ActiveBlockers):
            await HarnessStore.add_acceptance_checkpoint(
                task_id,
                attempt_id="attempt-1",
                authority="codex",
                idempotency_key="acceptance-checkpoint-attempt-1",
                expected_task_version=4,
                evidence={"gate_report_sha256": "1" * 64},
            )
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_acceptance_checkpoint_atomically_marks_task_pending():
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="validating",
        repo_path="/tmp",
        version=4,
    )
    no_existing = MagicMock()
    no_existing.scalar_one_or_none.return_value = None
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    events_result = MagicMock()
    events_result.scalars.return_value.all.return_value = []
    lease_result = MagicMock()
    lease_result.scalar_one_or_none.return_value = WorkerLease(
        id=1,
        task_id=task_id,
        worker_name="codex-acceptance:attempt-1",
        lease_expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=2),
        fencing_token=17,
    )
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(side_effect=[no_existing, task_result, events_result, lease_result])

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        event = await HarnessStore.add_acceptance_checkpoint(
            task_id,
            attempt_id="attempt-1",
            authority="codex",
            idempotency_key="acceptance-checkpoint-attempt-1",
            expected_task_version=4,
            evidence={"gate_report_sha256": "1" * 64, "fencing_token": 17},
        )

    assert task.status == "acceptance_pending"
    assert task.version == 5
    assert event.task_version == 5
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_acceptance_checkpoint_fails_before_mutation_when_public_key_missing(
    monkeypatch,
):
    from brain.memory.harness_store import AcceptanceUnavailable

    monkeypatch.delenv("NOSTIA_HARNESS_ACCEPTANCE_PUBLIC_KEY")
    mock_session, mock_session_factory = _mock_session_factory()

    with (
        patch("brain.memory.harness_store.async_session_factory", mock_session_factory),
        pytest.raises(AcceptanceUnavailable),
    ):
        await HarnessStore.add_acceptance_checkpoint(
            uuid.uuid4(),
            attempt_id="attempt-1",
            authority="codex",
            idempotency_key="acceptance-checkpoint-attempt-1",
            expected_task_version=4,
            evidence={"gate_report_sha256": "1" * 64, "fencing_token": 17},
        )

    mock_session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_acceptance_finalize_atomically_marks_task_passed():
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="acceptance_pending",
        repo_path="/tmp",
        version=5,
    )
    checkpoint = AgentTaskEvent(
        id=99,
        task_id=task_id,
        event_type="acceptance_checkpoint",
        actor="codex",
        classification="decision",
        task_version=5,
        payload_json={
            "external_task_id": "NOSTIA-TEST",
            "attempt_id": "attempt-1",
            "contract_sha256": "1" * 64,
            "gate_report_sha256": "2" * 64,
            "ledger_sha256": "3" * 64,
            "brain_context_sha256": "4" * 64,
            "result_commit": "5" * 40,
            "repository_fingerprint": "6" * 64,
            "fencing_token": 9,
        },
    )
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    no_finalized = MagicMock()
    no_finalized.scalar_one_or_none.return_value = None
    checkpoint_result = MagicMock()
    checkpoint_result.scalar_one_or_none.return_value = checkpoint
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(side_effect=[task_result, checkpoint_result, no_finalized])

    with (
        patch("brain.memory.harness_store.async_session_factory", mock_session_factory),
        patch.dict(
            os.environ,
            {"NOSTIA_HARNESS_ACCEPTANCE_PUBLIC_KEY": TEST_ACCEPTANCE_PUBLIC_HEX},
        ),
    ):
        event = await HarnessStore.finalize_acceptance_checkpoint(
            task_id,
            checkpoint_id=99,
            expected_task_version=5,
            acceptance_record=_signed_acceptance_record(),
        )

    assert task.status == "passed"
    assert task.version == 6
    assert event.event_type == "acceptance_finalized"
    assert event.causal_parent_id == 99
    assert event.task_version == 6
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_acceptance_finalize_rejects_forged_signature():
    checkpoint = AgentTaskEvent(
        id=99,
        task_id=uuid.uuid4(),
        event_type="acceptance_checkpoint",
        actor="codex",
        classification="decision",
        task_version=5,
        payload_json={
            "external_task_id": "NOSTIA-TEST",
            "attempt_id": "attempt-1",
            "contract_sha256": "1" * 64,
            "gate_report_sha256": "2" * 64,
            "ledger_sha256": "3" * 64,
            "brain_context_sha256": "4" * 64,
            "result_commit": "5" * 40,
            "repository_fingerprint": "6" * 64,
            "fencing_token": 9,
        },
    )
    forged = _signed_acceptance_record() | {"signature": "0" * 128}
    from brain.memory.harness_store import AcceptanceAttestationInvalid

    with (
        patch.dict(
            os.environ,
            {"NOSTIA_HARNESS_ACCEPTANCE_PUBLIC_KEY": TEST_ACCEPTANCE_PUBLIC_HEX},
        ),
        pytest.raises(AcceptanceAttestationInvalid),
    ):
        HarnessStore._verify_acceptance_attestation(
            forged,
            checkpoint=checkpoint,
        )


def test_acceptance_finalize_rejects_mismatched_fencing_token():
    checkpoint = AgentTaskEvent(
        id=99,
        task_id=uuid.uuid4(),
        event_type="acceptance_checkpoint",
        actor="codex",
        classification="decision",
        task_version=5,
        payload_json={
            "external_task_id": "NOSTIA-TEST",
            "attempt_id": "attempt-1",
            "contract_sha256": "1" * 64,
            "gate_report_sha256": "2" * 64,
            "ledger_sha256": "3" * 64,
            "brain_context_sha256": "4" * 64,
            "result_commit": "5" * 40,
            "repository_fingerprint": "6" * 64,
            "fencing_token": 10,
        },
    )
    from brain.memory.harness_store import AcceptanceAttestationInvalid

    with (
        patch.dict(
            os.environ,
            {"NOSTIA_HARNESS_ACCEPTANCE_PUBLIC_KEY": TEST_ACCEPTANCE_PUBLIC_HEX},
        ),
        pytest.raises(AcceptanceAttestationInvalid),
    ):
        HarnessStore._verify_acceptance_attestation(
            _signed_acceptance_record(),
            checkpoint=checkpoint,
        )


@pytest.mark.asyncio
async def test_acceptance_checkpoint_rejects_stale_fencing_token():
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="validating",
        repo_path="/tmp",
        version=4,
    )
    no_existing = MagicMock()
    no_existing.scalar_one_or_none.return_value = None
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    events_result = MagicMock()
    events_result.scalars.return_value.all.return_value = []
    lease_result = MagicMock()
    lease_result.scalar_one_or_none.return_value = WorkerLease(
        id=1,
        task_id=task_id,
        worker_name="codex-acceptance:attempt-1",
        lease_expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=2),
        fencing_token=18,
    )
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(side_effect=[no_existing, task_result, events_result, lease_result])

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(LeaseConflict):
            await HarnessStore.add_acceptance_checkpoint(
                task_id,
                attempt_id="attempt-1",
                authority="codex",
                idempotency_key="acceptance-checkpoint-attempt-1",
                expected_task_version=4,
                evidence={"gate_report_sha256": "1" * 64, "fencing_token": 17},
            )
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_status_rejects_mutation_after_acceptance_checkpoint():
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        title="t",
        goal="g",
        status="passed",
        repo_path="/tmp",
        version=5,
    )
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    checkpoint_result = MagicMock()
    checkpoint_result.scalar_one_or_none.return_value = 99
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(side_effect=[task_result, checkpoint_result])

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(AcceptanceLocked):
            await HarnessStore.update_status(
                task_id,
                "passed",
                actor="codex",
                expected_task_version=5,
            )
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_lease_rejects_second_live_worker():
    task_id = uuid.uuid4()
    task_exists = MagicMock()
    task_exists.scalar_one_or_none.return_value = task_id
    lease_result = MagicMock()
    lease_result.scalar_one_or_none.return_value = WorkerLease(
        id=1,
        task_id=task_id,
        worker_name="claude",
        lease_expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5),
        fencing_token=1,
    )
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(side_effect=[task_exists, lease_result])

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        with pytest.raises(LeaseConflict):
            await HarnessStore.acquire_lease(task_id, "devin", 60)
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_stale_task_requeues_within_retry_budget_and_releases_expired_lease():
    now = datetime.datetime(2026, 7, 28, 8, 0, tzinfo=datetime.timezone.utc)
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        title="stuck implementation",
        goal="finish it",
        status="running",
        repo_path="/tmp/repo",
        timeout_seconds=300,
        retry_budget=1,
        version=4,
        current_session_id="old-session",
        created_at=now - datetime.timedelta(hours=2),
        updated_at=now - datetime.timedelta(hours=1),
    )
    lease = WorkerLease(
        id=1,
        task_id=task_id,
        worker_name="old-worker",
        lease_started_at=now - datetime.timedelta(hours=1),
        lease_expires_at=now - datetime.timedelta(minutes=30),
        heartbeat_at=now - datetime.timedelta(minutes=45),
        fencing_token=7,
    )

    task_result = MagicMock()
    task_result.scalars.return_value.all.return_value = [task]
    lease_result = MagicMock()
    lease_result.scalars.return_value.all.return_value = [lease]
    retry_result = MagicMock()
    retry_result.all.return_value = []
    checkpoint_result = MagicMock()
    checkpoint_result.scalars.return_value.all.return_value = []
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(
        side_effect=[task_result, lease_result, retry_result, checkpoint_result]
    )

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        result = await HarnessStore.reconcile_stale_tasks(now=now)

    assert result["requeued"] == 1
    assert result["timed_out"] == 0
    assert result["expired_leases_released"] == 1
    assert task.status == "routed"
    assert task.version == 5
    assert task.current_session_id is None
    mock_session.delete.assert_awaited_once_with(lease)
    event = mock_session.add.call_args.args[0]
    assert event.event_type == "stale_recovery_requeued"
    assert event.payload_json["expired_lease_fencing_token"] == 7
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_stale_tasks_times_out_exhausted_work_but_skips_live_lease():
    now = datetime.datetime(2026, 7, 28, 8, 0, tzinfo=datetime.timezone.utc)
    exhausted_id = uuid.uuid4()
    live_id = uuid.uuid4()
    exhausted = AgentTask(
        id=exhausted_id,
        title="retry exhausted",
        goal="finish it",
        status="running",
        repo_path="/tmp/repo",
        timeout_seconds=300,
        retry_budget=1,
        version=2,
        created_at=now - datetime.timedelta(hours=2),
        updated_at=now - datetime.timedelta(hours=1),
    )
    live = AgentTask(
        id=live_id,
        title="worker is alive",
        goal="finish it",
        status="claimed",
        repo_path="/tmp/repo",
        timeout_seconds=300,
        retry_budget=2,
        version=1,
        created_at=now - datetime.timedelta(hours=2),
        updated_at=now - datetime.timedelta(hours=1),
    )
    live_lease = WorkerLease(
        id=2,
        task_id=live_id,
        worker_name="healthy-worker",
        lease_started_at=now - datetime.timedelta(minutes=1),
        lease_expires_at=now + datetime.timedelta(minutes=5),
        heartbeat_at=now,
        fencing_token=2,
    )

    task_result = MagicMock()
    task_result.scalars.return_value.all.return_value = [exhausted, live]
    lease_result = MagicMock()
    lease_result.scalars.return_value.all.return_value = [live_lease]
    retry_result = MagicMock()
    retry_result.all.return_value = [(exhausted_id, 1)]
    checkpoint_result = MagicMock()
    checkpoint_result.scalars.return_value.all.return_value = []
    mock_session, mock_session_factory = _mock_session_factory()
    mock_session.execute = AsyncMock(
        side_effect=[task_result, lease_result, retry_result, checkpoint_result]
    )

    with patch("brain.memory.harness_store.async_session_factory", mock_session_factory):
        result = await HarnessStore.reconcile_stale_tasks(now=now)

    assert result["timed_out"] == 1
    assert result["skipped_live_lease"] == 1
    assert exhausted.status == "timed_out"
    assert live.status == "claimed"
    mock_session.delete.assert_not_awaited()
    event = mock_session.add.call_args.args[0]
    assert event.event_type == "stale_recovery_timed_out"
    assert event.payload_json["retries_used_before"] == 1
