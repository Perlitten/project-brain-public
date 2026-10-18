"""Origin poll: follow each attached repository's origin without relying on webhook delivery.

Every ``BRAIN_ORIGIN_POLL_INTERVAL_S`` the scheduler runs ``origin_poll``. For
each repository it asks origin for the tracked branch head with a cheap
``git ls-remote``, records it in ``repository_origin_state`` (what the
freshness envelope reports as ``origin_head`` / ``commits_behind_origin``),
and, when origin has moved past the indexed commit, enqueues the same
``reindex`` job the post-merge webhook does. A missed or failed webhook
delivery therefore delays the index by at most one interval.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from loguru import logger

from brain.config.paths import resolve_repo_path
from brain.config.settings import settings
from brain.database.repository_utils import list_all_repositories
from brain.database.session import init_db
from brain.indexers.source_mirror import (
    OriginSpec,
    count_behind,
    fetch_into_mirror,
    mirror_path,
    mirror_root,
    redact_url,
    remote_head,
    resolve_commit,
    resolve_origin_spec,
)
from brain.indexers.source_sync import SourceSyncError, SourceSyncTransientError
from brain.memory.origin_state_store import get_origin_state, upsert_origin_state, utc_now
from brain.memory.source_manifest import revision_matches

# A reindex already requested for the same head is not requested again until
# this long has passed (the queue's idempotency claim absorbs live duplicates;
# this only stops a permanently failing head from being retried every poll).
REQUEST_COOLDOWN = timedelta(hours=1)

EnqueueReindex = Callable[[Any, str], Awaitable[str]]

# Row fields that only describe an active origin follow.
_FOLLOW_CLEARED: Dict[str, Any] = {
    name: None
    for name in ("source_mode", "mirror_head", "remote_url", "tracked_branch", "remote_head",
                 "commits_behind", "commits_behind_base", "commits_behind_head")
}


def _primary_repo_path() -> Optional[str]:
    try:
        return resolve_repo_path(settings.PROJECT_BRAIN_GIT_REPO_PATH or None).as_posix()
    except Exception:
        return None


async def enqueue_reindex(repo: Any, head: str) -> str:
    """Queue the webhook's reindex for ``head``.

    The idempotency key is the signed webhook's, so a webhook delivery and a
    poll that notice the same commit share one job.
    """
    from brain.database.session import redis_client
    from brain.workers.initiator import JobInitiator
    from brain.workers.queue import queue_for_job

    queue = queue_for_job(
        redis_client, settings.WORKER_REDIS_PREFIX, "reindex", pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED
    )
    return await queue.enqueue(
        "reindex",
        {
            "clean": False,
            "repo_path": repo.path,
            "source_revision": head,
            "verify_after": True,
            # The smoke benchmark's golden tasks describe the primary repository only.
            "benchmark_after": repo.path == _primary_repo_path(),
        },
        idempotency_key=f"git-reindex:signed:{head}",
        max_attempts=3,
        initiator=JobInitiator.system("origin_poll"),
    )


def _as_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _recently_requested(state: Dict[str, Any], head: str, now: datetime) -> bool:
    if state.get("last_reindex_head") != head or not state.get("last_reindex_job_id"):
        return False
    requested_at = _as_utc(state.get("last_reindex_requested_at"))
    return requested_at is not None and now - requested_at < REQUEST_COOLDOWN


def _count(repo_path: Path, spec: OriginSpec, indexed: Optional[str], head: str) -> Optional[int]:
    """Commits origin is ahead of the index, from whichever local objects have both commits."""
    candidates: list[Path] = []
    mirror = mirror_path(repo_path)
    if mirror is not None and mirror.exists():
        candidates.append(mirror)
    if (repo_path / ".git").exists():
        candidates.append(repo_path)
    for git_dir in candidates:
        value = count_behind(git_dir, indexed, head)
        if value is not None:
            return value
    return None


async def poll_repository(repo: Any, *, enqueue: EnqueueReindex = enqueue_reindex) -> Dict[str, Any]:
    """Poll one repository's origin, record the result and request a reindex when origin moved."""
    repo_path = Path(str(repo.path))
    now = utc_now()
    report: Dict[str, Any] = {"repository_id": repo.id, "path": repo.path}
    state = await get_origin_state(repo.id) or {}
    spec = await asyncio.to_thread(resolve_origin_spec, repo_path)
    if spec is None:
        message = (
            "No origin to poll: the source has no 'origin' remote and BRAIN_ORIGIN_REMOTES names none for "
            f"{repo.path}."
        )
        # Following was switched off (BRAIN_ORIGIN_REMOTES no longer names the
        # source): what an earlier follow recorded, the mirror marking above
        # all, must not keep judging the index against a mirror head.
        await upsert_origin_state(repo.id, polled_at=now, poll_status="unconfigured", poll_error=message,
                                  **_FOLLOW_CLEARED)
        return {**report, "status": "unconfigured", "error": message}

    base: Dict[str, Any] = {"remote_url": spec.display_url, "tracked_branch": spec.branch, "polled_at": now}
    report.update(remote=spec.display_url, branch=spec.branch)
    try:
        head = await asyncio.to_thread(remote_head, spec)
    except SourceSyncTransientError as exc:
        message = redact_url(str(exc))[:500]
        await upsert_origin_state(repo.id, **base, poll_status="error", poll_error=message)
        return {**report, "status": "error", "error": message}
    if head is None:
        message = f"origin {spec.display_url} has no branch {spec.branch!r}"
        await upsert_origin_state(repo.id, **base, poll_status="error", poll_error=message)
        return {**report, "status": "error", "error": message}

    values: Dict[str, Any] = {**base, "remote_head": head, "poll_status": "ok", "poll_error": None}
    if head != state.get("remote_head"):
        values["remote_head_seen_at"] = now
    report.update(status="ok", remote_head=head)

    # A repository already served from a mirror keeps it current, so the
    # count and the reindex find the new objects locally.
    if state.get("source_mode") == "mirror" and mirror_root() is not None:
        mirror = mirror_path(repo_path)
        if mirror is None or await asyncio.to_thread(resolve_commit, mirror, head) is None:
            try:
                fetched = await asyncio.to_thread(fetch_into_mirror, repo_path, spec, head)
                values["mirror_head"] = fetched.head
            except (SourceSyncError, SourceSyncTransientError) as exc:
                values["poll_error"] = f"mirror fetch failed: {redact_url(str(exc))[:400]}"
                report["mirror_error"] = values["poll_error"]

    indexed = str(getattr(repo, "last_indexed_commit", None) or "").strip() or None
    behind = 0 if indexed and revision_matches(head, indexed) else await asyncio.to_thread(
        _count, repo_path, spec, indexed, head
    )
    values.update(commits_behind=behind, commits_behind_base=indexed, commits_behind_head=head)
    report["indexed_commit"] = indexed
    report["commits_behind"] = behind

    if indexed and revision_matches(head, indexed):
        report["reindex"] = "not_needed"
    elif not settings.BRAIN_WEBHOOK_FETCH_SOURCE:
        report["reindex"] = "skipped"
        report["reindex_reason"] = "BRAIN_WEBHOOK_FETCH_SOURCE is off, so the source is never moved to origin"
    elif _recently_requested(state, head, now):
        report["reindex"] = "already_requested"
        report["reindex_job_id"] = state.get("last_reindex_job_id")
    else:
        try:
            job_id = await enqueue(repo, head)
        except Exception as exc:  # the next poll retries
            report["reindex"] = "enqueue_failed"
            report["reindex_error"] = f"{type(exc).__name__}: {exc}"[:300]
        else:
            values.update(last_reindex_job_id=job_id, last_reindex_head=head, last_reindex_requested_at=now)
            report["reindex"] = "enqueued"
            report["reindex_job_id"] = job_id
            logger.info("origin poll: {} moved to {}; reindex job {}", repo.path, head[:12], job_id)

    await upsert_origin_state(repo.id, **values)
    return report


async def run_origin_poll(params: Dict[str, Any], *, enqueue: EnqueueReindex = enqueue_reindex) -> Dict[str, Any]:
    """Worker task ``origin_poll``. ``repo_path`` limits it to one repository."""
    await init_db()
    repositories = await list_all_repositories()
    if params.get("repo_path"):
        target = resolve_repo_path(params["repo_path"]).as_posix()
        repositories = [repo for repo in repositories if repo.path == target]
    results = []
    for repo in repositories:
        try:
            results.append(await poll_repository(repo, enqueue=enqueue))
        except Exception as exc:  # one broken repository must not hide the others
            logger.warning("origin poll failed for {}: {}", repo.path, exc)
            results.append({"repository_id": repo.id, "path": repo.path, "status": "error",
                            "error": redact_url(f"{type(exc).__name__}: {exc}")[:300]})
    return {
        "polled": len(results),
        "reindex_enqueued": sum(1 for item in results if item.get("reindex") == "enqueued"),
        "errors": sum(1 for item in results if item.get("status") == "error"),
        "repositories": results,
    }
