"""Hybrid lexical + vector code search with file-type weighting."""

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import case, false, or_, select, text

from brain.config.settings import settings
from brain.database.models import Embedding, File, FileChunk, Symbol
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import async_session_factory
from brain.embeddings.config import get_embedding_config
from brain.embeddings.constants import VectorSearchStatus
from brain.embeddings.store import EmbeddingDimensionError, validate_vector
from brain.llm import get_embedding_provider
from brain.memory.repo_freshness import assess_repository_freshness
from brain.late_interaction.application import (
    apply_late_interaction,
    late_interaction_requested,
    late_interaction_search_candidate_budget,
)
from brain.search.embedding_cache import get_cached_embedding, set_cached_embedding
from brain.search.filters import (
    HISTORICAL_AUTHORITY_NOTE,
    HISTORICAL_SUMMARY_PREFIX,
    KNOWLEDGE_STATUS_HISTORICAL,
    get_file_type_weight,
    knowledge_authority_weight,
    knowledge_status_from_summary,
    query_requests_historical_context,
    should_exclude_from_retrieval,
)
from brain.search.similarity import cosine_similarity
from brain.search.symbol_ranking import symbol_relevance_order
from brain.embeddings.pgvector_sql import (
    format_pgvector_literal,
    pgvector_cast_type,
    truncate_vector_for_index,
)

ENGLISH_STOPWORDS = frozenset(
    {
        "and",
        "the",
        "for",
        "that",
        "with",
        "from",
        "your",
        "this",
        "have",
        "are",
        "was",
        "were",
        "been",
        "there",
        "their",
        "about",
        "into",
        "would",
        "could",
        "should",
        "which",
        "these",
        "those",
        "them",
        "then",
        "than",
        "when",
        "what",
        "where",
        "will",
        "only",
        "just",
        "also",
        "been",
        "such",
        "over",
        "other",
        "most",
        "after",
        "first",
        "been",
        "each",
        "many",
        "more",
        "some",
        "like",
        "than",
        "into",
        "its",
        "they",
        "them",
    }
)


# File summaries are LLM-generated and run to several paragraphs. Measured on a
# live 5-result response they were 72% of the whole payload (10.9 KB of 17.4 KB,
# ~2.2 KB per file) — and that payload is pasted into an agent's context on every
# search, and into the /ask prompt for every question. The stored summary stays
# whole because the lexical channel matches against it (File.summary ILIKE); only
# what is handed back is trimmed. The opening sentences carry what the file is;
# the rest is elaboration that the returned code chunks already show.
SUMMARY_RESPONSE_CHARS = int(os.environ.get("BRAIN_SUMMARY_RESPONSE_CHARS", "600"))


def _trim_summary(summary: Optional[str]) -> Optional[str]:
    if not summary or SUMMARY_RESPONSE_CHARS <= 0 or len(summary) <= SUMMARY_RESPONSE_CHARS:
        return summary
    return summary[:SUMMARY_RESPONSE_CHARS].rstrip() + "…"


def extract_keywords(query: str) -> List[str]:
    """Extract lowercase keywords from a natural-language query.

    Stopwords are dropped so lexical matching keys on the terms that carry
    meaning. If a query is *nothing but* stopwords ("why did they"), the
    unfiltered tokens are returned instead — searching with weak terms beats
    searching with none, and an empty keyword list would silently disable the
    whole lexical branch.
    """
    tokens = [kw.lower() for kw in re.findall(r"\b[a-zA-Z]{3,}\b", query)]
    meaningful = [kw for kw in tokens if kw not in ENGLISH_STOPWORDS]
    return meaningful or tokens


@dataclass
class VectorSearchResult:
    matches: List[Tuple[float, FileChunk, Optional[File]]] = field(default_factory=list)
    status: str = VectorSearchStatus.OK
    message: str = ""


async def _embed_query(query: str) -> List[float]:
    cached = await get_cached_embedding(query, input_type="query")
    if cached:
        return cached
    provider = get_embedding_provider()
    vector = await provider.embed(query, input_type="query")
    if vector:
        validate_vector(vector)
        await set_cached_embedding(query, vector, input_type="query")
    return vector or []


