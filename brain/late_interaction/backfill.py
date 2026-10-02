"""Resumable repository backfill for the additive ColBERT store."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from loguru import logger
from sqlalchemy import and_, func, select

from brain.config.settings import settings
from brain.database.models import File, FileChunk, LateInteractionEmbedding
from brain.database.session import async_session_factory
from brain.workers.db_fencing import assert_current_db_fence
from brain.late_interaction.metrics import increment
from brain.late_interaction.provider import LfmColbertProvider, get_lfm_colbert_provider
from brain.late_interaction.store import upsert_late_interaction_embedding


@dataclass
class LateInteractionBackfillResult:
    repository_id: int
    examined: int = 0
    written: int = 0
    current: int = 0
    failed: int = 0
    truncated: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


async def _work_items(
    repository_id: int,
    limit: int | None,
) -> tuple[list[tuple[int, str]], int]:
    if limit is None or limit <= 0:
        raise ValueError("late-interaction backfill requires a positive bounded limit")
    async with async_session_factory() as session:
        join_condition = (
            (LateInteractionEmbedding.chunk_id == FileChunk.id)
            & (LateInteractionEmbedding.model == settings.LATE_INTERACTION_MODEL)
            & (
                LateInteractionEmbedding.model_revision
                == settings.LATE_INTERACTION_MODEL_REVISION
            )
        )
        database_content_hash = func.encode(
            func.sha256(func.convert_to(FileChunk.content, "UTF8")),
            "hex",
        )
        current_condition = and_(
            LateInteractionEmbedding.id.is_not(None),
            LateInteractionEmbedding.content_hash == database_content_hash,
            LateInteractionEmbedding.dimension == settings.LATE_INTERACTION_DIMENSION,
            LateInteractionEmbedding.vector_dtype == settings.LATE_INTERACTION_STORAGE_DTYPE,
        )
        base = (
            select(FileChunk.id, FileChunk.content)
            .join(File, FileChunk.file_id == File.id)
            .outerjoin(LateInteractionEmbedding, join_condition)
            .where(File.repository_id == repository_id)
        )
        pending_stmt = base.where(~current_condition).order_by(FileChunk.id).limit(limit)
        pending = [(chunk_id, text) for chunk_id, text in (await session.execute(pending_stmt)).all()]
        current_stmt = (
            select(func.count(FileChunk.id))
            .join(File, FileChunk.file_id == File.id)
            .outerjoin(LateInteractionEmbedding, join_condition)
            .where(File.repository_id == repository_id, current_condition)
        )
        current = int((await session.execute(current_stmt)).scalar_one())
    return pending, current


async def backfill_late_interaction(
    repository_id: int,
    *,
    limit: int | None = 100,
    dry_run: bool = False,
    provider: LfmColbertProvider | None = None,
) -> LateInteractionBackfillResult:
    result = LateInteractionBackfillResult(repository_id=repository_id, dry_run=dry_run)
    items, result.current = await _work_items(repository_id, limit)
    result.examined = len(items)
    if dry_run:
        return result
    active_provider = provider or get_lfm_colbert_provider()
    consecutive_failures = 0
    for chunk_id, text in items:
        try:
            encoded = await active_provider.embed(text, is_query=False)
            async with async_session_factory() as session:
                async with session.begin():
                    await assert_current_db_fence(session)
                    await upsert_late_interaction_embedding(
                        session,
                        repository_id=repository_id,
                        chunk_id=chunk_id,
                        source_text=text,
                        encoded=encoded,
                        provider=active_provider,
                    )
            result.written += 1
            result.truncated += int(encoded.truncated)
            increment("dual_write_successes")
            consecutive_failures = 0
        except Exception as exc:
            result.failed += 1
            consecutive_failures += 1
            increment("dual_write_failures")
            logger.warning(
                "Late-interaction backfill failed open for chunk {}: {}",
                chunk_id,
                str(exc).strip() or type(exc).__name__,
            )
            if consecutive_failures >= settings.LATE_INTERACTION_DUAL_WRITE_MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "Late-interaction backfill circuit opened after {} consecutive failures",
                    consecutive_failures,
                )
                break
    return result
