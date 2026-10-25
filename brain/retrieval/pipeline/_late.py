"""Late-interaction (ColBERT) rerank stage: candidate resolution, canary, counterfactual."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Awaitable, Optional, TypeVar
from sqlalchemy import case, select, true
from brain.config.settings import settings
from brain.database.models import File, FileChunk
from brain.database.session import async_session_factory
from brain.late_interaction.application import late_interaction_candidate_budget, late_interaction_canary_selected
from brain.retrieval.reranker import RerankCandidate, rerank_top_k
from brain.retrieval.types import ChannelCandidate


def _late_canary_selected(repository_id: int, query: str) -> bool:
    """Backward-compatible test/import surface for the shared sampler."""
    return late_interaction_canary_selected(repository_id, query)


_LateResult = TypeVar("_LateResult")


async def _await_late_rerank(
    awaitable: Awaitable[_LateResult],
) -> tuple[Optional[_LateResult], bool]:
    try:
        result = await asyncio.wait_for(
            awaitable,
            timeout=settings.LATE_INTERACTION_RERANK_TIMEOUT_S,
        )
        return result, False
    except asyncio.TimeoutError:
        return None, True


def _apply_late_interaction_scores(
    candidates: list[ChannelCandidate],
    scores: dict[str, float],
) -> int:
    """Apply a bounded rank bonus; raw MaxSim scales are not cross-query comparable."""
    ranked_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    score_rank = {path: rank for rank, (path, _score) in enumerate(ranked_scores, 1)}
    denominator = max(1, len(score_rank) - 1)
    applied = 0
    for candidate in candidates:
        rank = score_rank.get(candidate.item_id)
        if rank is None:
            continue
        normalized = 1.0 - ((rank - 1) / denominator)
        candidate.reranker_score += settings.LATE_INTERACTION_SCORE_WEIGHT * normalized
        candidate.metadata["late_interaction_score"] = scores[candidate.item_id]
        candidate.metadata["late_interaction_rank"] = rank
        applied += 1
    candidates.sort(key=lambda candidate: candidate.reranker_score, reverse=True)
    return applied


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


async def _resolve_late_interaction_candidates(
    *,
    repository_id: int,
    pool_paths: list[str],
    dense_matches: list[tuple[float, FileChunk, Optional[File]]],
) -> list:
    """Resolve a bounded, repository-scoped chunk pool.

    Dense provenance is retained first in its original similarity order.  Any
    remaining budget is filled by file-pool order and then ``FileChunk.id``.
    """
    from brain.late_interaction.client import LateInteractionCandidate

    budget = late_interaction_candidate_budget()
    ordered_paths = list(dict.fromkeys(pool_paths))
    allowed_paths = set(ordered_paths)
    selected: list = []
    seen_chunk_ids: set[int] = set()

    for _similarity, chunk, file_obj in dense_matches:
        path = file_obj.path if file_obj else ""
        if (
            not path
            or getattr(file_obj, "repository_id", repository_id) != repository_id
            or path not in allowed_paths
            or chunk.id in seen_chunk_ids
        ):
            continue
        selected.append(
            LateInteractionCandidate(
                chunk_id=chunk.id,
                path=path,
                content_hash=_content_hash(chunk.content or ""),
            )
        )
        seen_chunk_ids.add(chunk.id)
        if len(selected) >= budget:
            return selected

    remaining = budget - len(selected)
    if remaining <= 0 or not ordered_paths:
        return selected

    path_priority = case(
        {path: rank for rank, path in enumerate(ordered_paths)},
        value=File.path,
        else_=len(ordered_paths),
    )
    async with async_session_factory() as session:
        stmt = (
            select(FileChunk.id, FileChunk.content, File.path)
            .join(File, FileChunk.file_id == File.id)
            .where(
                File.repository_id == repository_id,
                File.path.in_(ordered_paths),
                FileChunk.id.notin_(seen_chunk_ids) if seen_chunk_ids else true(),
            )
            .order_by(path_priority, FileChunk.id)
            .limit(remaining)
        )
        rows = (await session.execute(stmt)).all()
    for chunk_id, content, path in rows:
        selected.append(
            LateInteractionCandidate(
                chunk_id=chunk_id,
                path=path,
                content_hash=_content_hash(content or ""),
            )
        )
    return selected


def _build_rerank_pool(
    pool_candidates: list[ChannelCandidate],
    *,
    path_to_file: dict[str, File],
    vector_by_path: dict[str, float],
    lexical_by_path: dict[str, float],
    symbol_by_path: dict[str, float],
    graph_by_path: dict[str, float],
    card_by_path: dict[str, float],
    card_rerank: bool,
) -> list[RerankCandidate]:
    return [
        RerankCandidate(
            path=candidate.item_id,
            reranker_score=candidate.reranker_score,
            lexical_score=lexical_by_path.get(candidate.item_id, 0.0),
            vector_score=vector_by_path.get(candidate.item_id, 0.0),
            graph_score=graph_by_path.get(candidate.item_id, 0.0),
            symbol_score=symbol_by_path.get(candidate.item_id, 0.0),
            card_score=card_by_path.get(candidate.item_id, 0.0) if card_rerank else 0.0,
            summary=(path_to_file[candidate.item_id].summary or "")
            if candidate.item_id in path_to_file
            else "",
        )
        for candidate in pool_candidates
    ]


async def _rerank_late_counterfactual(
    rerank_pool: list[RerankCandidate],
    *,
    task_description: str,
    task_type: str,
    repository_name: str,
    precision_k: int,
    expected_surface: str,
    two_stage: bool,
    commit_hash: str,
):
    """Run the LFM arm outside the production cache and without a second LLM call."""
    return await rerank_top_k(
        rerank_pool,
        task_description=task_description,
        task_type=task_type,
        repo_hash=repository_name,
        top_k=precision_k,
        use_cache=False,
        use_llm=False,
        expected_surface=expected_surface,
        two_stage=two_stage,
        commit_hash=commit_hash,
    )