def _format_pgvector(query_vector: List[float]) -> str:
    config = get_embedding_config()
    indexed = truncate_vector_for_index(query_vector, config.dimension)
    return format_pgvector_literal(indexed)


async def _pgvector_chunk_search(
    session,
    query: str,
    query_vector: List[float],
    top_k: int,
    repository_id: Optional[int],
) -> List[Tuple[float, FileChunk, Optional[File]]]:
    """Use pgvector cosine distance when the embedding column is available."""
    config = get_embedding_config()
    cast_type = pgvector_cast_type(config.dimension)
    vector_literal = _format_pgvector(query_vector)
    repo_filter = "AND f.repository_id = :repository_id" if repository_id is not None else ""
    dim_filter = "AND e.dimension = :dimension"
    sql = text(
        f"""
        SELECT
            fc.id AS chunk_id,
            1 - (e.embedding <=> CAST(:query_vector AS {cast_type})) AS similarity,
            f.id AS file_id,
            f.path AS file_path,
            f.file_type AS file_type
        FROM embeddings e
        JOIN file_chunks fc ON fc.id = e.entity_id AND e.id = fc.embedding_id
        JOIN files f ON f.id = fc.file_id
        WHERE e.entity_type = 'file_chunk'
          AND e.embedding IS NOT NULL
          {dim_filter}
          {repo_filter}
        ORDER BY e.embedding <=> CAST(:query_vector AS {cast_type})
        LIMIT :top_k
        """
    )
    params: Dict[str, Any] = {
        "query_vector": vector_literal,
        "top_k": top_k * 3,
        "dimension": config.dimension,
    }
    if repository_id is not None:
        params["repository_id"] = repository_id

    rows = (await session.execute(sql, params)).mappings().all()
    if len(rows) < top_k * 3 and repository_id is not None:
        # ANN filtering can exhaust its global candidate window before finding
        # enough rows for a small repository. Retry the same canonical query with an
        # exact distance sort; adding zero prevents the ANN ordering shortcut.
        exact_sql = text(str(sql).replace(
            f"ORDER BY e.embedding <=> CAST(:query_vector AS {cast_type})",
            f"ORDER BY (e.embedding <=> CAST(:query_vector AS {cast_type})) + 0",
        ))
        rows = (await session.execute(exact_sql, params)).mappings().all()
    if not rows:
        return []

    chunk_ids = [row["chunk_id"] for row in rows]
    file_ids = [row["file_id"] for row in rows]

    chunks_by_id = {
        chunk.id: chunk
        for chunk in (await session.execute(select(FileChunk).where(FileChunk.id.in_(chunk_ids)))).scalars().all()
    }
    files_by_id = {
        file_obj.id: file_obj
        for file_obj in (await session.execute(select(File).where(File.id.in_(file_ids)))).scalars().all()
    }

    results: List[Tuple[float, FileChunk, Optional[File]]] = []
    for row in rows:
        file_obj = files_by_id.get(row["file_id"])
        if file_obj and should_exclude_from_retrieval(file_obj.path):
            continue
        chunk = chunks_by_id.get(row["chunk_id"])
        if not chunk:
            continue
        weight = get_file_type_weight(file_obj.file_type) if file_obj else 0.7
        authority_weight = (
            knowledge_authority_weight(file_obj.summary, query) if file_obj else 1.0
        )
        results.append(
            (float(row["similarity"]) * weight * authority_weight, chunk, file_obj)
        )
    results.sort(key=lambda item: item[0], reverse=True)
    return results[:top_k]


