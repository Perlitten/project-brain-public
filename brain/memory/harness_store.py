"""Store for the Claude Code harness task ledger (P1 hardening, 2026-07-07).

Mirrors the shape of brain.memory.decision_store: a thin classmethod wrapper
around async_session_factory, no ORM sessions leak past this module.
"""

import datetime
import hashlib
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import IntegrityError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from brain.database.harness_models import (
    ALLOWED_TRANSITIONS,
    AgentTask,
    AgentTaskArtifact,
    AgentTaskEvent,
    AgentValidationResult,
    MemoryHandoff,
    TERMINAL_STATUSES,
    WorkerLease,
)
from brain.database.session import async_session_factory
from brain.workers.db_fencing import assert_current_db_fence

RUNNING_TASK_STATUSES = ("running", "in_progress", "started", "indexing", "processing", "claimed", "artifacted", "memory_updated", "validating", "acceptance_pending")
QUEUED_TASK_STATUSES = ("queued", "pending", "created", "routed", "scheduled", "waiting")

def _task_status_filter(status: Optional[str]):
    if status == "__running__":
        return AgentTask.status.in_(RUNNING_TASK_STATUSES)
    if status == "__queued__":
        return AgentTask.status.in_(QUEUED_TASK_STATUSES)
    return AgentTask.status == status


class InvalidTransition(ValueError):
    """Raised when a status update doesn't follow the task state machine."""


class TaskNotFound(LookupError):
    pass


class VersionConflict(ValueError):
    """Raised when a client writes against a stale task version."""


class IdempotencyConflict(ValueError):
    """Raised when an idempotency key is reused for different event content."""


class LeaseConflict(ValueError):
    """Raised when another live worker owns the task lease."""


class AcceptanceLocked(ValueError):
    """Raised when a terminal acceptance checkpoint already fences the task."""


class ActiveBlockers(ValueError):
    """Raised when acceptance is attempted with unresolved Codex blockers."""


class AcceptanceAttestationInvalid(ValueError):
    """Raised when finalize evidence is not signed by the acceptance authority."""


class AcceptanceUnavailable(RuntimeError):
    """Raised when protected acceptance is not configured on the server."""


