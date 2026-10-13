"""Opt-in maintenance prose enrichment; never part of indexing readiness."""

import asyncio
from pathlib import Path

from sqlalchemy import select, update

from brain.config.settings import settings
from brain.database.models import File
from brain.database.session import async_session_factory
from brain.embeddings.config import content_hash
from brain.llm.router import get_model_router
from brain.memory.source_overlay import read_live_source
from brain.workers.db_fencing import assert_current_db_fence
from brain.workers.runtime import check_job_lease


async def enrich_summaries(repository_id: int, repo_path: Path) -> dict[str, int]:
    """Summarize a bounded current-source snapshot, then compare-and-set prose.

    Provider latency occurs outside transactions. Changed/deleted projections
    cannot receive results from an older snapshot; chunks and vectors stay intact.
    """
    async with async_session_factory() as session:
        rows = (await session.execute(select(File.id, File.path, File.hash).where(
            File.repository_id == repository_id).order_by(File.id).limit(
                max(1, settings.INDEX_SUMMARY_ENRICH_MAX_FILES)))).all()
    counts = {"updated": 0, "skipped": 0, "failed": 0}
    for file_id, path, digest in rows:
        await check_job_lease()
        source = await asyncio.to_thread(read_live_source, repo_path, path, 10 * 1024 * 1024)
        content = "".join(source.lines).replace("\r\n", "\n") if source else ""
        if source is None or source.truncated or content_hash(content) != digest:
            counts["skipped"] += 1
            continue
        try:
            prose = await get_model_router().summarizer().summarize(content[:4000])
        except Exception:
            counts["failed"] += 1
            continue
        # Source may have changed during synthesis, even before a reindex commit.
        current = await asyncio.to_thread(read_live_source, repo_path, path, 10 * 1024 * 1024)
        current_content = "".join(current.lines).replace("\r\n", "\n") if current else ""
        if current is None or current.truncated or content_hash(current_content) != digest:
            counts["skipped"] += 1
            continue
        async with async_session_factory() as session, session.begin():
            await assert_current_db_fence(session)
            result = await session.execute(update(File).where(
                File.id == file_id, File.repository_id == repository_id, File.hash == digest
            ).values(summary=str(prose)[:3200]).returning(File.id))
            counts["updated" if result.scalar_one_or_none() is not None else "skipped"] += 1
    return counts