async def _python_vector_chunk_search(
    session,
    query: str,
    query_vector: List[float],
    top_k: int,
    repository_id: Optional[int],
) -> List[Tuple[float, FileChunk, Optional[File]]]:
    """Explicit dev-only O(n) cosine scan over JSON embeddings."""
    config = get_embedding_config()
    stmt_embeddings = select(Embedding).join(FileChunk, FileChunk.embedding_id == Embedding.id).where(
        Embedding.entity_type == "file_chunk",
        Embedding.entity_id == FileChunk.id,
        Embedding.dimension == config.dimension,
    )
    embeddings = (await session.execute(stmt_embeddings)).scalars().all()

    chunk_ids = [emb.entity_id for emb in embeddings]
    chunks_by_id: Dict[int, FileChunk] = {}
    files_by_id: Dict[int, File] = {}
    if chunk_ids:
        for chunk in (await session.execute(select(FileChunk).where(FileChunk.id.in_(chunk_ids)))).scalars().all():
            chunks_by_id[chunk.id] = chunk

        file_ids = {chunk.file_id for chunk in chunks_by_id.values()}
        if file_ids:
            file_stmt = select(File).where(File.id.in_(list(file_ids)))
            if repository_id is not None:
                file_stmt = file_stmt.where(File.repository_id == repository_id)
            for file_obj in (await session.execute(file_stmt)).scalars().all():
                files_by_id[file_obj.id] = file_obj

    scores: List[Tuple[float, int]] = []
    for emb in embeddings:
        chunk = chunks_by_id.get(emb.entity_id)
        if not chunk:
            continue
        file_obj = files_by_id.get(chunk.file_id)
        if repository_id is not None and file_obj is None:
            continue
        if file_obj and should_exclude_from_retrieval(file_obj.path):
            continue
        weight = get_file_type_weight(file_obj.file_type) if file_obj else 0.7
        authority_weight = (
            knowledge_authority_weight(file_obj.summary, query) if file_obj else 1.0
        )
        scores.append(
            (
                cosine_similarity(query_vector, emb.vector_data)
                * weight
                * authority_weight,
                emb.entity_id,
            )
        )

    scores.sort(key=lambda item: item[0], reverse=True)
    results: List[Tuple[float, FileChunk, Optional[File]]] = []
    for score, chunk_id in scores[:top_k]:
        chunk = chunks_by_id.get(chunk_id)
        if chunk:
            file_obj = files_by_id.get(chunk.file_id)
            results.append((score, chunk, file_obj))
    return results


async def vector_search_chunks(
    query: str,
    top_k: int = 50,
    repository_id: Optional[int] = None,
) -> VectorSearchResult:
    """Return weighted vector matches with explicit pgvector / DEGRADED status."""
    result = VectorSearchResult()
    try:
        query_vector = await _embed_query(query)
        if not query_vector:
            result.status = VectorSearchStatus.DEGRADED
            result.message = "Query embedding unavailable"
            return result

        async with async_session_factory() as session:
            try:
                result.matches = await _pgvector_chunk_search(
                    session,
                    query,
                    query_vector,
                    top_k,
                    repository_id,
                )
                if result.matches:
                    result.status = VectorSearchStatus.OK
                    return result
            except EmbeddingDimensionError as exc:
                result.status = VectorSearchStatus.DIMENSION_MISMATCH
                result.message = str(exc)
                return result
            except Exception as exc:
                logger.warning(f"pgvector search failed: {exc}")
                result.status = VectorSearchStatus.PGVECTOR_UNAVAILABLE
                result.message = str(exc)

        if settings.ALLOW_EMBEDDING_JSON_FALLBACK:
            logger.warning("Using explicit JSON fallback scan (ALLOW_EMBEDDING_JSON_FALLBACK=true)")
            async with async_session_factory() as session:
                result.matches = await _python_vector_chunk_search(
                    session,
                    query,
                    query_vector,
                    top_k,
                    repository_id,
                )
                result.status = VectorSearchStatus.DEGRADED
                result.message = "JSON cosine fallback (dev mode)"
        else:
            if result.status == VectorSearchStatus.OK:
                result.status = VectorSearchStatus.DEGRADED
                result.message = "pgvector returned no matches"
    except EmbeddingDimensionError as exc:
        result.status = VectorSearchStatus.DIMENSION_MISMATCH
        result.message = str(exc)
    except Exception as exc:
        logger.warning(f"Vector chunk search failed: {exc}")
        result.status = VectorSearchStatus.DEGRADED
        result.message = str(exc)
    return result


@dataclass
class CardVectorResult:
    matches: List[Tuple[float, str]] = field(default_factory=list)  # (similarity, path)
    status: str = VectorSearchStatus.OK
    message: str = ""


def _late_chunk_label(chunk: FileChunk, file_obj: Optional[File]) -> str:
    path = file_obj.path if file_obj else ""
    return f"{path}#chunk:{chunk.id}"


