"""Deep-context and nightly-maintenance helpers: repo approval, exact-LFM reconcile, quality probes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict
from brain.config.paths import resolve_repo_path
from brain.config.settings import settings
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import init_db
from brain.late_interaction.client import build_maintenance_late_interaction_client
from brain.late_interaction.remote_sync import sync_remote_late_interaction
from brain.workers.errors import PermanentJobError


_NIGHTLY_QUALITY_PROBES = (
    (
        "where the worker enqueues and executes durable background jobs",
        frozenset(
            {
                "brain/workers/queue.py",
                "brain/workers/worker.py",
                "brain/workers/tasks.py",
            }
        ),
    ),
    (
        "how repository indexing verifies and repairs semantic embeddings",
        frozenset(
            {
                "brain/embeddings/backfill.py",
                "brain/embeddings/integrity.py",
                "brain/workers/tasks.py",
            }
        ),
    ),
    (
        "how the LFM late interaction corpus is synced and reranking applied",
        frozenset(
            {
                "brain/late_interaction/application.py",
                "brain/late_interaction/remote_sync.py",
                "brain/workers/tasks.py",
            }
        ),
    ),
)


def _deep_repo_path(params: Dict[str, Any]) -> Path:
    return resolve_repo_path(
        params.get("repo_path")
        or settings.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH
    )


async def _approved_deep_repository(repo_path: Path):
    record = await get_repository_by_path(repo_path)
    if record is None:
        raise PermanentJobError(
            f"Deep repository is not indexed: {repo_path.as_posix()}"
        )
    if record.id != settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID:
        raise PermanentJobError(
            "Deep repository does not match the approved LFM repository",
            result={
                "repository_id": record.id,
                "repository_path": record.path,
            },
        )
    return record


async def _reconcile_exact_lfm(repository_id: int):
    """Synchronize, prove exact equality, then return an exact scoring client."""
    maintenance_client = build_maintenance_late_interaction_client(
        repository_id=repository_id,
    )
    try:
        sync = await sync_remote_late_interaction(
            repository_id,
            dry_run=False,
            prune=True,
            client=maintenance_client,
        )
    finally:
        await maintenance_client.aclose()

    sync_result = sync.to_dict()
    exact = (
        sync.passed
        and sync.verification_mode == "exact"
        and bool(sync.index_revision)
        and bool(sync.source_identity_digest)
        and sync.source_identity_digest == sync.remote_identity_digest
        and sync.verified_documents > 0
    )
    if not exact:
        raise PermanentJobError(
            "LFM exact corpus reconciliation failed",
            result={"late_interaction": sync_result},
        )

    exact_client = build_maintenance_late_interaction_client(
        repository_id=repository_id,
        index_revision=sync.index_revision,
        identity_digest=sync.remote_identity_digest,
        document_count=sync.verified_documents,
    )
    status = await exact_client.status(repository_id)
    if status.status != "ready":
        await exact_client.aclose()
        raise PermanentJobError(
            f"LFM exact release is not ready: {status.reason or status.status}",
            result={
                "late_interaction": sync_result,
                "release_status": status.__dict__,
            },
        )
    return sync_result, exact_client


async def _run_deep_quality_probes(
    repo_path: str,
    exact_client,
) -> list[dict[str, Any]]:
    from brain.search.code_search import search_code

    probes: list[dict[str, Any]] = []
    limit = min(
        settings.NIGHTLY_DEEP_MAINTENANCE_QUALITY_QUERIES,
        len(_NIGHTLY_QUALITY_PROBES),
    )
    for query, expected_paths in _NIGHTLY_QUALITY_PROBES[:limit]:
        result = await search_code(
            query,
            limit=10,
            repo_path=repo_path,
            retrieval_mode="deep",
            late_interaction_client=exact_client,
        )
        late = result.get("late_interaction") or {}
        top_paths = [
            chunk.get("file_path")
            for chunk in result.get("chunks", [])[:10]
            if chunk.get("file_path")
        ]
        matched_expected = sorted(expected_paths.intersection(top_paths))
        passed = bool(
            late.get("status") == "scored"
            and late.get("applied")
            and float(late.get("coverage") or 0.0) >= 1.0
            and matched_expected
        )
        probes.append(
            {
                "query_hash": hashlib.sha256(
                    query.encode("utf-8")
                ).hexdigest(),
                "status": late.get("status"),
                "applied": bool(late.get("applied")),
                "coverage": late.get("coverage"),
                "latency_ms": late.get("latency_ms"),
                "candidate_count": late.get("candidate_count"),
                "expected_paths": sorted(expected_paths),
                "matched_expected": matched_expected,
                "top_paths": top_paths,
                "passed": passed,
            }
        )
    return probes


async def _standard_deep_context(task_description: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Durable context pack with the default retrieval, for installs without the
    late-interaction model service. The agent still gets a full, persisted pack."""
    from brain.context.context_pack_builder import ContextPackBuilder

    await init_db()
    repo_path = resolve_repo_path(params.get("repo_path") or settings.TARGET_REPO_PATH)
    if await get_repository_by_path(repo_path) is None:
        raise PermanentJobError(
            f"Repository is not indexed yet: {repo_path.as_posix()}. Run `brain index --repo {repo_path.as_posix()}` first."
        )
    context_pack = await ContextPackBuilder().build_context_pack(
        task_description=task_description,
        repo_path=repo_path,
        budget="deep",
        consumer=str(params.get("consumer") or "") or None,
    )
    context_pack.pop("markdown_content", None)
    context_pack = json.loads(json.dumps(context_pack, ensure_ascii=False, default=str))
    return {
        "status": "completed",
        "mode": "standard",
        "note": "Late-interaction reranking is not configured; built with the default retrieval.",
        "context_pack": context_pack,
    }
