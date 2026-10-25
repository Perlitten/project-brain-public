"""Source planning for indexing jobs: mirror/checkout decisions, revision prep, fingerprint repair."""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from loguru import logger
from brain.config.settings import settings
from brain.database.repository_utils import get_repository_by_path
from brain.indexers.file_indexer import _scan_repo_files, source_content_digest
from brain.indexers.repo_indexer import get_git_commit_hash
from brain.indexers.reliability import git_source_is_clean
from brain.indexers.source_mirror import (export_commit, fetch_into_mirror, mirror_path, mirror_root, resolve_commit, resolve_origin_spec)
from brain.indexers.source_sync import SourceSyncError, sync_checkout_to_revision
from brain.memory.origin_state_store import get_origin_state, upsert_origin_state
from brain.memory.source_manifest import revision_matches
from brain.workers.errors import PermanentJobError


# A checkout that cannot be moved for one of these reasons can still follow
# origin through a Brain-owned mirror (BRAIN_SOURCE_MIRROR_DIR).
_MIRROR_FALLBACK_REASONS = frozenset({"read_only", "not_git", "no_origin", "dirty"})


@dataclass
class _SourcePlan:
    """Where a reindex reads its content: the checkout itself, or ``commit`` exported from ``mirror``."""

    sync: Optional[Dict[str, Any]] = None
    mirror: Optional[Path] = None
    commit: Optional[str] = None


async def _mirror_plan(repo_path: Path, expected_revision: str, current: str, exc: SourceSyncError) -> _SourcePlan:
    """Fetch ``expected_revision`` into the repository's mirror after the checkout could not be moved."""
    failure: Dict[str, Any] = {
        "path": repo_path.as_posix(),
        "commit_hash": current,
        "source_revision_verified": False,
    }
    if mirror_root() is None:
        raise PermanentJobError(
            f"Cannot move the source checkout to {expected_revision[:16]}: {exc} "
            "Alternatively set BRAIN_SOURCE_MIRROR_DIR to a writable directory so Brain indexes origin "
            "through its own mirror.",
            result={**failure, "source_sync": {"status": "failed", "reason": exc.reason,
                                               "requested": expected_revision, "head": current}},
        ) from exc
    spec = await asyncio.to_thread(resolve_origin_spec, repo_path)
    if spec is None:
        raise PermanentJobError(
            f"Cannot move the source checkout to {expected_revision[:16]}: {exc} "
            f"No origin URL is known for {repo_path.as_posix()}; name it in BRAIN_ORIGIN_REMOTES "
            "so the source mirror can fetch it.",
            result={**failure, "source_sync": {"status": "failed", "reason": exc.reason, "mode": "mirror",
                                               "mirror": "no_origin", "requested": expected_revision,
                                               "head": current}},
        ) from exc
    try:
        fetched = await asyncio.to_thread(
            fetch_into_mirror,
            repo_path,
            spec,
            expected_revision,
            timeout=float(settings.BRAIN_WEBHOOK_FETCH_TIMEOUT_S),
        )
    except SourceSyncError as mirror_exc:
        raise PermanentJobError(
            f"Cannot fetch {expected_revision[:16]} into the source mirror: {mirror_exc}",
            result={**failure, "source_sync": {"status": "failed", "reason": mirror_exc.reason, "mode": "mirror",
                                               "fallback_reason": exc.reason, "remote": spec.display_url,
                                               "requested": expected_revision, "head": current}},
        ) from mirror_exc
    # SourceSyncTransientError (network, timeout) propagates: the queue retries it.
    logger.info(
        "run_reindex: {} cannot be moved ({}); indexing {} from mirror {}",
        repo_path,
        exc.reason,
        fetched.commit[:12],
        fetched.mirror.name,
    )
    return _SourcePlan(
        sync={
            "status": "mirror",
            "mode": "mirror",
            "fallback_reason": exc.reason,
            "requested": expected_revision,
            "previous_head": current or None,
            "head": fetched.commit,
            "changed": True,
            "remote": spec.display_url,
            "branch": spec.branch,
            **fetched.to_dict(),
        },
        mirror=fetched.mirror,
        commit=fetched.commit,
    )