def _late_chunk_candidates(
    matches: list[tuple[float, FileChunk, Optional[File]]],
    *,
    repository_id: int,
) -> list:
    from brain.late_interaction.client import LateInteractionCandidate

    candidates = []
    seen_chunk_ids: set[int] = set()
    for _similarity, chunk, file_obj in matches:
        if (
            file_obj is None
            or file_obj.repository_id != repository_id
            or chunk.id in seen_chunk_ids
        ):
            continue
        candidates.append(
            LateInteractionCandidate(
                chunk_id=chunk.id,
                path=file_obj.path,
                content_hash=hashlib.sha256(
                    (chunk.content or "").encode("utf-8", errors="replace")
                ).hexdigest(),
            )
        )
        seen_chunk_ids.add(chunk.id)
        if len(candidates) >= late_interaction_search_candidate_budget():
            break
    return candidates


async def vector_search_cards(
    query: str,
    top_k: int = 50,
    repository_id: Optional[int] = None,
) -> CardVectorResult:
    """v6 P3 — pgvector search over file-card embeddings (entity_type='file_card').

    Returns (similarity, path) tuples. In P3 the card provider equals the default
    embedding provider, so the query embedding is shared with the chunk channel.
    """
    result = CardVectorResult()
    try:
        query_vector = await _embed_query(query)
        if not query_vector:
            result.status = VectorSearchStatus.DEGRADED
            result.message = "Query embedding unavailable"
            return result
        config = get_embedding_config()
        cast_type = pgvector_cast_type(config.dimension)
        vector_literal = _format_pgvector(query_vector)
        repo_filter = "AND fc.repository_id = :repository_id" if repository_id is not None else ""
        sql = text(
            f"""
            SELECT fc.path AS path,
                   f.summary AS file_summary,
                   1 - (e.embedding <=> CAST(:query_vector AS {cast_type})) AS similarity
            FROM embeddings e
            JOIN file_cards fc ON fc.id = e.entity_id
            JOIN files f ON f.id = fc.file_id
            WHERE e.entity_type = 'file_card'
              AND e.embedding IS NOT NULL
              AND e.dimension = :dimension
              {repo_filter}
            ORDER BY e.embedding <=> CAST(:query_vector AS {cast_type})
            LIMIT :top_k
            """
        )
        params: Dict[str, Any] = {
            "query_vector": vector_literal,
            "top_k": top_k * 5,
            "dimension": config.dimension,
        }
        if repository_id is not None:
            params["repository_id"] = repository_id
        async with async_session_factory() as session:
            rows = (await session.execute(sql, params)).mappings().all()
        weighted_rows: List[Tuple[float, str]] = []
        for row in rows:
            path = row["path"]
            if should_exclude_from_retrieval(path):
                continue
            authority_weight = knowledge_authority_weight(
                row["file_summary"],
                query,
            )
            weighted_rows.append(
                (float(row["similarity"]) * authority_weight, path)
            )
        weighted_rows.sort(key=lambda item: item[0], reverse=True)
        result.matches = weighted_rows[:top_k]
        result.status = VectorSearchStatus.OK if result.matches else VectorSearchStatus.DEGRADED
    except Exception as exc:
        logger.warning(f"Card vector search failed: {exc}")
        result.status = VectorSearchStatus.DEGRADED
        result.message = str(exc)
    return result


