"""Persistence, coverage and MaxSim reranking for ColBERT matrices."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.config.settings import settings
from brain.database.models import File, FileChunk, LateInteractionEmbedding
from brain.database.session import async_session_factory
from brain.late_interaction.codec import decode_matrix, encode_matrix, maxsim_score
from brain.late_interaction.metrics import increment, observe
from brain.late_interaction.provider import (
    EncodedText,
    LfmColbertProvider,
    get_lfm_colbert_provider,
)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class LateInteractionInventory:
    repository_id: int | None
    model: str
    model_revision: str
    total_chunks: int = 0
    current_chunks: int = 0
    stale_chunks: int = 0
    missing_chunks: int = 0
    stored_bytes: int = 0
    token_vectors: int = 0
    truncated_chunks: int = 0

    @property
    def coverage_pct(self) -> float:
        if not self.total_chunks:
            return 0.0
        return round((self.current_chunks / self.total_chunks) * 100.0, 2)

    def to_dict(self) -> dict:
        return {**asdict(self), "coverage_pct": self.coverage_pct}


@dataclass
class LateInteractionRerankResult:
    status: str
    scores: dict[str, float] = field(default_factory=dict)
    candidate_count: int = 0
    resolvable_candidates: int = 0
    fully_covered_candidates: int = 0
    candidate_chunks: int = 0
    chunk_budget: int = 0
    coverage_ratio: float = 0.0
    latency_ms: float = 0.0
    truncated_query: bool = False
    error: str = ""

    def to_debug(self) -> dict:
        return {
            "status": self.status,
            "candidate_count": self.candidate_count,
            "resolvable_candidates": self.resolvable_candidates,
            "fully_covered_candidates": self.fully_covered_candidates,
            "candidate_chunks": self.candidate_chunks,
            "chunk_budget": self.chunk_budget,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "latency_ms": round(self.latency_ms, 2),
            "truncated_query": self.truncated_query,
            "error": self.error,
            "top_scores": [
                {"path": path, "score": round(score, 4)}
                for path, score in sorted(self.scores.items(), key=lambda item: item[1], reverse=True)[:10]
            ],
        }


async def upsert_late_interaction_embedding(
    session: AsyncSession,
    *,
    repository_id: int,
    chunk_id: int,
    source_text: str,
    encoded: EncodedText,
    provider: LfmColbertProvider,
) -> None:
    payload = encode_matrix(encoded.vectors, dtype=settings.LATE_INTERACTION_STORAGE_DTYPE)
    values = {
        "repository_id": repository_id,
        "chunk_id": chunk_id,
        "provider": provider.provider,
        "model": provider.model,
        "model_revision": provider.model_revision,
        "dimension": provider.dimension,
        "token_count": encoded.token_count,
        "vector_dtype": settings.LATE_INTERACTION_STORAGE_DTYPE,
        "vector_data": payload,
        "content_hash": content_hash(source_text),
        "was_truncated": encoded.truncated,
    }
    stmt = pg_insert(LateInteractionEmbedding).values(**values)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=["chunk_id", "model", "model_revision"],
        set_={
            "repository_id": excluded.repository_id,
            "provider": excluded.provider,
            "dimension": excluded.dimension,
            "token_count": excluded.token_count,
            "vector_dtype": excluded.vector_dtype,
            "vector_data": excluded.vector_data,
            "content_hash": excluded.content_hash,
            "was_truncated": excluded.was_truncated,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def collect_late_interaction_inventory(repository_id: int | None) -> LateInteractionInventory:
    inventory = LateInteractionInventory(
        repository_id=repository_id,
        model=settings.LATE_INTERACTION_MODEL,
        model_revision=settings.LATE_INTERACTION_MODEL_REVISION,
    )
    async with async_session_factory() as session:
        database_content_hash = func.encode(
            func.sha256(func.convert_to(FileChunk.content, "UTF8")),
            "hex",
        )
        current_condition = (
            LateInteractionEmbedding.id.is_not(None)
            & (LateInteractionEmbedding.dimension == settings.LATE_INTERACTION_DIMENSION)
            & (
                LateInteractionEmbedding.vector_dtype
                == settings.LATE_INTERACTION_STORAGE_DTYPE
            )
            & (LateInteractionEmbedding.content_hash == database_content_hash)
        )
        stmt = (
            select(
                func.count(FileChunk.id),
                func.count(FileChunk.id).filter(current_condition),
                func.count(FileChunk.id).filter(LateInteractionEmbedding.id.is_(None)),
                func.coalesce(func.sum(func.octet_length(LateInteractionEmbedding.vector_data)), 0),
                func.coalesce(func.sum(LateInteractionEmbedding.token_count), 0),
                func.count(FileChunk.id).filter(LateInteractionEmbedding.was_truncated.is_(True)),
            )
            .join(File, FileChunk.file_id == File.id)
            .outerjoin(
                LateInteractionEmbedding,
                (LateInteractionEmbedding.chunk_id == FileChunk.id)
                & (LateInteractionEmbedding.model == settings.LATE_INTERACTION_MODEL)
                & (
                    LateInteractionEmbedding.model_revision
                    == settings.LATE_INTERACTION_MODEL_REVISION
                ),
            )
        )
        if repository_id is not None:
            stmt = stmt.where(File.repository_id == repository_id)
        (
            inventory.total_chunks,
            inventory.current_chunks,
            inventory.missing_chunks,
            inventory.stored_bytes,
            inventory.token_vectors,
            inventory.truncated_chunks,
        ) = (int(value or 0) for value in (await session.execute(stmt)).one())
        inventory.stale_chunks = (
            inventory.total_chunks - inventory.current_chunks - inventory.missing_chunks
        )
    return inventory


async def _candidate_coverage(
    repository_id: int,
    paths: Iterable[str],
) -> tuple[dict[str, int], int, int]:
    selected_paths = list(dict.fromkeys(paths))
    if not selected_paths:
        return {}, 0, 0
    async with async_session_factory() as session:
        expected_stmt = (
            select(File.path, func.count(FileChunk.id))
            .join(FileChunk, FileChunk.file_id == File.id)
            .where(File.repository_id == repository_id, File.path.in_(selected_paths))
            .group_by(File.path)
        )
        expected = {path: int(count) for path, count in (await session.execute(expected_stmt)).all()}
        database_content_hash = func.encode(
            func.sha256(func.convert_to(FileChunk.content, "UTF8")),
            "hex",
        )
        current_stmt = (
            select(File.path, func.count(FileChunk.id))
            .join(FileChunk, FileChunk.file_id == File.id)
            .join(LateInteractionEmbedding, LateInteractionEmbedding.chunk_id == FileChunk.id)
            .where(
                File.repository_id == repository_id,
                File.path.in_(selected_paths),
                LateInteractionEmbedding.model == settings.LATE_INTERACTION_MODEL,
                LateInteractionEmbedding.model_revision == settings.LATE_INTERACTION_MODEL_REVISION,
                LateInteractionEmbedding.dimension == settings.LATE_INTERACTION_DIMENSION,
                LateInteractionEmbedding.vector_dtype == settings.LATE_INTERACTION_STORAGE_DTYPE,
                LateInteractionEmbedding.content_hash == database_content_hash,
            )
            .group_by(File.path)
        )
        current = {
            path: int(count)
            for path, count in (await session.execute(current_stmt)).all()
        }
    fully_covered = {
        path: chunk_count
        for path, chunk_count in expected.items()
        if chunk_count > 0 and current.get(path, 0) == chunk_count
    }
    return fully_covered, len(expected), sum(expected.values())


async def _score_candidate_matrices(
    repository_id: int,
    expected_chunks: dict[str, int],
    query_vectors: np.ndarray,
) -> dict[str, float]:
    if not expected_chunks:
        return {}
    database_content_hash = func.encode(
        func.sha256(func.convert_to(FileChunk.content, "UTF8")),
        "hex",
    )
    async with async_session_factory() as session:
        stmt = (
            select(
                File.path,
                LateInteractionEmbedding.token_count,
                LateInteractionEmbedding.dimension,
                LateInteractionEmbedding.vector_dtype,
                LateInteractionEmbedding.vector_data,
            )
            .join(FileChunk, FileChunk.file_id == File.id)
            .join(LateInteractionEmbedding, LateInteractionEmbedding.chunk_id == FileChunk.id)
            .where(
                File.repository_id == repository_id,
                File.path.in_(list(expected_chunks)),
                LateInteractionEmbedding.model == settings.LATE_INTERACTION_MODEL,
                LateInteractionEmbedding.model_revision == settings.LATE_INTERACTION_MODEL_REVISION,
                LateInteractionEmbedding.dimension == settings.LATE_INTERACTION_DIMENSION,
                LateInteractionEmbedding.vector_dtype == settings.LATE_INTERACTION_STORAGE_DTYPE,
                LateInteractionEmbedding.content_hash == database_content_hash,
            )
            .order_by(File.path, FileChunk.id)
        )
        rows = await session.stream(stmt.execution_options(yield_per=8))
        scores: dict[str, float] = {}
        observed_chunks: dict[str, int] = {}
        async for path, token_count, dimension, vector_dtype, vector_data in rows:
            matrix = decode_matrix(
                vector_data,
                token_count=token_count,
                dimension=dimension,
                dtype=vector_dtype,
            )
            score = maxsim_score(query_vectors, matrix)
            scores[path] = max(scores.get(path, float("-inf")), score)
            observed_chunks[path] = observed_chunks.get(path, 0) + 1
    return {
        path: score
        for path, score in scores.items()
        if observed_chunks.get(path, 0) == expected_chunks[path]
    }


async def rerank_candidate_paths(
    query: str,
    *,
    repository_id: int,
    paths: Iterable[str],
    provider: LfmColbertProvider | None = None,
) -> LateInteractionRerankResult:
    started = time.perf_counter()
    increment("rerank_requests")
    selected_paths = list(dict.fromkeys(paths))[: settings.LATE_INTERACTION_RERANK_LIMIT]
    result = LateInteractionRerankResult(status="skipped", candidate_count=len(selected_paths))
    try:
        fully_covered, expected_count, candidate_chunks = await _candidate_coverage(
            repository_id,
            selected_paths,
        )
        result.resolvable_candidates = expected_count
        result.fully_covered_candidates = len(fully_covered)
        result.candidate_chunks = candidate_chunks
        result.chunk_budget = settings.LATE_INTERACTION_MAX_RERANK_CHUNKS
        result.coverage_ratio = len(fully_covered) / len(selected_paths) if selected_paths else 0.0
        if result.coverage_ratio < settings.LATE_INTERACTION_MIN_CANDIDATE_COVERAGE:
            result.status = "insufficient_coverage"
            return result
        if candidate_chunks > settings.LATE_INTERACTION_MAX_RERANK_CHUNKS:
            result.status = "chunk_budget_exceeded"
            return result

        active_provider = provider or get_lfm_colbert_provider()
        query_embedding = await active_provider.embed(query, is_query=True)
        result.truncated_query = query_embedding.truncated
        result.scores = await _score_candidate_matrices(
            repository_id,
            fully_covered,
            query_embedding.vectors,
        )
        if len(result.scores) != len(fully_covered):
            result.scores = {}
            result.status = "coverage_changed"
            return result
        result.status = "scored"
        return result
    except Exception as exc:
        increment("rerank_fail_open")
        result.status = "failed_open"
        result.error = str(exc).strip() or type(exc).__name__
        return result
    finally:
        result.latency_ms = (time.perf_counter() - started) * 1000
        observe("rerank_latency_ms_total", result.latency_ms)
