"""Store for L4 procedural memory: skills distilled from episodes and learnings.

Mirrors the shape of brain.memory.learning_store: thin async helpers around
async_session_factory, no ORM sessions leak past this module.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from brain.database.models import MemorySkill
from brain.database.session import async_session_factory
from brain.memory.repo_scope import normalize_repo_scope


class SkillConflictError(Exception):
    """Raised when a skill with the same name already exists."""


def _scope_clause(repo_scope: Optional[str]):
    """Include global skills plus skills owned by one repository.

    A NULL ``repo_scope`` row is global and matches every scope; a query with
    no scope (None) sees only global rows — same contract as
    ``LearningStore.list_active_learnings``. Used for match/list endpoints,
    NOT for prompt injection (see ``_injection_scope_clause``).
    """
    normalized = normalize_repo_scope(repo_scope)
    return or_(
        MemorySkill.repo_scope.is_(None),
        func.lower(func.trim(MemorySkill.repo_scope)) == (normalized or "").casefold(),
    )


def _injection_scope_clause(repo_scope: Optional[str]):
    """Stricter scope for prompt injection only.

    Eligible: skills explicitly marked ``is_global`` plus skills whose
    ``repo_scope`` equals the requesting repository. A NULL ``repo_scope`` is
    never injected — underivable backfilled rows stay NULL, and treating them
    as global would leak every old unscoped skill into every repository the
    moment ``MEMORY_SKILLS_IN_ASK`` is enabled. NULL-scope rows remain
    listable via GET /skills and match endpoints; they are just not injected.
    """
    normalized = normalize_repo_scope(repo_scope)
    clauses: list[ColumnElement[bool]] = [MemorySkill.is_global.is_(True)]
    if normalized:
        clauses.append(
            func.lower(func.trim(MemorySkill.repo_scope)) == normalized.casefold()
        )
    return or_(*clauses)


async def list_skills(*, status: Optional[str] = "active", limit: int = 50) -> List[MemorySkill]:
    """List skills by descending confidence (GET /skills)."""
    async with async_session_factory() as session:
        query = select(MemorySkill).order_by(desc(MemorySkill.confidence)).limit(limit)
        if status:
            query = query.where(MemorySkill.status == status)
        return list((await session.execute(query)).scalars().all())


async def create_skill(
    *,
    name: str,
    description: str,
    triggers: List[str],
    workflow: List[Dict[str, Any]],
    source_episode_ids: List[int],
    source_learning_ids: List[int],
    confidence: float,
    repo_scope: Optional[str],
    is_global: bool = False,
) -> MemorySkill:
    """Persist a new skill; raises SkillConflictError on a name conflict.

    ``repo_scope`` is normalized at write time (trailing slashes, backslashes)
    so scope filtering matches the same rules as learnings. ``is_global`` is
    the explicit opt-in for prompt injection into every repository; a NULL
    scope alone is never injected.
    """
    name = name.strip()
    description = description.strip()
    if not name or not description:
        raise ValueError("name and description are required")
    async with async_session_factory() as session:
        existing = await session.execute(select(MemorySkill).where(MemorySkill.name == name))
        if existing.scalar_one_or_none():
            raise SkillConflictError(f"Skill '{name}' already exists")
        embedding = None
        try:
            from brain.llm import get_embedding_provider

            vec = await get_embedding_provider().embed(f"{name}: {description}")
            embedding = list(vec) if vec else None
        except Exception as emb_exc:
            from loguru import logger

            logger.warning(f"Skill embedding failed, storing NULL: {emb_exc}")
        skill = MemorySkill(
            name=name,
            description=description,
            triggers=triggers,
            workflow=workflow,
            source_episode_ids=source_episode_ids,
            source_learning_ids=source_learning_ids,
            confidence=confidence,
            embedding=embedding,
            repo_scope=normalize_repo_scope(repo_scope),
            is_global=is_global,
        )
        session.add(skill)
        await session.commit()
        await session.refresh(skill)
        return skill


async def match_skills(
    query: str,
    *,
    repo_scope: Optional[str] = None,
    limit: int = 5,
    strict_scope: bool = False,
) -> List[Dict[str, Any]]:
    """Find skills relevant to a task: embedding order, then trigger re-rank.

    ``repo_scope`` strictly filters candidates: global skills (NULL scope) plus
    skills owned by the given repository — a skill scoped to another repo is
    never returned. Pass None to see only global skills.

    ``strict_scope=True`` is the prompt-injection contract: only skills owned
    by the requesting repository or explicitly ``is_global`` qualify; NULL
    scope alone never qualifies.
    """
    from brain.llm import get_embedding_provider

    query_vec = await get_embedding_provider().embed(query)

    async with async_session_factory() as session:
        stmt = select(MemorySkill).where(
            MemorySkill.status == "active",
            MemorySkill.embedding.isnot(None),
            _injection_scope_clause(repo_scope) if strict_scope else _scope_clause(repo_scope),
        )
        if query_vec:
            stmt = stmt.order_by(MemorySkill.embedding.cosine_distance(query_vec))
        stmt = stmt.limit(limit * 2)  # over-fetch for trigger re-ranking
        skills = (await session.execute(stmt)).scalars().all()

    query_lower = query.lower()
    scored = []
    for s in skills:
        score = 0.0
        for trigger in s.triggers or []:
            if trigger.lower() in query_lower:
                score += 1.0
        if s.times_used > 0:
            score += (s.times_successful / s.times_used) * 0.5
        scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "workflow": s.workflow,
            "confidence": s.confidence,
            "repo_scope": s.repo_scope,
            "is_global": s.is_global,
            "trigger_score": score,
        }
        for score, s in scored[:limit]
    ]


async def select_skills_for_context(
    query: str,
    repo_scope: Optional[str],
    *,
    max_count: int,
) -> List[Dict[str, Any]]:
    """Pick the top skills eligible to enter a runtime context for this repo.

    Injection semantics are stricter than match: only same-repo skills and
    ``is_global`` skills qualify — a NULL ``repo_scope`` is never injected.
    """
    if max_count <= 0:
        return []
    return await match_skills(query, repo_scope=repo_scope, limit=max_count, strict_scope=True)
