"""Memory endpoints: /decisions, /rules, /learnings, /episodes (L2) and
/skills (L4)."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from loguru import logger

from apps.api.auth import require_api_key, require_scope
from apps.api.schemas import (
    EpisodeDecisionRequest,
    EpisodeSearchRequest,
    SkillCreate,
    SkillMatchRequest,
    DecisionCreate,
    LearningCreate,
    RuleCreate,
)
from brain.database.session import async_session_factory
from brain.memory.decision_store import DecisionStore
from brain.memory.learning_store import LearningStore
from brain.memory.rule_store import RuleStore

EPISODES_MAX_LIMIT = 200

router = APIRouter()


class SkillOutcomeEvidence(BaseModel):
    artifact_ids: list[int] = Field(min_length=1, max_length=32)
    source_refs: list[str] = Field(min_length=1, max_length=16)


class SkillOutcomeRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    validation_id: int = Field(gt=0)
    outcome: Literal["success", "failure"]
    evidence: SkillOutcomeEvidence


@router.post("/skills/{skill_id}/outcomes", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def record_skill_outcome_endpoint(skill_id: int, body: SkillOutcomeRequest):
    from brain.memory.skill_outcomes import SkillOutcomeConflict, record_skill_outcome
    try:
        return await record_skill_outcome(skill_id=skill_id, task_id=body.task_id, validation_id=body.validation_id,
                                          outcome=body.outcome, evidence=body.evidence.model_dump())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SkillOutcomeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/decisions", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_decisions():
    try:
        return await DecisionStore.list_decisions()
    except Exception as exc:
        logger.error(f"Failed to fetch decisions: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch decisions") from exc


@router.post("/decisions", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_decision(body: DecisionCreate):
    try:
        decision_id = await DecisionStore.add_decision(
            title=body.title,
            repo_path=body.repo_path,
            description=body.description,
            status=body.status,
            date=body.date,
            reason=body.reason,
            consequences=body.consequences,
            affected_features=body.affected_features,
            affected_modules=body.affected_modules,
            affected_files=body.affected_files,
        )
        return {"status": "success", "decision_id": decision_id}
    except Exception as exc:
        logger.error(f"Failed to add decision: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add decision") from exc


@router.get("/rules", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_rules():
    try:
        return await RuleStore.list_rules()
    except Exception as exc:
        logger.error(f"Failed to fetch rules: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch rules") from exc


@router.post("/rules", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_rule(body: RuleCreate):
    try:
        rule_id = await RuleStore.add_rule(
            name=body.name,
            repo_path=body.repo_path,
            description=body.description,
            type=body.type,
            severity=body.severity,
            status=body.status,
            applies_to=body.applies_to,
            rule_id=body.rule_id,
        )
        return {"status": "success", "rule_id": rule_id}
    except Exception as exc:
        logger.error(f"Failed to add rule: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add rule") from exc


@router.get("/learnings", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_learnings(repo_path: Optional[str] = None):
    try:
        learnings = await LearningStore.list_active_learnings(repo_path)
        return [
            {
                "id": lrng.id,
                "statement": lrng.statement,
                "category": lrng.category,
                "confidence": lrng.confidence,
                "repo_scope": lrng.repo_scope,
                "status": lrng.status,
            }
            for lrng in learnings
        ]
    except Exception as exc:
        logger.error(f"Failed to fetch learnings: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch learnings") from exc


@router.get("/episodes", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_episodes(
    status: Optional[Literal["pending", "promoted", "rejected", "duplicate", "merged"]] = None,
    topic: Optional[str] = None,
    limit: int = Query(50, ge=1, le=EPISODES_MAX_LIMIT),
):
    """L2 episodic memory: list episodes, newest first."""
    try:
        from brain.database.models import MemoryEpisode
        from sqlalchemy import desc

        async with async_session_factory() as session:
            q = select(MemoryEpisode).order_by(desc(MemoryEpisode.created_at)).limit(limit)
            if status:
                q = q.where(MemoryEpisode.status == status)
            if topic:
                q = q.where(MemoryEpisode.topic == topic)
            episodes = (await session.execute(q)).scalars().all()
            return [
                {
                    "id": ep.id,
                    "distilled_summary": ep.distilled_summary,
                    "topic": ep.topic,
                    "status": ep.status,
                    "confidence": ep.confidence,
                    "source_event_ids": ep.source_event_ids,
                    "promoted_to_learning_id": ep.promoted_to_learning_id,
                    "duplicate_of_learning_id": ep.duplicate_of_learning_id,
                    "gate_reasons": ep.gate_reasons,
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                }
                for ep in episodes
            ]
    except Exception as exc:
        logger.error(f"Failed to fetch episodes: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch episodes") from exc


@router.post("/episodes/search", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def search_episodes(body: EpisodeSearchRequest):
    """L2 episodic memory: semantic search over episode summaries."""
    try:
        from brain.database.models import MemoryEpisode
        from brain.embeddings.constants import EMBEDDING_DIMENSION
        from brain.embeddings.pgvector_sql import truncate_vector_for_index
        from brain.llm import get_embedding_provider

        query_vec = await get_embedding_provider().embed(body.query)
        if not query_vec:
            raise HTTPException(status_code=500, detail="Embedding failed")
        query_vec = truncate_vector_for_index(list(query_vec), EMBEDDING_DIMENSION)

        async with async_session_factory() as session:
            q = select(MemoryEpisode).where(MemoryEpisode.embedding.isnot(None))
            if body.status:
                q = q.where(MemoryEpisode.status == body.status)
            q = q.order_by(MemoryEpisode.embedding.cosine_distance(query_vec)).limit(body.limit)
            episodes = (await session.execute(q)).scalars().all()
            return [
                {
                    "id": ep.id,
                    "distilled_summary": ep.distilled_summary,
                    "topic": ep.topic,
                    "status": ep.status,
                    "confidence": ep.confidence,
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                }
                for ep in episodes
            ]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Episode search failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Episode search failed") from exc


async def _decide_episode(episode_id: int, body: Optional[EpisodeDecisionRequest], *, approve: bool) -> dict:
    from brain.memory.consolidation import EpisodeStateError, approve_episode, reject_episode

    reason = body.reason if body else None
    try:
        if approve:
            return await approve_episode(episode_id, reason)
        return await reject_episode(episode_id, reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EpisodeStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Episode decision failed for {episode_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Episode decision failed") from exc


@router.post(
    "/episodes/{episode_id}/approve",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))],
)
async def approve_pending_episode(episode_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Promote a pending (needs_approval) L2 episode into an L3 learning."""
    return await _decide_episode(episode_id, body, approve=True)


