"""Store for L3 semantic memory: durable learnings promoted from L2 episodes.

Mirrors the shape of brain.memory.rule_store: a thin classmethod wrapper
around async_session_factory, no ORM sessions leak past this module.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.database.models import Learning
from brain.database.session import async_session_factory
from brain.memory.repo_scope import normalize_repo_scope, scope_equals


def json_key(item: Any) -> str:
    """Stable identity of an evidence ref for dedup when reinforcing."""
    try:
        return json.dumps(item, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(item)


def _scope_clause(repo_path: Optional[str]):
    """Include global learnings plus learnings owned by one repository."""
    normalized = normalize_repo_scope(repo_path)
    return or_(
        Learning.repo_scope.is_(None),
        scope_equals(Learning.repo_scope, normalized),
    )


class LearningStore:
    """Store for managing consolidated learnings (L3 semantic memory)."""

    @classmethod
    async def add_learning(
        cls,
        statement: str,
        category: Optional[str] = None,
        confidence: float = 0.5,
        evidence: Optional[List[Dict[str, Any]]] = None,
        repo_scope: Optional[str] = None,
        valid_until: Optional[datetime] = None,
        promoted_from: Optional[str] = None,
        session: Optional[AsyncSession] = None,
        author_principal_id: Optional[int] = None,
        updated_by_principal_id: Optional[int] = None,
        status: str = "active",
    ) -> int:
        """Save a new learning and return its id.

        ``status="personal"`` keeps the learning in its author's personal tier:
        every retrieval path reads ``active`` learnings only, so it reaches shared
        memory only when a reviewer approves a promotion proposal.

        ``author_principal_id`` names who contributed it (the API caller, or the
        sole principal behind consolidated evidence); ``updated_by_principal_id``
        defaults to the author.

        With ``session`` the row is only flushed, so the caller's transaction
        decides whether it commits (used by consolidation for atomic L2→L3 writes).

        Computes the statement embedding once at write time so query-ranked
        retrieval and G1 dedup never pay per-query embedding costs. If the
        embedding provider is unavailable, the learning is still saved with
        a NULL embedding; retrieval falls back to confidence ordering.
        """
        statement = statement.strip()
        if not statement:
            raise ValueError("Learning statement must not be empty")
        embedding: Optional[List[float]] = None
        embedding_model: Optional[str] = None
        try:
            from brain.llm import get_embedding_provider

            embedder = get_embedding_provider()
            embedding = await embedder.embed(statement)
            embedding_model = getattr(embedder, "model", None) or getattr(
                embedder, "provider", "unknown"
            )
        except Exception as exc:
            logger.warning(f"Learning embedding failed, storing NULL: {exc}")
        learning = Learning(
            statement=statement,
            category=category,
            confidence=max(0.0, min(1.0, confidence)),
            evidence=evidence or [],
            status=status,
            repo_scope=normalize_repo_scope(repo_scope),
            valid_until=valid_until,
            promoted_from=promoted_from,
            embedding=embedding,
            embedding_model=embedding_model,
            author_principal_id=author_principal_id,
            updated_by_principal_id=(
                updated_by_principal_id if updated_by_principal_id is not None else author_principal_id
            ),
        )
        if session is not None:
            session.add(learning)
            await session.flush()
            return learning.id
        async with async_session_factory() as own_session:
            own_session.add(learning)
            await own_session.commit()
            await own_session.refresh(learning)
            return learning.id

    @classmethod
    async def list_learnings(cls) -> List[Learning]:
        """Retrieve all learnings, newest first."""
        async with async_session_factory() as session:
            result = await session.execute(select(Learning).order_by(Learning.id.desc()))
            return list(result.scalars().all())

    @classmethod
    async def list_active_learnings(cls, repo_path: Optional[str] = None) -> List[Learning]:
        """Return active, non-expired global learnings plus learnings for one repo."""
        now = datetime.now(timezone.utc)
        async with async_session_factory() as session:
            result = await session.execute(
                select(Learning).where(
                    Learning.status == "active",
                    or_(Learning.valid_until.is_(None), Learning.valid_until > now),
                    _scope_clause(repo_path),
                ).order_by(Learning.confidence.desc(), Learning.id.desc())
            )
            return list(result.scalars().all())

    @classmethod
    async def supersede(
        cls,
        learning_id: int,
        superseded_by: int,
        session: Optional[AsyncSession] = None,
        updated_by_principal_id: Optional[int] = None,
    ) -> None:
        """Mark a learning superseded by a newer one. Append-only history.

        With ``session`` the change is only flushed inside the caller's
        transaction (consolidation retires the corrected learning atomically
        with the episode and the new learning).
        """
        if learning_id == superseded_by:
            raise ValueError("A learning cannot supersede itself")

        async def _apply(sess: AsyncSession) -> None:
            result = await sess.execute(select(Learning).where(Learning.id == learning_id))
            learning = result.scalar_one_or_none()
            if learning is None:
                raise LookupError(f"Learning {learning_id} not found")
            learning.status = "superseded"
            learning.superseded_by = superseded_by
            if updated_by_principal_id is not None:
                learning.updated_by_principal_id = updated_by_principal_id

        if session is not None:
            await _apply(session)
            await session.flush()
            return
        async with async_session_factory() as own_session:
            await _apply(own_session)
            await own_session.commit()

    @classmethod
    async def reinforce(
        cls,
        learning_id: int,
        *,
        evidence: Optional[List[Dict[str, Any]]] = None,
        confidence_step: float = 0.05,
        session: Optional[AsyncSession] = None,
    ) -> float:
        """Record that an active learning was observed again (G1 duplicate).

        Appends the new evidence refs and raises confidence by ``confidence_step``
        (capped at 1.0). Returns the new confidence. The statement never changes:
        a duplicate confirms, it does not rewrite.
        """

        async def _apply(sess: AsyncSession) -> float:
            result = await sess.execute(select(Learning).where(Learning.id == learning_id))
            learning = result.scalar_one_or_none()
            if learning is None:
                raise LookupError(f"Learning {learning_id} not found")
            if evidence:
                known = {json_key(item) for item in (learning.evidence or [])}
                merged = list(learning.evidence or [])
                for item in evidence:
                    if json_key(item) not in known:
                        merged.append(item)
                        known.add(json_key(item))
                learning.evidence = merged
            learning.confidence = max(0.0, min(1.0, float(learning.confidence) + confidence_step))
            return float(learning.confidence)

        if session is not None:
            new_conf = await _apply(session)
            await session.flush()
            return new_conf
        async with async_session_factory() as own_session:
            new_conf = await _apply(own_session)
            await own_session.commit()
            return new_conf

    @classmethod
    async def reject(cls, learning_id: int, updated_by_principal_id: Optional[int] = None) -> None:
        """Mark a learning rejected (failed promotion or human veto)."""
        async with async_session_factory() as session:
            result = await session.execute(select(Learning).where(Learning.id == learning_id))
            learning = result.scalar_one_or_none()
            if learning is None:
                raise LookupError(f"Learning {learning_id} not found")
            learning.status = "rejected"
            if updated_by_principal_id is not None:
                learning.updated_by_principal_id = updated_by_principal_id
            await session.commit()


async def add_learning(
    statement: str,
    category: Optional[str] = None,
    confidence: float = 0.5,
    evidence: Optional[List[Dict[str, Any]]] = None,
    repo_scope: Optional[str] = None,
    valid_until: Optional[datetime] = None,
    promoted_from: Optional[str] = None,
    author_principal_id: Optional[int] = None,
) -> int:
    return await LearningStore.add_learning(
        statement=statement,
        category=category,
        confidence=confidence,
        evidence=evidence,
        repo_scope=repo_scope,
        valid_until=valid_until,
        promoted_from=promoted_from,
        author_principal_id=author_principal_id,
    )


async def list_active_learnings(repo_path: Optional[str] = None) -> List[Learning]:
    return await LearningStore.list_active_learnings(repo_path)
