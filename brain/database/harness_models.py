"""Task ledger for the Claude Code harness (P1 hardening, 2026-07-07).

Lives in its own Postgres schema (``harness``) inside brain_db so it never
collides with Brain's own tables (decisions, tasks, files, ...). Mirrors the
state machine and table shapes from the harness hardening plan:
created -> routed -> claimed -> running -> artifacted -> memory_updated ->
validating -> passed|failed|cancelled|timed_out.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.database.models import Base

HARNESS_SCHEMA = "harness"


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = {"schema": HARNESS_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created", index=True)
    owner_agent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_agent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    repo_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    worktree_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_by_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="P2")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    retry_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentTaskEvent(Base):
    __tablename__ = "agent_task_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_agent_task_events_task_idempotency",
        ),
        {"schema": HARNESS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{HARNESS_SCHEMA}.agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False, default="observation")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    causal_parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_task_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentTaskArtifact(Base):
    __tablename__ = "agent_task_artifacts"
    __table_args__ = {"schema": HARNESS_SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{HARNESS_SCHEMA}.agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)  # log|diff|test-report|screenshot|trace|summary
    path_or_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    compressed_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    redaction_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown"
    )  # clean|redacted|unknown
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentValidationResult(Base):
    __tablename__ = "agent_validation_results"
    __table_args__ = {"schema": HARNESS_SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{HARNESS_SCHEMA}.agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validator: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # pass|fail|error
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(f"{HARNESS_SCHEMA}.agent_task_artifacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    metrics_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MemoryHandoff(Base):
    __tablename__ = "memory_handoffs"
    __table_args__ = {"schema": HARNESS_SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{HARNESS_SCHEMA}.agent_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    memory_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_refs: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkerLease(Base):
    __tablename__ = "worker_leases"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_worker_leases_task"),
        {"schema": HARNESS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{HARNESS_SCHEMA}.agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    worker_name: Mapped[str] = mapped_column(String(100), nullable=False)
    lease_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)


# Valid state-machine transitions. Enforced in brain.memory.harness_store,
# not at the DB layer, so the API can return a clear 409 instead of a raw
# constraint violation.
TASK_STATUSES = (
    "created",
    "routed",
    "claimed",
    "running",
    "artifacted",
    "memory_updated",
    "validating",
    "acceptance_pending",
    "passed",
    "failed",
    "cancelled",
    "timed_out",
)
TERMINAL_STATUSES = {"passed", "failed", "cancelled", "timed_out"}
ALLOWED_TRANSITIONS = {
    "created": {"routed", "cancelled", "timed_out"},
    "routed": {"claimed", "cancelled", "timed_out"},
    "claimed": {"running", "cancelled", "timed_out"},
    "running": {"artifacted", "failed", "cancelled", "timed_out"},
    "artifacted": {"memory_updated", "failed", "timed_out"},
    "memory_updated": {"validating", "failed", "timed_out"},
    "validating": {"passed", "failed", "timed_out"},
    "acceptance_pending": set(),
}