class HarnessStore:
    @classmethod
    async def create_task(
        cls,
        title: str,
        goal: str,
        repo_path: str,
        owner_agent: Optional[str] = None,
        target_agent: Optional[str] = None,
        worktree_path: Optional[str] = None,
        created_by_session_id: Optional[str] = None,
        priority: str = "P2",
        timeout_seconds: int = 900,
        retry_budget: int = 1,
    ) -> AgentTask:
        async with async_session_factory() as session:
            task = AgentTask(
                id=uuid.uuid4(),
                title=title,
                goal=goal,
                status="created",
                owner_agent=owner_agent,
                target_agent=target_agent,
                repo_path=repo_path,
                worktree_path=worktree_path,
                created_by_session_id=created_by_session_id,
                current_session_id=created_by_session_id,
                priority=priority,
                timeout_seconds=timeout_seconds,
                retry_budget=retry_budget,
            )
            session.add(task)
            await session.flush()
            session.add(
                AgentTaskEvent(
                    task_id=task.id,
                    event_type="created",
                    actor=owner_agent or "unknown",
                    payload_json={"title": title},
                )
            )
            await session.commit()
            await session.refresh(task)
            return task

    @classmethod
    async def get_task(cls, task_id: uuid.UUID) -> Optional[AgentTask]:
        async with async_session_factory() as session:
            result = await session.execute(select(AgentTask).where(AgentTask.id == task_id))
            return result.scalar_one_or_none()

    @classmethod
    async def list_tasks(
        cls, status: Optional[str] = None, limit: int = 100,
        *, repo_path: Optional[str] = None, offset: int = 0, query: Optional[str] = None,
    ) -> List[AgentTask]:
        async with async_session_factory() as session:
            stmt = select(AgentTask).order_by(AgentTask.created_at.desc()).offset(max(0, offset)).limit(limit)
            if status:
                stmt = stmt.where(_task_status_filter(status))
            if repo_path:
                stmt = stmt.where(AgentTask.repo_path == repo_path)
            if query:
                from sqlalchemy import or_
                needle = f"%{query}%"
                stmt = stmt.where(or_(cast(AgentTask.id, String).ilike(needle), AgentTask.title.ilike(needle), AgentTask.goal.ilike(needle),
                                      AgentTask.target_agent.ilike(needle), AgentTask.owner_agent.ilike(needle)))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @classmethod
    async def count_tasks(cls, status: Optional[str] = None, *, repo_path: Optional[str] = None,
                          query: Optional[str] = None) -> int:
        from sqlalchemy import func, or_
        async with async_session_factory() as session:
            stmt = select(func.count()).select_from(AgentTask)
            if status:
                stmt = stmt.where(_task_status_filter(status))
            if repo_path:
                stmt = stmt.where(AgentTask.repo_path == repo_path)
            if query:
                needle = f"%{query}%"
                stmt = stmt.where(or_(cast(AgentTask.id, String).ilike(needle), AgentTask.title.ilike(needle), AgentTask.goal.ilike(needle),
                                      AgentTask.target_agent.ilike(needle), AgentTask.owner_agent.ilike(needle)))
            return int((await session.execute(stmt)).scalar() or 0)

    @classmethod
    async def task_status_counts(cls, *, repo_path: Optional[str] = None,
                                 query: Optional[str] = None) -> dict[str, int]:
        """Full status facets in one query, before a requested status filter."""
        from sqlalchemy import or_
        async with async_session_factory() as session:
            stmt = select(AgentTask.status, func.count()).group_by(AgentTask.status)
            if repo_path:
                stmt = stmt.where(AgentTask.repo_path == repo_path)
            if query:
                needle = f"%{query}%"
                stmt = stmt.where(or_(cast(AgentTask.id, String).ilike(needle), AgentTask.title.ilike(needle), AgentTask.goal.ilike(needle),
                                      AgentTask.target_agent.ilike(needle), AgentTask.owner_agent.ilike(needle)))
            rows = (await session.execute(stmt)).all()
            return {str(status): int(count) for status, count in rows}

    @classmethod
    async def reconcile_stale_tasks(
        cls,
        *,
        limit: int = 500,
        now: Optional[datetime.datetime] = None,
    ) -> dict:
        """Recover or close non-terminal tasks whose lifecycle deadline expired.

        The reaper is intentionally conservative:
        - a live worker lease always wins;
        - acceptance-fenced tasks are never mutated;
        - only routed/claimed/running work is eligible for an automatic retry;
        - each retry consumes one unit from ``retry_budget``;
        - later-stage or exhausted work becomes ``timed_out`` instead of
          remaining a false-active task forever.
        """

        checked_at = now or datetime.datetime.now(datetime.timezone.utc)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=datetime.timezone.utc)
        batch_size = max(1, min(int(limit), 5000))
        active_statuses = tuple(
            status
            for status in ALLOWED_TRANSITIONS
            if status not in TERMINAL_STATUSES and status != "acceptance_pending"
        )

        summary: Dict[str, Any] = {
            "checked_at": checked_at.isoformat(),
            "inspected": 0,
            "stale": 0,
            "requeued": 0,
            "timed_out": 0,
            "skipped_live_lease": 0,
            "skipped_acceptance": 0,
            "expired_leases_released": 0,
            "requeued_task_ids": [],
            "timed_out_task_ids": [],
        }

        async with async_session_factory() as session:
            task_result = await session.execute(
                select(AgentTask)
                .where(AgentTask.status.in_(active_statuses))
                .order_by(AgentTask.updated_at.asc(), AgentTask.created_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            tasks = list(task_result.scalars().all())
            summary["inspected"] = len(tasks)
            if not tasks:
                return summary

            stale_tasks: list[AgentTask] = []
            for task in tasks:
                touched = task.updated_at or task.created_at
                if touched is None:
                    continue
                if touched.tzinfo is None:
                    touched = touched.replace(tzinfo=datetime.timezone.utc)
                timeout_seconds = max(int(task.timeout_seconds or 900), 60)
                if (checked_at - touched).total_seconds() > timeout_seconds:
                    stale_tasks.append(task)
            summary["stale"] = len(stale_tasks)
            if not stale_tasks:
                return summary

            task_ids = [task.id for task in stale_tasks]
            lease_result = await session.execute(
                select(WorkerLease).where(WorkerLease.task_id.in_(task_ids)).with_for_update(skip_locked=True)
            )
            leases = {lease.task_id: lease for lease in lease_result.scalars().all()}

            retry_result = await session.execute(
                select(AgentTaskEvent.task_id, func.count(AgentTaskEvent.id))
                .where(
                    AgentTaskEvent.task_id.in_(task_ids),
                    AgentTaskEvent.event_type == "stale_recovery_requeued",
                )
                .group_by(AgentTaskEvent.task_id)
            )
            retry_counts = {task_id: int(count) for task_id, count in retry_result.all()}

            checkpoint_result = await session.execute(
                select(AgentTaskEvent.task_id)
                .where(
                    AgentTaskEvent.task_id.in_(task_ids),
                    AgentTaskEvent.event_type == "acceptance_checkpoint",
                )
                .distinct()
            )
            checkpoint_task_ids = set(checkpoint_result.scalars().all())

            for task in stale_tasks:
                if task.id in checkpoint_task_ids:
                    summary["skipped_acceptance"] += 1
                    continue

                lease = leases.get(task.id)
                if lease is not None:
                    lease_expires_at = lease.lease_expires_at
                    if lease_expires_at.tzinfo is None:
                        lease_expires_at = lease_expires_at.replace(tzinfo=datetime.timezone.utc)
                    if lease_expires_at > checked_at:
                        summary["skipped_live_lease"] += 1
                        continue

                old_status = task.status
                retries_used = retry_counts.get(task.id, 0)
                retry_budget = max(int(task.retry_budget or 0), 0)
                should_requeue = old_status in {"routed", "claimed", "running"} and retries_used < retry_budget
                new_status = "routed" if should_requeue else "timed_out"

                previous_version = int(task.version or 0)
                task.status = new_status
                task.version = previous_version + 1
                task.updated_at = checked_at
                task.current_session_id = None

                if lease is not None:
                    await session.delete(lease)
                    summary["expired_leases_released"] += 1

                event_type = "stale_recovery_requeued" if should_requeue else "stale_recovery_timed_out"
                event_payload = {
                    "from": old_status,
                    "to": new_status,
                    "timeout_seconds": max(int(task.timeout_seconds or 900), 60),
                    "retry_budget": retry_budget,
                    "retries_used_before": retries_used,
                    "expired_lease_worker": lease.worker_name if lease is not None else None,
                    "expired_lease_fencing_token": lease.fencing_token if lease is not None else None,
                }
                session.add(
                    AgentTaskEvent(
                        task_id=task.id,
                        event_type=event_type,
                        actor="lifecycle-reaper",
                        classification="decision",
                        idempotency_key=f"lifecycle-reaper:{previous_version}:{event_type}",
                        expected_task_version=previous_version,
                        task_version=task.version,
                        payload_json=event_payload,
                    )
                )
                result_key = "requeued" if should_requeue else "timed_out"
                result_ids_key = "requeued_task_ids" if should_requeue else "timed_out_task_ids"
                summary[result_key] += 1
                if len(summary[result_ids_key]) < 20:
                    summary[result_ids_key].append(str(task.id))

            await assert_current_db_fence(session)
            await session.commit()
            return summary

    @classmethod
    async def update_status(
        cls,
        task_id: uuid.UUID,
        new_status: str,
        actor: str,
        expected_task_version: int,
        current_session_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> AgentTask:
        async with async_session_factory() as session:
            result = await session.execute(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
            task = result.scalar_one_or_none()
            if task is None:
                raise TaskNotFound(str(task_id))
            if expected_task_version != task.version:
                raise VersionConflict(f"task {task_id} version is {task.version}, expected {expected_task_version}")
            allowed = ALLOWED_TRANSITIONS.get(task.status, set())
            if new_status != task.status and new_status not in allowed:
                raise InvalidTransition(
                    f"cannot move task {task_id} from '{task.status}' to '{new_status}' "
                    f"(allowed: {sorted(allowed) or 'none — terminal state'})"
                )

            checkpoint_result = await session.execute(
                select(AgentTaskEvent.id)
                .where(
                    AgentTaskEvent.task_id == task_id,
                    AgentTaskEvent.event_type == "acceptance_checkpoint",
                )
                .limit(1)
            )
            if checkpoint_result.scalar_one_or_none() is not None:
                raise AcceptanceLocked(f"task {task_id} is fenced by an acceptance checkpoint")

            old_status = task.status
            task.status = new_status
            task.version += 1
            if current_session_id:
                task.current_session_id = current_session_id
            if new_status == "cancelled" and task.cancellation_requested_at is None:
                task.cancellation_requested_at = datetime.datetime.now(datetime.timezone.utc)

            session.add(
                AgentTaskEvent(
                    task_id=task.id,
                    event_type="status_change",
                    actor=actor,
                    classification="decision",
                    expected_task_version=expected_task_version,
                    task_version=task.version,
                    payload_json={"from": old_status, "to": new_status, "note": note},
                )
            )
            await session.commit()
            await session.refresh(task)
            return task

    @classmethod
    async def add_event(
        cls,
        task_id: uuid.UUID,
        event_type: str,
        actor: str,
        payload: Optional[dict] = None,
        *,
        classification: str = "observation",
        idempotency_key: Optional[str] = None,
        causal_parent_id: Optional[int] = None,
        expected_task_version: Optional[int] = None,
    ) -> AgentTaskEvent:
        async with async_session_factory() as session:
            if idempotency_key:
                existing_result = await session.execute(
                    select(AgentTaskEvent).where(
                        AgentTaskEvent.task_id == task_id,
                        AgentTaskEvent.idempotency_key == idempotency_key,
                    )
                )
                existing = existing_result.scalar_one_or_none()
                if existing is not None:
                    cls._assert_idempotent_event_match(
                        existing,
                        event_type=event_type,
                        actor=actor,
                        classification=classification,
                        payload=payload,
                        expected_task_version=expected_task_version,
                        causal_parent_id=causal_parent_id,
                    )
                    return existing

            task_result = await session.execute(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
            task = task_result.scalar_one_or_none()
            if task is None:
                raise TaskNotFound(str(task_id))
            checkpoint_result = await session.execute(
                select(AgentTaskEvent.id)
                .where(
                    AgentTaskEvent.task_id == task_id,
                    AgentTaskEvent.event_type == "acceptance_checkpoint",
                )
                .limit(1)
            )
            if checkpoint_result.scalar_one_or_none() is not None:
                raise AcceptanceLocked(f"task {task_id} is fenced by an acceptance checkpoint")
            if expected_task_version is not None and expected_task_version != task.version:
                raise VersionConflict(f"task {task_id} version is {task.version}, expected {expected_task_version}")

            next_version = task.version + 1
            event = AgentTaskEvent(
                task_id=task_id,
                event_type=event_type,
                actor=actor,
                classification=classification,
                idempotency_key=idempotency_key,
                causal_parent_id=causal_parent_id,
                expected_task_version=expected_task_version,
                task_version=next_version,
                payload_json=payload,
            )
            task.version = next_version
            session.add(event)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if not idempotency_key:
                    raise
                duplicate_result = await session.execute(
                    select(AgentTaskEvent).where(
                        AgentTaskEvent.task_id == task_id,
                        AgentTaskEvent.idempotency_key == idempotency_key,
                    )
                )
                duplicate = duplicate_result.scalar_one_or_none()
                if duplicate is None:
                    raise
                cls._assert_idempotent_event_match(
                    duplicate,
                    event_type=event_type,
                    actor=actor,
                    classification=classification,
                    payload=payload,
                    expected_task_version=expected_task_version,
                    causal_parent_id=causal_parent_id,
                )
                return duplicate
            await session.refresh(event)
            return event

    @classmethod
    async def add_acceptance_checkpoint(
        cls,
        task_id: uuid.UUID,
        *,
        attempt_id: str,
        authority: str,
        idempotency_key: str,
        expected_task_version: int,
        evidence: dict,
    ) -> AgentTaskEvent:
        cls._acceptance_public_key()
        async with async_session_factory() as session:
            existing_result = await session.execute(
                select(AgentTaskEvent).where(
                    AgentTaskEvent.task_id == task_id,
                    AgentTaskEvent.idempotency_key == idempotency_key,
                )
            )
            existing = existing_result.scalar_one_or_none()
            payload = {"attempt_id": attempt_id, **evidence}
            if existing is not None:
                cls._assert_idempotent_event_match(
                    existing,
                    event_type="acceptance_checkpoint",
                    actor=authority,
                    classification="decision",
                    payload=payload,
                    expected_task_version=expected_task_version,
                    causal_parent_id=None,
                )
                return existing

            task_result = await session.execute(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
            task = task_result.scalar_one_or_none()
            if task is None:
                raise TaskNotFound(str(task_id))
            if task.version != expected_task_version:
                raise VersionConflict(f"task {task_id} version is {task.version}, expected {expected_task_version}")
            if task.status != "validating":
                raise InvalidTransition(
                    f"acceptance checkpoint requires task {task_id} to be validating, got '{task.status}'"
                )
            events_result = await session.execute(
                select(AgentTaskEvent).where(AgentTaskEvent.task_id == task_id).order_by(AgentTaskEvent.id.asc())
            )
            events = list(events_result.scalars().all())
            if any(event.event_type == "acceptance_checkpoint" for event in events):
                raise AcceptanceLocked(f"task {task_id} already has an acceptance checkpoint")
            if cls._active_blocker_ids(events):
                raise ActiveBlockers(f"task {task_id} has active blockers")
            lease_result = await session.execute(
                select(WorkerLease).where(WorkerLease.task_id == task_id).with_for_update()
            )
            lease = lease_result.scalar_one_or_none()
            now = datetime.datetime.now(datetime.timezone.utc)
            if (
                lease is None
                or lease.lease_expires_at <= now
                or lease.worker_name != f"{authority}-acceptance:{attempt_id}"
                or lease.fencing_token != evidence.get("fencing_token")
            ):
                raise LeaseConflict(f"task {task_id} acceptance lease is missing, expired, or stale")
            next_version = task.version + 1
            event = AgentTaskEvent(
                task_id=task_id,
                event_type="acceptance_checkpoint",
                actor=authority,
                classification="decision",
                idempotency_key=idempotency_key,
                expected_task_version=expected_task_version,
                task_version=next_version,
                payload_json=payload,
            )
            task.version = next_version
            task.status = "acceptance_pending"
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    @classmethod
    async def finalize_acceptance_checkpoint(
        cls,
        task_id: uuid.UUID,
        *,
        checkpoint_id: int,
        expected_task_version: int,
        acceptance_record: dict,
    ) -> AgentTaskEvent:
        payload = {
            "checkpoint_id": checkpoint_id,
            "acceptance_record_sha256": acceptance_record.get("record_sha256"),
            "signature": acceptance_record.get("signature"),
        }
        async with async_session_factory() as session:
            task_result = await session.execute(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
            task = task_result.scalar_one_or_none()
            if task is None:
                raise TaskNotFound(str(task_id))
            checkpoint_result = await session.execute(
                select(AgentTaskEvent).where(
                    AgentTaskEvent.id == checkpoint_id,
                    AgentTaskEvent.task_id == task_id,
                    AgentTaskEvent.event_type == "acceptance_checkpoint",
                )
            )
            checkpoint = checkpoint_result.scalar_one_or_none()
            if checkpoint is None:
                raise AcceptanceLocked(f"acceptance checkpoint {checkpoint_id} does not belong to task")
            cls._verify_acceptance_attestation(
                acceptance_record,
                checkpoint=checkpoint,
            )
            finalized_result = await session.execute(
                select(AgentTaskEvent)
                .where(
                    AgentTaskEvent.task_id == task_id,
                    AgentTaskEvent.event_type == "acceptance_finalized",
                )
                .limit(1)
            )
            finalized = finalized_result.scalar_one_or_none()
            if finalized is not None:
                if finalized.payload_json != payload:
                    raise IdempotencyConflict("acceptance checkpoint was finalized with different content")
                return finalized
            if task.version != expected_task_version:
                raise VersionConflict(f"task {task_id} version is {task.version}, expected {expected_task_version}")
            if task.status != "acceptance_pending":
                raise InvalidTransition(f"acceptance finalize requires pending task, got '{task.status}'")
            next_version = task.version + 1
            event = AgentTaskEvent(
                task_id=task_id,
                event_type="acceptance_finalized",
                actor=checkpoint.actor,
                classification="decision",
                idempotency_key=f"acceptance-finalize:{checkpoint_id}",
                expected_task_version=expected_task_version,
                task_version=next_version,
                causal_parent_id=checkpoint_id,
                payload_json=payload,
            )
            task.version = next_version
            task.status = "passed"
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    @staticmethod
    def _verify_acceptance_attestation(
        record: dict,
        *,
        checkpoint: AgentTaskEvent,
    ) -> None:
        checkpoint_payload = checkpoint.payload_json
        if not isinstance(checkpoint_payload, dict):
            raise AcceptanceAttestationInvalid("checkpoint evidence is invalid")
        expected = {
            "task_id": checkpoint_payload.get("external_task_id"),
            "attempt_id": checkpoint_payload.get("attempt_id"),
            "contract_sha256": checkpoint_payload.get("contract_sha256"),
            "gate_report_sha256": checkpoint_payload.get("gate_report_sha256"),
            "brain_context_sha256": checkpoint_payload.get("brain_context_sha256"),
            "result_commit": checkpoint_payload.get("result_commit"),
            "repository_fingerprint": checkpoint_payload.get("repository_fingerprint"),
            "brain_checkpoint_id": checkpoint.id,
            "brain_checkpoint_version": checkpoint.task_version,
            "ledger_sha256": checkpoint_payload.get("ledger_sha256"),
            "fencing_token": checkpoint_payload.get("fencing_token"),
            "authority": checkpoint.actor,
        }
        if any(record.get(field) != value for field, value in expected.items()):
            raise AcceptanceAttestationInvalid("signed acceptance differs from checkpoint evidence")
        signed_payload = {
            "schema_version": 1,
            "task_id": record.get("task_id"),
            "attempt_id": record.get("attempt_id"),
            "contract_sha256": record.get("contract_sha256"),
            "gate_report_sha256": record.get("gate_report_sha256"),
            "brain_context_sha256": record.get("brain_context_sha256"),
            "result_commit": record.get("result_commit"),
            "repository_fingerprint": record.get("repository_fingerprint"),
            "brain_checkpoint_id": record.get("brain_checkpoint_id"),
            "brain_checkpoint_version": record.get("brain_checkpoint_version"),
            "ledger_sha256": record.get("ledger_sha256"),
            "fencing_token": record.get("fencing_token"),
            "accepted_at": record.get("accepted_at"),
            "authority": record.get("authority"),
        }
        canonical = json.dumps(
            signed_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if digest != record.get("record_sha256"):
            raise AcceptanceAttestationInvalid("acceptance record digest is invalid")
        public_key = HarnessStore._acceptance_public_key()
        try:
            signature = bytes.fromhex(str(record.get("signature") or ""))
            message = json.dumps(
                signed_payload | {"record_sha256": digest},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            public_key.verify(signature, message)
        except (ValueError, InvalidSignature) as exc:
            raise AcceptanceAttestationInvalid("acceptance Ed25519 signature is invalid") from exc

    @staticmethod
    def _acceptance_public_key() -> Ed25519PublicKey:
        public_hex = os.environ.get("NOSTIA_HARNESS_ACCEPTANCE_PUBLIC_KEY", "")
        try:
            return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        except ValueError as exc:
            raise AcceptanceUnavailable(
                "protected acceptance is unavailable because the server public key is not configured"
            ) from exc

    @staticmethod
    def _active_blocker_ids(events: List[AgentTaskEvent]) -> set[int]:
        active: set[int] = set()
        for event in events:
            payload = event.payload_json
            if (
                event.actor not in {"codex", "human"}
                or event.classification != "decision"
                or not isinstance(payload, dict)
            ):
                continue
            resolved = payload.get("resolves_blocker_event_ids", [])
            if isinstance(resolved, list):
                active.difference_update(event_id for event_id in resolved if isinstance(event_id, int))
            if payload.get("blocks_progress") is True and isinstance(event.id, int):
                active.add(event.id)
        return active

    @staticmethod
    def _assert_idempotent_event_match(
        existing: AgentTaskEvent,
        *,
        event_type: str,
        actor: str,
        classification: str,
        payload: Optional[dict],
        expected_task_version: Optional[int],
        causal_parent_id: Optional[int],
    ) -> None:
        if (
            existing.event_type != event_type
            or existing.actor != actor
            or existing.classification != classification
            or existing.payload_json != payload
            or existing.expected_task_version != expected_task_version
            or existing.causal_parent_id != causal_parent_id
        ):
            raise IdempotencyConflict("idempotency key is already bound to different event content")

    @classmethod
    async def list_events(
        cls,
        task_id: uuid.UUID,
        limit: int = 200,
        after_id: Optional[int] = None,
    ) -> List[AgentTaskEvent]:
        async with async_session_factory() as session:
            stmt = select(AgentTaskEvent).where(AgentTaskEvent.task_id == task_id)
            if after_id is not None:
                stmt = stmt.where(AgentTaskEvent.id > after_id).order_by(AgentTaskEvent.id.asc()).limit(limit)
            else:
                stmt = stmt.order_by(AgentTaskEvent.id.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            return rows if after_id is not None else list(reversed(rows))

    @classmethod
    async def add_artifact(
        cls,
        task_id: uuid.UUID,
        kind: str,
        path_or_uri: str,
        checksum: Optional[str] = None,
        content_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        compressed_bytes: Optional[int] = None,
        redaction_status: str = "unknown",
    ) -> AgentTaskArtifact:
        async with async_session_factory() as session:
            exists = await session.execute(select(AgentTask.id).where(AgentTask.id == task_id))
            if exists.scalar_one_or_none() is None:
                raise TaskNotFound(str(task_id))
            artifact = AgentTaskArtifact(
                task_id=task_id,
                kind=kind,
                path_or_uri=path_or_uri,
                checksum=checksum,
                content_type=content_type,
                bytes=size_bytes,
                compressed_bytes=compressed_bytes,
                redaction_status=redaction_status,
            )
            session.add(artifact)
            await session.commit()
            await session.refresh(artifact)
            return artifact

    @classmethod
    async def list_artifacts(cls, task_id: uuid.UUID) -> List[AgentTaskArtifact]:
        async with async_session_factory() as session:
            stmt = (
                select(AgentTaskArtifact)
                .where(AgentTaskArtifact.task_id == task_id)
                .order_by(AgentTaskArtifact.created_at.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @classmethod
    async def add_validation(
        cls,
        task_id: uuid.UUID,
        validator: str,
        status: str,
        command: Optional[str] = None,
        exit_code: Optional[int] = None,
        artifact_id: Optional[int] = None,
        metrics: Optional[dict] = None,
    ) -> AgentValidationResult:
        async with async_session_factory() as session:
            exists = await session.execute(select(AgentTask.id).where(AgentTask.id == task_id))
            if exists.scalar_one_or_none() is None:
                raise TaskNotFound(str(task_id))
            validation = AgentValidationResult(
                task_id=task_id,
                validator=validator,
                command=command,
                status=status,
                exit_code=exit_code,
                artifact_id=artifact_id,
                metrics_json=metrics,
            )
            session.add(validation)
            await session.commit()
            await session.refresh(validation)
            return validation

    @classmethod
    async def list_validations(cls, task_id: uuid.UUID) -> List[AgentValidationResult]:
        async with async_session_factory() as session:
            stmt = (
                select(AgentValidationResult)
                .where(AgentValidationResult.task_id == task_id)
                .order_by(AgentValidationResult.created_at.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @classmethod
    async def add_handoff(
        cls,
        session_id: str,
        summary: str,
        task_id: Optional[uuid.UUID] = None,
        memory_ref: Optional[str] = None,
        artifact_refs: Optional[list] = None,
    ) -> MemoryHandoff:
        async with async_session_factory() as session:
            handoff = MemoryHandoff(
                task_id=task_id,
                session_id=session_id,
                summary=summary,
                memory_ref=memory_ref,
                artifact_refs=artifact_refs,
            )
            session.add(handoff)
            await session.commit()
            await session.refresh(handoff)
            return handoff

    @classmethod
    async def list_handoffs(
        cls,
        session_id: Optional[str] = None,
        task_id: Optional[uuid.UUID] = None,
        limit: int = 20,
    ) -> List[MemoryHandoff]:
        async with async_session_factory() as session:
            stmt = select(MemoryHandoff).order_by(MemoryHandoff.created_at.desc()).limit(limit)
            if session_id:
                stmt = stmt.where(MemoryHandoff.session_id == session_id)
            if task_id:
                stmt = stmt.where(MemoryHandoff.task_id == task_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @classmethod
    async def acquire_lease(cls, task_id: uuid.UUID, worker_name: str, ttl_seconds: int) -> WorkerLease:
        async with async_session_factory() as session:
            exists = await session.execute(select(AgentTask.id).where(AgentTask.id == task_id))
            if exists.scalar_one_or_none() is None:
                raise TaskNotFound(str(task_id))
            now = datetime.datetime.now(datetime.timezone.utc)
            lease_result = await session.execute(
                select(WorkerLease).where(WorkerLease.task_id == task_id).with_for_update()
            )
            lease = lease_result.scalar_one_or_none()
            if lease is not None and lease.lease_expires_at > now:
                if lease.worker_name != worker_name:
                    raise LeaseConflict(
                        f"task {task_id} is leased by {lease.worker_name} until {lease.lease_expires_at.isoformat()}"
                    )
                return lease
            if lease is None:
                lease = WorkerLease(
                    task_id=task_id,
                    worker_name=worker_name,
                    lease_started_at=now,
                    lease_expires_at=now + datetime.timedelta(seconds=ttl_seconds),
                    heartbeat_at=now,
                    fencing_token=1,
                )
                session.add(lease)
            else:
                lease.worker_name = worker_name
                lease.lease_started_at = now
                lease.lease_expires_at = now + datetime.timedelta(seconds=ttl_seconds)
                lease.heartbeat_at = now
                lease.fencing_token += 1
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raced_result = await session.execute(select(WorkerLease).where(WorkerLease.task_id == task_id))
                raced = raced_result.scalar_one_or_none()
                if raced is None:
                    raise
                if raced.lease_expires_at > now and raced.worker_name != worker_name:
                    raise LeaseConflict(f"task {task_id} was concurrently leased by {raced.worker_name}") from exc
                return raced
            await session.refresh(lease)
            return lease