@router.post(
    "/episodes/{episode_id}/reject",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))],
)
async def reject_pending_episode(episode_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Reject a pending L2 episode."""
    return await _decide_episode(episode_id, body, approve=False)


# === L4 Procedural Memory: Skills Registry ===

@router.get("/skills", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_skills(status: Optional[str] = "active", limit: int = Query(50, ge=1, le=200)):
    """L4 procedural memory: list learned skills and workflows."""
    try:
        from brain.memory.skill_store import list_skills

        skills = await list_skills(status=status, limit=limit)
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "triggers": s.triggers,
                "workflow": s.workflow,
                "times_used": s.times_used,
                "times_successful": s.times_successful,
                "success_rate": round(s.times_successful / s.times_used, 3) if s.times_used > 0 else None,
                "status": s.status,
                "confidence": s.confidence,
            }
            for s in skills
        ]
    except Exception as exc:
        logger.error(f"Failed to fetch skills: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch skills") from exc


@router.post("/skills", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_skill(body: SkillCreate):
    """L4 procedural memory: register a new skill/workflow."""
    try:
        from brain.memory.skill_store import SkillConflictError, create_skill as create_skill_row

        skill = await create_skill_row(
            name=body.name,
            description=body.description,
            triggers=body.triggers,
            workflow=body.workflow,
            source_episode_ids=body.source_episode_ids,
            source_learning_ids=body.source_learning_ids,
            confidence=body.confidence,
            repo_scope=body.repo_scope,
            is_global=body.is_global,
        )
        return {"id": skill.id, "name": skill.name, "status": "created"}
    except SkillConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to create skill: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create skill") from exc


@router.post("/skills/match", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def match_skills(body: SkillMatchRequest):
    """L4 procedural memory: find skills relevant to a task (embedding order, trigger re-rank)."""
    try:
        from brain.memory.skill_store import match_skills as match_skills_query

        return await match_skills_query(body.query, repo_scope=body.repo_path, limit=body.limit)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Skill matching failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Skill matching failed") from exc


@router.post("/learnings", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_learning(body: LearningCreate):
    try:
        valid_until = None
        if body.valid_until:
            from datetime import datetime

            valid_until = datetime.fromisoformat(body.valid_until)
        learning_id = await LearningStore.add_learning(
            statement=body.statement,
            category=body.category,
            confidence=body.confidence,
            repo_scope=body.repo_scope,
            valid_until=valid_until,
        )
        return {"status": "success", "learning_id": learning_id}
    except Exception as exc:
        logger.error(f"Failed to add learning: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add learning") from exc


@router.delete(
    "/learnings/{learning_id}", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))]
)
async def delete_learning(learning_id: int):
    try:
        await LearningStore.reject(learning_id)
        return {"status": "success", "learning_id": learning_id}
    except Exception as exc:
        logger.error(f"Failed to reject learning: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to reject learning") from exc
