"""Validate task-owned *reported* receipts without claiming server execution."""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from brain.database.harness_models import AgentTaskArtifact, AgentValidationResult


async def validate_reported_evidence(session, task_id, validation: AgentValidationResult | None,
                                     artifact_ids: Sequence[int], source_refs: Sequence[str]) -> None:
    if validation is None or validation.status not in {"pass", "fail", "error"}:
        raise ValueError("validation must belong to task and have pass, fail, or error status")
    if validation.status == "pass" and validation.exit_code not in {None, 0}:
        raise ValueError("passing validation cannot have a failing exit code")
    if (not artifact_ids or len(artifact_ids) > 32
            or any(type(value) is not int or value <= 0 for value in artifact_ids)):
        raise ValueError("artifact_ids must contain 1 to 32 positive integers")
    if (not source_refs or len(source_refs) > 16
            or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 2048 for ref in source_refs)):
        raise ValueError("source_refs must contain 1 to 16 non-blank references")
    if validation.artifact_id is None or validation.artifact_id not in artifact_ids:
        raise ValueError("evidence must include the validation's linked artifact")
    artifacts = list((await session.execute(select(AgentTaskArtifact).where(
        AgentTaskArtifact.task_id == task_id, AgentTaskArtifact.id.in_(artifact_ids),
    ))).scalars().all())
    if {artifact.id for artifact in artifacts} != set(artifact_ids):
        raise ValueError("all artifact_ids must belong to task")
    if not set(source_refs).issubset({artifact.path_or_uri for artifact in artifacts}):
        raise ValueError("source_refs must reference the registered task artifacts")
