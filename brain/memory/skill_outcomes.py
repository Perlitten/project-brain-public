"""Evidence-linked, idempotent L4 procedure outcomes."""
from __future__ import annotations

from typing import Any
import uuid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from brain.database.harness_models import AgentTask, AgentValidationResult
from brain.database.models import MemorySkill, SkillOutcome
from brain.database.session import async_session_factory
from brain.memory.repo_scope import normalize_repo_scope
from brain.memory.validated_evidence import validate_reported_evidence


class SkillOutcomeConflict(ValueError):
    pass


async def record_skill_outcome(*, skill_id: int, task_id: str, validation_id: int,
                               outcome: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    if outcome not in {"success", "failure"}:
        raise ValueError("outcome must be success or failure")
    evidence = evidence or {}
    task_uuid = uuid.UUID(str(task_id))
    if not evidence.get("artifact_ids") or not evidence.get("source_refs"):
        raise ValueError("evidence requires non-empty artifact_ids and source_refs")
    async with async_session_factory() as session:
        skill = (await session.execute(select(MemorySkill).where(MemorySkill.id == skill_id).with_for_update())).scalar_one_or_none()
        task = (await session.execute(select(AgentTask).where(AgentTask.id == task_uuid))).scalar_one_or_none()
        validation = (await session.execute(select(AgentValidationResult).where(AgentValidationResult.id == validation_id, AgentValidationResult.task_id == task_uuid))).scalar_one_or_none()
        if not skill or not task or not validation:
            raise LookupError("skill, task, or task validation not found")
        existing = (await session.execute(select(SkillOutcome).where(SkillOutcome.task_id == task_uuid, SkillOutcome.skill_id == skill_id))).scalar_one_or_none()
        if existing:
            if existing.validation_id != validation_id or existing.outcome != outcome or existing.evidence != evidence:
                raise SkillOutcomeConflict("skill outcome already recorded for this task")
            return {"id": existing.id, "skill_id": skill_id, "task_id": str(task_id), "outcome": existing.outcome, "idempotent": True, "reported_validation": True}
        if skill.status != "active":
            raise ValueError("only active skills may record outcomes")
        if outcome == "success" and validation.status != "pass":
            raise ValueError("success requires a passing reported validation")
        if outcome == "failure" and validation.status not in {"pass", "fail", "error"}:
            raise ValueError("failure requires validation status pass, fail, or error")
        if skill.repo_scope is None and not skill.is_global:
            raise PermissionError("unscoped non-global skills cannot record outcomes")
        if normalize_repo_scope(skill.repo_scope) and normalize_repo_scope(skill.repo_scope) != normalize_repo_scope(task.repo_path):
            raise PermissionError("skill repository scope does not match task")
        await validate_reported_evidence(session, task_uuid, validation,
            evidence.get("artifact_ids", []), evidence.get("source_refs", []))
        row = SkillOutcome(skill_id=skill_id, task_id=task_uuid, validation_id=validation_id, outcome=outcome, evidence=evidence or {}, reported_validation=True)
        session.add(row)
        skill.times_used += 1
        if outcome == "success":
            skill.times_successful += 1
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise SkillOutcomeConflict("skill outcome already recorded for this task") from exc
        return {"id": row.id, "skill_id": skill_id, "task_id": str(task_id), "outcome": outcome, "idempotent": False, "reported_validation": True}
