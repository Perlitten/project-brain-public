"""Memory endpoints: /decisions, /rules, /learnings, /episodes (L2) and
/skills (L4)."""

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from loguru import logger

from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.schemas import (
    EpisodeDecisionRequest,
    EpisodeSearchRequest,
    SkillCreate,
    SkillMatchRequest,
    DecisionCreate,
    LearningCreate,
    RuleCreate,
)
from apps.api import team_access
from apps.api.team_access import repository_authority, team_administrator_access
from brain.auth.principals import ResolvedPrincipal
from brain.team.authorization import Permission
from brain.database.session import async_session_factory
from brain.memory.decision_store import DecisionStore, caller_role_for
from brain.memory.learning_store import LearningStore
from brain.memory.rule_store import RuleStore

EPISODES_MAX_LIMIT = 200

router = APIRouter()
# Brain-wide memory (unfiltered lists, episodes, approvals of trusted memory) belongs to no single
# repository: a team administrator once the team has members, every caller in solo state.
TEAM_ADMIN = Depends(team_administrator_access())


class SkillOutcomeEvidence(BaseModel):
    artifact_ids: list[int] = Field(min_length=1, max_length=32)
    source_refs: list[str] = Field(min_length=1, max_length=16)


class SkillOutcomeRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    validation_id: int = Field(gt=0)
    outcome: Literal["success", "failure"]
    evidence: SkillOutcomeEvidence


@router.post("/skills/{skill_id}/outcomes", dependencies=[Depends(require_scope("core:write"))])
async def record_skill_outcome_endpoint(skill_id: int, body: SkillOutcomeRequest,
                                        principal: ResolvedPrincipal = Depends(require_principal)):
    # An outcome is evidence from one harness task, like a lesson: knowledge.propose on that task's
    # repository; an unknown task leaves no repository, which only a team administrator may act on.
    if await team_access.current_team_state() == "team":
        from brain.memory.harness_store import HarnessStore
        try:
            task = await HarnessStore.get_task(uuid.UUID(body.task_id))
        except ValueError:
            task = None
        await repository_authority(principal, Permission.PROPOSE, repo_path=task.repo_path if task else None)
    return await record_skill_outcome_service(skill_id, body)


async def record_skill_outcome_service(skill_id: int, body: SkillOutcomeRequest):
    """Store a validated outcome after transport-specific authorization has completed.

    The local stdio MCP server is a trusted in-process operator surface and calls this
    service directly. HTTP callers must go through ``record_skill_outcome_endpoint``.
    """
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
async def get_decisions(principal: ResolvedPrincipal = Depends(require_principal)):
    try:
        return await team_access.readable_memory_rows(principal, await DecisionStore.list_decisions())
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to fetch decisions: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch decisions") from exc


@router.post("/decisions", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_decision(body: DecisionCreate, caller: ResolvedPrincipal = Depends(require_principal)):
    # The role and the author come from the authenticated principal, never from the body.
    # No repo_path is the system-global scope: a team administrator once the team has members.
    await repository_authority(caller, Permission.PROPOSE, repo_path=body.repo_path)
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
            caller_role=caller_role_for(caller),
            author_principal_id=caller.author_id,
        )
        return {"status": "success", "decision_id": decision_id}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to add decision: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add decision") from exc


@router.get("/rules", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_rules(principal: ResolvedPrincipal = Depends(require_principal)):
    try:
        return await team_access.readable_memory_rows(principal, await RuleStore.list_rules())
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to fetch rules: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch rules") from exc


@router.post("/rules", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_rule(body: RuleCreate, caller: ResolvedPrincipal = Depends(require_principal)):
    await repository_authority(caller, Permission.PROPOSE, repo_path=body.repo_path)
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
            author_principal_id=caller.author_id,
        )
        return {"status": "success", "rule_id": rule_id}
    except Exception as exc:
        logger.error(f"Failed to add rule: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add rule") from exc


@router.get("/learnings", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_learnings(repo_path: Optional[str] = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await repository_authority(principal, Permission.READ, repo_path=repo_path)
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


@router.get("/episodes", dependencies=[Depends(require_api_key), Depends(require_scope("core:read")), TEAM_ADMIN])
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


@router.post("/episodes/search",
             dependencies=[Depends(require_api_key), Depends(require_scope("core:read")), TEAM_ADMIN])
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
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write")), TEAM_ADMIN],
)
async def approve_pending_episode(episode_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Promote a pending (needs_approval) L2 episode into an L3 learning."""
    return await _decide_episode(episode_id, body, approve=True)


@router.post(
    "/episodes/{episode_id}/reject",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write")), TEAM_ADMIN],
)
async def reject_pending_episode(episode_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Reject a pending L2 episode."""
    return await _decide_episode(episode_id, body, approve=False)


# === L4 Procedural Memory: Skills Registry ===

@router.get("/skills", dependencies=[Depends(require_api_key), Depends(require_scope("core:read")), TEAM_ADMIN])
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
                "repo_scope": s.repo_scope,
                "is_global": s.is_global,
                "source_learning_ids": s.source_learning_ids,
            }
            for s in skills
        ]
    except Exception as exc:
        logger.error(f"Failed to fetch skills: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch skills") from exc


@router.post("/skills", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_skill(body: SkillCreate, principal: ResolvedPrincipal = Depends(require_principal)):
    """L4 procedural memory: register a new skill/workflow."""
    # A global skill, or one without a repository, applies everywhere: a team administrator.
    await repository_authority(principal, Permission.PROPOSE,
                               repo_path=None if body.is_global else body.repo_scope)
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


async def _decide_skill(skill_id: int, body: Optional[EpisodeDecisionRequest], *, approve: bool) -> dict:
    from brain.memory.skill_store import SkillStateError, decide_skill

    reason = body.reason if body else None
    try:
        return await decide_skill(skill_id, approve=approve, reason=reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SkillStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Skill decision failed for {skill_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Skill decision failed") from exc


@router.post(
    "/skills/{skill_id}/approve",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write")), TEAM_ADMIN],
)
async def approve_pending_skill(skill_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Activate a pending (machine-distilled) L4 skill so it can be matched and injected."""
    return await _decide_skill(skill_id, body, approve=True)


@router.post(
    "/skills/{skill_id}/reject",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write")), TEAM_ADMIN],
)
async def reject_pending_skill(skill_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Reject a pending distilled L4 skill; its source learnings stay active."""
    return await _decide_skill(skill_id, body, approve=False)


@router.post("/skills/match", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def match_skills(body: SkillMatchRequest, principal: ResolvedPrincipal = Depends(require_principal)):
    """L4 procedural memory: find skills relevant to a task (embedding order, trigger re-rank)."""
    await repository_authority(principal, Permission.READ, repo_path=body.repo_path)
    try:
        from brain.memory.skill_store import match_skills as match_skills_query

        return await match_skills_query(body.query, repo_scope=body.repo_path, limit=body.limit)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Skill matching failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Skill matching failed") from exc


@router.post("/learnings", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_learning(body: LearningCreate, principal: ResolvedPrincipal = Depends(require_principal)):
    await repository_authority(principal, Permission.PROPOSE, repo_path=body.repo_scope)
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
    "/learnings/{learning_id}",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write")), TEAM_ADMIN],
)
async def delete_learning(learning_id: int):
    try:
        await LearningStore.reject(learning_id)
        return {"status": "success", "learning_id": learning_id}
    except Exception as exc:
        logger.error(f"Failed to reject learning: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to reject learning") from exc
