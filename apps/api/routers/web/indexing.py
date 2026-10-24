"""Indexing-run listing endpoint and row shaping."""
from __future__ import annotations

from typing import Any
from fastapi import Query
from sqlalchemy import String, cast, func, select
from brain.database.models import IndexingRun
from brain.database.session import async_session_factory
from ._common import (REPOSITORY_READ, _as_dict, _as_int, _iso, _parse_dt, _repository_dict, _resolve_repository, router)


def shape_index_run(run: Any) -> dict[str, Any]:
    progress = _as_dict(run.progress)
    verification = _as_dict(run.verification)
    file_counts = _as_dict(run.file_counts) or _as_dict(verification.get("file_counts"))
    progress_files = _as_dict(progress.get("files"))
    progress_chunks = _as_dict(progress.get("chunks"))

    discovered = _as_int(file_counts.get("discovered"))
    if discovered is None:
        discovered = _as_int(progress_files.get("discovered"))
    changed = _as_int(file_counts.get("indexed"))
    chunks = _as_int(progress_chunks.get("processed"))

    completeness = None
    excluded = _as_int(file_counts.get("excluded")) or 0
    failed = _as_int(file_counts.get("failed"))
    if failed is None:
        failed = _as_int(progress_files.get("failed"))
    if discovered is not None and failed is not None:
        eligible = discovered - excluded
        if eligible > 0:
            completeness = round(max(eligible - failed, 0) / eligible, 4)

    duration = verification.get("duration_seconds")
    duration_seconds: float | None = float(duration) if isinstance(duration, (int, float)) else None
    if duration_seconds is None:
        started = _parse_dt(run.started_at)
        completed = _parse_dt(run.completed_at)
        if started is not None and completed is not None and completed >= started:
            duration_seconds = round((completed - started).total_seconds(), 3)

    return {
        "id": run.id,
        "repository_id": run.repository_id,
        "revision": run.commit_hash,
        # Not recorded: indexing_runs carries no trigger column.
        "trigger": None,
        "status": (run.status or "").lower() or None,
        "completeness": completeness,
        "files": discovered,
        "changed": changed,
        "chunks": chunks,
        "started_at": _iso(run.started_at),
        "duration_seconds": duration_seconds,
        # Tracked files edited but not committed when the run read the committed HEAD.
        "uncommitted_changes": _as_int(verification.get("uncommitted_changes")) or 0,
    }


@router.get("/index-runs", dependencies=[REPOSITORY_READ])
async def web_index_runs(
    repository_id: int | None = None,
    limit: int = Query(20, ge=1, le=200),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=200),
    q: str | None = Query(None, max_length=500),
    status: str | None = Query(None, max_length=50),
) -> dict[str, Any]:
    repository = await _resolve_repository(repository_id)
    if repository is None:
        return {"repository": None, "runs": []}
    async with async_session_factory() as session:
        size = page_size or limit
        where = [IndexingRun.repository_id == repository.id]
        if q:
            needle = f"%{q.strip()}%"
            where.append(IndexingRun.commit_hash.ilike(needle) | cast(IndexingRun.id, String).ilike(needle))
        facet_where = list(where)
        if status:
            where.append(func.lower(IndexingRun.status) == status.lower())
        total = int((await session.execute(select(func.count()).select_from(IndexingRun).where(*where))).scalar() or 0)
        facet_rows = (
            await session.execute(
                select(IndexingRun.status, func.count())
                .where(*facet_where)
                .group_by(IndexingRun.status)
            )
        ).all()
        runs = (
            (
                await session.execute(
                    select(IndexingRun)
                    .where(*where)
                    .order_by(IndexingRun.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )
    return {
        "repository": _repository_dict(repository),
        "runs": [shape_index_run(run) for run in runs],
        "total": total,
        "page": page,
        "page_size": size,
        "facets": {str(k): int(v) for k, v in facet_rows},
        "total_pages": max(1, (total + size - 1) // size),
    }
