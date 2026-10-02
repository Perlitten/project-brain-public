"""Pydantic request/response models for the harness task-ledger API."""

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    title: str
    goal: str
    repo_path: str
    owner_agent: Optional[str] = None
    target_agent: Optional[str] = None
    worktree_path: Optional[str] = None
    created_by_session_id: Optional[str] = None
    priority: str = "P2"
    timeout_seconds: int = Field(default=900, ge=1)
    retry_budget: int = Field(default=1, ge=0)


class TaskStatusUpdateRequest(BaseModel):
    status: str
    actor: str
    expected_task_version: int = Field(ge=0)
    current_session_id: Optional[str] = None
    note: Optional[str] = None


class EventCreateRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=100)
    actor: str = Field(min_length=1, max_length=100)
    classification: Literal[
        "claim",
        "observation",
        "evidence",
        "decision",
        "instruction",
    ] = "observation"
    idempotency_key: str = Field(min_length=8, max_length=255)
    causal_parent_id: Optional[int] = None
    expected_task_version: int = Field(ge=0)
    payload: Optional[dict[str, Any]] = None


class ArtifactCreateRequest(BaseModel):
    kind: str
    path_or_uri: str
    checksum: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    compressed_bytes: Optional[int] = None
    redaction_status: str = "unknown"


class ValidationCreateRequest(BaseModel):
    validator: str
    status: str
    command: Optional[str] = None
    exit_code: Optional[int] = None
    artifact_id: Optional[int] = None
    metrics: Optional[dict[str, Any]] = None


class HandoffCreateRequest(BaseModel):
    session_id: str
    summary: str
    task_id: Optional[uuid.UUID] = None
    memory_ref: Optional[str] = None
    artifact_refs: Optional[list] = None


class LeaseAcquireRequest(BaseModel):
    worker_name: str
    ttl_seconds: int = Field(default=900, ge=1)


class AcceptanceCheckpointRequest(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=255)
    external_task_id: str = Field(min_length=1, max_length=255)
    authority: Literal["codex", "human"]
    idempotency_key: str = Field(min_length=8, max_length=255)
    expected_task_version: int = Field(ge=0)
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    brain_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    repository_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fencing_token: int = Field(ge=1)


class SignedAcceptanceRecord(BaseModel):
    task_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    brain_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    repository_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    brain_checkpoint_id: int = Field(ge=1)
    brain_checkpoint_version: int = Field(ge=1)
    ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fencing_token: int = Field(ge=1)
    accepted_at: str = Field(min_length=1, max_length=64)
    authority: Literal["codex", "human"]
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")


class AcceptanceFinalizeRequest(BaseModel):
    expected_task_version: int = Field(ge=0)
    acceptance_record: SignedAcceptanceRecord