async def _prepare_source_revision(repo_path: Path, expected_revision: str) -> _SourcePlan:
    """Bring the source to the revision a job asks for, or explain why it cannot.

    The git-merge webhook (and the origin poll) name a commit that is on
    origin, so the checkout HEAD differs from it exactly when there is
    something new to index. A checkout that cannot be moved (read-only, no
    origin, not git at all) is served from a Brain-owned mirror instead when
    ``BRAIN_SOURCE_MIRROR_DIR`` is set.
    """
    current = str(await asyncio.to_thread(get_git_commit_hash, repo_path) or "").strip()
    if revision_matches(expected_revision, current):
        return _SourcePlan(sync={"status": "already_at_revision", "requested": expected_revision,
                                 "previous_head": current, "head": current, "fetched": False, "changed": False})
    if not settings.BRAIN_WEBHOOK_FETCH_SOURCE:
        raise PermanentJobError(
            "Indexed source revision mismatch: "
            f"requested {expected_revision[:16]} but the checkout HEAD is {current[:16] or 'missing'}; "
            "BRAIN_WEBHOOK_FETCH_SOURCE is off, so the checkout was not updated",
            result={
                "path": repo_path.as_posix(),
                "commit_hash": current,
                "source_revision_verified": False,
                "source_sync": {"status": "disabled", "requested": expected_revision, "head": current},
            },
        )
    try:
        outcome = await asyncio.to_thread(
            sync_checkout_to_revision,
            repo_path,
            expected_revision,
            timeout=float(settings.BRAIN_WEBHOOK_FETCH_TIMEOUT_S),
        )
    except SourceSyncError as exc:
        if exc.reason in _MIRROR_FALLBACK_REASONS:
            return await _mirror_plan(repo_path, expected_revision, current, exc)
        # Unreachable commit, failed checkout: retrying cannot help.
        raise PermanentJobError(
            f"Cannot move the source checkout to {expected_revision[:16]}: {exc}",
            result={
                "path": repo_path.as_posix(),
                "commit_hash": current,
                "source_revision_verified": False,
                "source_sync": {"status": "failed", "reason": exc.reason,
                                "requested": expected_revision, "head": current},
            },
        ) from exc
    # SourceSyncTransientError (fetch failure, timeout) propagates: the queue retries it.
    logger.info(
        "run_reindex: moved {} from {} to {} (fetched={})",
        repo_path,
        str(outcome.previous_head)[:12],
        outcome.head[:12],
        outcome.fetched,
    )
    return _SourcePlan(sync=outcome.to_dict())


async def _recorded_mirror_plan(repo_path: Path, repository_id: Optional[int] = None) -> Optional[_SourcePlan]:
    """For a run without a revision: keep indexing the mirror a repository is already served from.

    Without this a nightly or self-heal run would index the stale mounted
    checkout and move the index backwards. Uses only local mirror objects;
    returns None (checkout mode) whenever mirrors are disabled.
    """
    if mirror_root() is None:
        return None
    if repository_id is None:
        record = await get_repository_by_path(repo_path)
        repository_id = record.id if record is not None else None
    if not isinstance(repository_id, int):
        return None
    state = await get_origin_state(repository_id)
    if not state or state.get("source_mode") != "mirror":
        return None
    spec = await asyncio.to_thread(resolve_origin_spec, repo_path)
    mirror = mirror_path(repo_path)
    if spec is None or mirror is None:
        return None
    commit = await asyncio.to_thread(resolve_commit, mirror, spec.tracking_ref)
    if not commit:
        return None
    return _SourcePlan(
        sync={"status": "mirror", "mode": "mirror", "requested": None, "head": commit, "fetched": False,
              "changed": False, "mirror": mirror.name, "mirror_head": commit, "commit": commit,
              "remote": spec.display_url, "branch": spec.branch},
        mirror=mirror,
        commit=commit,
    )


async def _index_with_plan(
    indexer: Any,
    repo_path: Path,
    plan: Optional[_SourcePlan],
    *,
    clean: bool,
    expected_revision: str = "",
) -> Any:
    """Index the checkout, or a snapshot of the planned mirror commit exported to a temporary directory."""
    if plan is None or plan.mirror is None or not plan.commit:
        if expected_revision:
            return await indexer.index_repository(repo_path, clean=clean, expected_revision=expected_revision)
        return await indexer.index_repository(repo_path, clean=clean)
    with tempfile.TemporaryDirectory(prefix="brain-mirror-") as tmp:
        snapshot = Path(tmp)
        await asyncio.to_thread(export_commit, plan.mirror, plan.commit, snapshot)
        return await indexer.index_repository(
            repo_path,
            clean=clean,
            expected_revision=expected_revision,
            source_root=snapshot,
            source_commit=plan.commit,
        )


async def _record_source_mode(repository_id: Any, plan: Optional[_SourcePlan]) -> None:
    """Remember whether the repository is served from its checkout or its mirror (mirrors enabled only)."""
    if mirror_root() is None or not isinstance(repository_id, int):
        return
    if plan is not None and plan.mirror is not None:
        await upsert_origin_state(
            repository_id, source_mode="mirror", mirror_head=(plan.sync or {}).get("mirror_head") or plan.commit
        )
    else:
        await upsert_origin_state(repository_id, source_mode="checkout")


async def _repair_source_fingerprint(
    repo_path: Path, mirror: Optional[Path], commit: Optional[str]
) -> tuple[bool, str]:
    """(source is clean, content digest) of what the reindex read: the checkout or the mirror commit."""
    if mirror is None or not commit:
        files = await asyncio.to_thread(_scan_repo_files, repo_path)
        clean = await asyncio.to_thread(git_source_is_clean, repo_path, files)
        return clean, await asyncio.to_thread(source_content_digest, repo_path, files)
    with tempfile.TemporaryDirectory(prefix="brain-mirror-") as tmp:
        snapshot = Path(tmp)
        await asyncio.to_thread(export_commit, mirror, commit, snapshot)
        files = await asyncio.to_thread(_scan_repo_files, snapshot)
        clean = await asyncio.to_thread(git_source_is_clean, snapshot, files)
        return clean, await asyncio.to_thread(source_content_digest, snapshot, files)