async def search_code(
    query: str,
    limit: int = 5,
    apply_file_weights: bool = True,
    repository_id: Optional[int] = None,
    repo_path: Optional[str] = None,
    request_id: str | None = None,
    retrieval_mode: str = "fast",
    late_interaction_client=None,
) -> Dict[str, Any]:
    """Search files, symbols, and code chunks for a query string."""
    if retrieval_mode not in {"fast", "deep"}:
        raise ValueError("retrieval_mode must be 'fast' or 'deep'")
    deep_mode = retrieval_mode == "deep"
    repository_scope: Optional[Dict[str, Any]] = None
    if repo_path and repository_id is None:
        repo_record = await get_repository_by_path(repo_path)
        repository_scope = {
            "requested_path": repo_path,
            "found": repo_record is not None,
        }
        if repo_record is None:
            return {
                "files": [],
                "symbols": [],
                "chunks": [],
                "vector_status": VectorSearchStatus.DEGRADED,
                "repository_scope": repository_scope,
            }
        repository_id = repo_record.id
        repository_scope.update(
            {
                "repository_id": repo_record.id,
                "repository_path": repo_record.path,
                "repository_name": repo_record.name,
                "last_indexed_commit": repo_record.last_indexed_commit,
                "indexing_status": repo_record.indexing_status,
            }
        )
        repository_scope["freshness"] = await assess_repository_freshness(repo_record)

    keywords = extract_keywords(query)
    files_found: List[Dict[str, Any]] = []
    symbols_found: List[Dict[str, Any]] = []
    chunks_found: List[Dict[str, Any]] = []
    vector_status = VectorSearchStatus.OK
    late_requested = repository_id is not None and (
        deep_mode or late_interaction_requested(repository_id, query)
    )
    late_debug: Dict[str, Any] = {
        "status": "disabled",
        "execution_mode": retrieval_mode,
        "applied": False,
    }

    async with async_session_factory() as session:
        if keywords:
            file_clauses = (
                [File.path.ilike(f"%{kw}%") for kw in keywords]
                + [File.summary.ilike(f"%{kw}%") for kw in keywords]
            )
            historical_order = case(
                (File.summary.startswith(HISTORICAL_SUMMARY_PREFIX), 1),
                else_=0,
            )
            if query_requests_historical_context(query):
                historical_order = case(
                    (File.summary.startswith(HISTORICAL_SUMMARY_PREFIX), 0),
                    else_=1,
                )
            stmt_files = (
                select(File)
                .where(or_(*file_clauses))
                .order_by(historical_order, File.path)
                .limit(limit * 10)
            )
            if repository_id is not None:
                stmt_files = stmt_files.where(File.repository_id == repository_id)
            res_files = await session.execute(stmt_files)
            file_records = [
                file_record
                for file_record in res_files.scalars().all()
                if not should_exclude_from_retrieval(file_record.path)
            ]
            if not query_requests_historical_context(query):
                file_records.sort(
                    key=lambda file_record: (
                        knowledge_status_from_summary(file_record.summary)
                        == KNOWLEDGE_STATUS_HISTORICAL
                    )
                )
            for file_record in file_records:
                if should_exclude_from_retrieval(file_record.path):
                    continue
                status = knowledge_status_from_summary(file_record.summary)
                weight = (
                    get_file_type_weight(file_record.file_type)
                    if apply_file_weights
                    else 1.0
                )
                weight *= knowledge_authority_weight(file_record.summary, query)
                item = {
                    "path": file_record.path,
                    "language": file_record.language,
                    "summary": _trim_summary(file_record.summary),
                    "file_type": file_record.file_type,
                    "weight": weight,
                    "knowledge_status": status,
                }
                if status == KNOWLEDGE_STATUS_HISTORICAL:
                    item["authority_note"] = HISTORICAL_AUTHORITY_NOTE
                files_found.append(item)
                if len(files_found) >= limit:
                    break

            # Guarded like the file branch above: or_() with zero clauses is an
            # error in SQLAlchemy 2.x, and keywords can now legitimately be
            # empty for an all-stopword query.
            sym_clauses = (
                [Symbol.name.ilike(f"%{kw}%") for kw in keywords]
                + [Symbol.summary.ilike(f"%{kw}%") for kw in keywords]
            )
            stmt_symbols = select(Symbol, File.path).join(File, Symbol.file_id == File.id)
            stmt_symbols = stmt_symbols.where(
                or_(*sym_clauses) if sym_clauses else false()
            ).order_by(*symbol_relevance_order(keywords)).limit(limit * 2)
            if repository_id is not None:
                stmt_symbols = stmt_symbols.where(File.repository_id == repository_id)
            res_symbols = await session.execute(stmt_symbols)
            for sym, symbol_path in res_symbols.all():
                symbols_found.append(
                    {
                        "name": sym.name,
                        "kind": sym.kind,
                        "signature": sym.signature,
                        "summary": sym.summary,
                        # Locator consumers need an exact file/range instead of
                        # forcing a second discovery call after a symbol hit.
                        "path": symbol_path,
                        "start_line": sym.start_line,
                        "end_line": sym.end_line,
                    }
                )
                if len(symbols_found) >= limit:
                    break

    # Release the lexical/symbol session before vector sessions, remote scoring,
    # and durable shadow writes. The authoritative baseline always uses the
    # historical top_k=limit query; LFM gets a separate wider candidate query.
    try:
        vector_result = await vector_search_chunks(
            query,
            top_k=limit,
            repository_id=repository_id,
        )
        vector_status = vector_result.status
        selected_matches = list(vector_result.matches[:limit])
        if late_requested:
            assert repository_id is not None
            wide_matches: list[tuple[float, FileChunk, Optional[File]]] = []
            try:
                wide_result = await vector_search_chunks(
                    query,
                    top_k=max(
                        limit,
                        settings.LATE_INTERACTION_DEEP_VECTOR_LIMIT
                        if deep_mode
                        else late_interaction_search_candidate_budget(),
                    ),
                    repository_id=repository_id,
                )
                wide_matches = list(wide_result.matches)
            except Exception as exc:
                logger.warning(
                    f"Late-interaction wide vector search failed open: {exc}"
                )

            try:
                late_candidates = _late_chunk_candidates(
                    wide_matches,
                    repository_id=repository_id,
                )
            except Exception as exc:
                logger.warning(f"Late-interaction search candidate build failed open: {exc}")
                late_candidates = []

            baseline_labels = [
                _late_chunk_label(chunk, chunk_file)
                for _similarity, chunk, chunk_file in selected_matches
            ]
            matches_by_label = {
                _late_chunk_label(chunk, chunk_file): match
                for match in wide_matches
                for _similarity, chunk, chunk_file in [match]
            }

            def _build_chunk_counterfactual(remote_result) -> list[str]:
                score_by_chunk_id = {
                    int(score.chunk_id): float(score.score)
                    for score in remote_result.scores
                }
                ranked_matches = sorted(
                    enumerate(wide_matches),
                    key=lambda item: (
                        0
                        if item[1][1].id in score_by_chunk_id
                        else 1,
                        -score_by_chunk_id.get(item[1][1].id, 0.0),
                        item[0],
                    ),
                )
                return [
                    _late_chunk_label(chunk, chunk_file)
                    for _rank, (_similarity, chunk, chunk_file) in ranked_matches[:limit]
                ]

            late_application = await apply_late_interaction(
                query=query,
                repository_id=repository_id,
                candidates=late_candidates,
                baseline_top_k=baseline_labels,
                build_counterfactual=_build_chunk_counterfactual,
                query_class="code_search",
                index_revision=str(
                    (repository_scope or {}).get("last_indexed_commit") or ""
                ),
                request_id=request_id,
                mode="deep" if deep_mode else "default",
                client=late_interaction_client,
            )
            late_debug = late_application.to_debug()
            if deep_mode and not late_application.applied:
                raise RuntimeError(
                    "deep LFM rerank failed closed: "
                    f"{late_application.status}:"
                    f"{late_application.reason or 'not_applied'}"
                )
            if late_application.applied:
                selected_matches = [
                    matches_by_label[label]
                    for label in late_application.effective_top_k
                    if label in matches_by_label
                ][:limit]

        for weighted_sim, chunk, chunk_file in selected_matches:
            status = knowledge_status_from_summary(
                chunk_file.summary if chunk_file else None
            )
            item = {
                "similarity": weighted_sim,
                "content": chunk.content,
                "summary": chunk.summary,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "file_path": chunk_file.path if chunk_file else None,
                "file_type": chunk_file.file_type if chunk_file else None,
                "knowledge_status": status,
            }
            if status == KNOWLEDGE_STATUS_HISTORICAL:
                item["authority_note"] = HISTORICAL_AUTHORITY_NOTE
            chunks_found.append(item)
    except Exception as exc:
        if deep_mode:
            raise
        logger.warning(f"Vector search failed: {exc}")
        vector_status = VectorSearchStatus.DEGRADED

    response: Dict[str, Any] = {
        "files": files_found[:limit],
        "symbols": symbols_found[:limit],
        "chunks": chunks_found[:limit],
        "vector_status": vector_status,
    }
    if repository_scope is not None:
        response["repository_scope"] = repository_scope
    if late_requested:
        response["late_interaction"] = late_debug
    response["retrieval_mode"] = retrieval_mode
    return response
