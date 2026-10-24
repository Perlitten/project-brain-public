"""Overview and orchestration-health endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException, Query
from loguru import logger
from sqlalchemy import select
from apps.api.helpers import (get_db_counts, get_embedding_retrieval_health, get_recent_brain_jobs, get_self_diagnosis_status)
from brain.database.models import IndexingRun, Repository
from brain.database.session import async_session_factory, check_health
from ._common import (REPOSITORY_READ, TEAM_ADMIN, _as_int, _iso, _parse_dt, _pct, _repository_dict, _resolve_repository, router)
from .indexing import shape_index_run


def shape_job(job: dict[str, Any]) -> dict[str, Any]:
    created = _parse_dt(job.get("created_at"))
    updated = _parse_dt(job.get("updated_at"))
    status = str(job.get("status") or "") or None
    duration = None
    if created is not None and updated is not None and status not in ("queued", "processing", "running"):
        delta = (updated - created).total_seconds()
        duration = round(delta, 3) if delta >= 0 else None
    detail: str | None = None
    error = job.get("error")
    result = job.get("result")
    if isinstance(error, str) and error.strip():
        detail = error.strip()
    elif isinstance(result, str) and result.strip():
        detail = result.strip()
    elif isinstance(result, dict):
        for key in ("message", "summary", "status"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    if detail is not None and len(detail) > 300:
        detail = detail[:297] + "..."
    return {
        "id": job.get("id"),
        "kind": job.get("type"),
        "status": status,
        "started_at": _iso(created),
        "duration": duration,
        "detail": detail,
    }


async def _repository_freshness(record: Repository) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "indexed_commit": None,
        "head_commit": None,
        "commits_behind": None,
        "origin_ref": None,
        "origin_head": None,
        "commits_behind_origin": None,
        "status": None,
        "note": None,
        "last_indexed_at": None,
    }
    try:
        from brain.memory.repo_freshness import assess_repository_freshness

        freshness = await assess_repository_freshness(record)
    except Exception:  # noqa: BLE001 — a failed probe is not a verdict
        return facts
    facts["indexed_commit"] = freshness.get("indexed_commit")
    facts["head_commit"] = freshness.get("source_head_commit")
    facts["commits_behind"] = _as_int(freshness.get("commits_behind"))
    facts["origin_ref"] = freshness.get("origin_ref")
    facts["origin_head"] = freshness.get("origin_head")
    facts["commits_behind_origin"] = _as_int(freshness.get("commits_behind_origin"))
    facts["status"] = freshness.get("status")
    facts["note"] = freshness.get("note")
    last = freshness.get("last_indexed_at")
    facts["last_indexed_at"] = _iso(last) if isinstance(last, datetime) else last
    return facts


def build_coverage(embedding_health: dict[str, Any], total_chunks: int | None) -> dict[str, Any]:
    eligible = _as_int(embedding_health.get("total_eligible_chunks"))
    populated = _as_int(embedding_health.get("pgvector_populated"))
    current = _as_int(embedding_health.get("chunks_with_current_embeddings"))
    excluded = None
    if total_chunks is not None and eligible is not None:
        excluded = max(total_chunks - eligible, 0)
    return {
        "eligible_chunks": eligible,
        "vector": {"populated": populated, "pct": _pct(populated, eligible)},
        "retrieval": {"current": current, "pct": _pct(current, eligible)},
        "missing": _as_int(embedding_health.get("missing_embeddings")),
        "stale": _as_int(embedding_health.get("stale_embeddings")),
        "incompatible": _as_int(embedding_health.get("incompatible_embeddings")),
        "excluded": excluded,
        "vector_search_status": embedding_health.get("vector_search_status"),
        "pgvector_extension": embedding_health.get("pgvector_extension"),
        "pgvector_index_exists": embedding_health.get("pgvector_index_exists"),
        "issues": embedding_health.get("issues") or [],
    }


@router.get("/overview", dependencies=[REPOSITORY_READ])
async def web_overview(
    repository_id: int | None = None,
    jobs_limit: int = Query(10, ge=1, le=100),
    include_health: bool = True,
) -> dict[str, Any]:
    from brain.embeddings.config import get_embedding_config

    # A project overview does not need to probe every service. The dashboard
    # already polls health separately; repeating it for every repository stalls
    # usable counts behind unrelated slow Redis/Neo4j probes. Existing callers
    # retain the full response by default; skipped measurements are explicit null.
    services = await check_health() if include_health else None

    try:
        repository = await _resolve_repository(repository_id)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — registry down: report services/jobs anyway
        repository = None

    counts: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    freshness: dict[str, Any] | None = None
    last_run: dict[str, Any] | None = None
    if repository is not None:
        embedding_health = await get_embedding_retrieval_health(
            repository_id=repository.id, repository_path=repository.path
        )
        async with async_session_factory() as session:
            counts = dict(await get_db_counts(session, repository_id=repository.id, repository_path=repository.path))
            run = (
                await session.execute(
                    select(IndexingRun)
                    .where(IndexingRun.repository_id == repository.id)
                    .order_by(IndexingRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        coverage = build_coverage(embedding_health, _as_int(counts.get("chunks")))
        freshness = await _repository_freshness(repository)
        last_run = shape_index_run(run) if run is not None else None
        index_moment = _parse_dt(freshness.get("last_indexed_at")) or _parse_dt(getattr(run, "started_at", None))
        freshness["index_age_seconds"] = (
            max(int((datetime.now(timezone.utc) - index_moment).total_seconds()), 0) if index_moment else None
        )

    config = get_embedding_config()
    job_data = await get_recent_brain_jobs(
        limit=jobs_limit,
        repository_id=repository.id if repository else None,
        repository_path=repository.path if repository else None,
    )
    return {
        "repository": _repository_dict(repository),
        "services": services,
        "counts": counts,
        "coverage": coverage,
        "freshness": freshness,
        "last_index_run": last_run,
        "embedding": {"provider": config.provider, "model": config.model, "dimension": config.dimension},
        "jobs": [shape_job(job) for job in job_data.get("jobs") or []],
        "jobs_error": job_data.get("error"),
    }


@router.get("/health", dependencies=[TEAM_ADMIN])
async def web_health() -> dict[str, Any]:
    from brain.database.session import redis_client
    from brain.workers.scheduler import scheduler_status

    services = await check_health()
    try:
        scheduler: dict[str, Any] = await scheduler_status(redis_client)
    except Exception as exc:  # noqa: BLE001 — Redis down is reported in services
        logger.warning("Scheduler status unavailable: {}: {}", type(exc).__name__, exc)
        scheduler = {
            "error": "Scheduler status unavailable",
            "error_type": type(exc).__name__,
            "jobs": [],
            "stale": [],
        }
    job_data = await get_recent_brain_jobs(limit=10)
    diagnostics = await get_self_diagnosis_status()
    return {
        "services": services,
        "orchestration": {
            "scheduler": scheduler,
            "recent_jobs": [shape_job(job) for job in job_data.get("jobs") or []],
            "redis_error": job_data.get("error"),
            "diagnostics": diagnostics,
        },
    }
