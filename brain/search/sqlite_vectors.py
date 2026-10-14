"""Scoped streaming exact cosine over a bounded SQLite float32 corpus."""
import heapq

import numpy as np
from sqlalchemy import select

from brain.config.settings import settings
from brain.database.models import File, FileChunk, Embedding
from brain.embeddings.config import get_embedding_config, content_hash
from brain.search.filters import should_exclude_from_retrieval, get_file_type_weight, knowledge_authority_weight


async def sqlite_vector_search(session, query, query_vector, top_k, repository_id):
    config = get_embedding_config()
    stmt = select(Embedding, FileChunk, File).join(FileChunk, FileChunk.embedding_id == Embedding.id).join(
        File, FileChunk.file_id == File.id).where(
            Embedding.entity_type == "file_chunk", Embedding.entity_id == FileChunk.id,
            Embedding.dimension == config.dimension, Embedding.provider == config.provider, Embedding.model == config.model)
    if repository_id is not None:
        stmt = stmt.where(File.repository_id == repository_id)
    stmt = stmt.order_by(Embedding.id).execution_options(yield_per=128)
    query_array = np.asarray(query_vector, dtype=np.float32)
    norm = float(np.linalg.norm(query_array))
    heap: list[tuple[float, int, FileChunk, File]] = []
    count = 0
    async for embedding, chunk, file in await session.stream(stmt):
        count += 1
        if count > max(1, settings.BRAIN_LITE_MAX_CHUNKS):
            raise ValueError("Lite vector corpus exceeds configured chunk limit; use full mode")
        if should_exclude_from_retrieval(file.path) or embedding.content_hash != content_hash(chunk.content):
            continue
        array = np.asarray(embedding.embedding, dtype=np.float32)
        if array.shape != query_array.shape or not np.isfinite(array).all():
            continue
        denominator = norm * float(np.linalg.norm(array))
        score = float(np.dot(query_array, array)) / denominator if denominator else 0.0
        score *= get_file_type_weight(file.file_type) * knowledge_authority_weight(file.summary, query)
        record = (score, -chunk.id, chunk, file)
        if len(heap) < top_k:
            heapq.heappush(heap, record)
        elif record[:2] > heap[0][:2]:
            heapq.heapreplace(heap, record)
    return [(score, chunk, file) for score, _, chunk, file in sorted(heap, reverse=True)]
