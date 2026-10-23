"""Background job that connects a repository from a git URL.

``POST /git/connect`` registers the repository and queues this job. The job
clones (or re-fetches) the tracked branch into the Brain-owned managed
directory, makes sure the ``repositories`` row exists, records the origin
so the poller follows it from now on, and queues the first reindex.

A token the user typed in the UI is never in the job params: the API writes
it to a one-time hand-off file under the managed root (see
:mod:`brain.indexers.git_connect`) and this job reads and deletes it. The
clone keeps the credential in its own ``.git/config`` (``remote.origin.url``)
so the origin poller can fetch private repositories; everything the job
stores or returns is redacted.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from brain.config.settings import settings
from brain.database.models import Repository
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import async_session_factory, init_db
from brain.indexers.git_connect import (
    GitUrlError,
    clone_repository,
    managed_root,
    parse_remote_url,
    read_handoff,
    with_token,
)
from brain.indexers.source_mirror import redact_url
from brain.indexers.source_sync import SourceSyncError, SourceSyncTransientError
from brain.memory.origin_state_store import upsert_origin_state
from brain.workers.errors import PermanentJobError


def _under_managed_root(dest: Path) -> bool:
    root = managed_root()
    if root is None:
        return False
    try:
        dest.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return dest.resolve() != root.resolve()


async def _ensure_repository_row(dest: Path, display_name: str) -> Repository:
    existing = await get_repository_by_path(dest)
    if existing is not None:
        return existing
    async with async_session_factory() as session, session.begin():
        row = Repository(name=display_name or dest.name, path=str(dest), type="git", indexing_status="pending")
        session.add(row)
        await session.flush()
        await session.refresh(row)
        session.expunge(row)
        return row


async def _enqueue_reindex(repo_id: int, dest: Path, head: str) -> str:
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
            "repo_path": str(dest),
            "source_revision": head,
            "verify_after": True,
            "benchmark_after": False,
        },
        idempotency_key=f"git-connect:{repo_id}:{head}",
        max_attempts=3,
        initiator=JobInitiator.inherited("git_connect"),
    )


def _remove_if_empty(dest: Path) -> None:
    try:
        if dest.is_dir() and not any(dest.iterdir()):
            dest.rmdir()
    except OSError:
        pass


async def run_git_connect(params: Dict[str, Any]) -> Dict[str, Any]:
    """Clone ``params["url"]`` (``branch``) into ``params["dest"]`` and queue its first reindex."""
    url = str(params.get("url") or "")
    branch = str(params.get("branch") or "")
    dest_raw = str(params.get("dest") or params.get("repo_path") or "")
    display_name = str(params.get("display_name") or "")
    index_now = bool(params.get("index_now", True))
    if not url or not branch or not dest_raw:
        raise PermanentJobError("git_connect needs url, branch and dest")
    dest = Path(dest_raw)
    if not _under_managed_root(dest):
        raise PermanentJobError(f"{dest} is not inside the managed clones directory (BRAIN_MANAGED_REPOS_DIR)")

    try:
        info = parse_remote_url(url)
    except GitUrlError as exc:
        raise PermanentJobError(f"Invalid git URL: {exc}") from exc

    # One-time read: the file is deleted whether or not the clone succeeds, so a
    # retry of this job runs without the token (a token is only needed once; the
    # clone keeps its own copy for later fetches).
    token: Optional[str] = read_handoff(params.get("handoff"))
    try:
        clone_url = with_token(info, token)
    except GitUrlError as exc:
        raise PermanentJobError(str(exc)) from exc
    del token

    await init_db()
    started = datetime.now(timezone.utc)
    try:
        clone = await asyncio.to_thread(clone_repository, clone_url, branch, dest)
    except SourceSyncTransientError:
        raise  # the queue retries within max_attempts
    except SourceSyncError as exc:
        _remove_if_empty(dest)
        message = redact_url(str(exc))
        repo = await get_repository_by_path(dest)
        if repo is not None:
            await upsert_origin_state(
                repo.id, remote_url=info.display_url, tracked_branch=branch, source_mode="checkout",
                poll_status="error", poll_error=message[:500], polled_at=started,
            )
        raise PermanentJobError(message) from exc
    finally:
        del clone_url

    repo = await _ensure_repository_row(clone.path, display_name or info.name)
    now = datetime.now(timezone.utc)
    await upsert_origin_state(
        repo.id,
        remote_url=info.display_url,
        tracked_branch=branch,
        source_mode="checkout",
        remote_head=clone.head,
        remote_head_seen_at=now,
        polled_at=now,
        poll_status="ok",
        poll_error=None,
    )

    reindex_job_id: Optional[str] = None
    if index_now:
        reindex_job_id = await _enqueue_reindex(repo.id, clone.path, clone.head)
        await upsert_origin_state(
            repo.id, last_reindex_job_id=reindex_job_id, last_reindex_head=clone.head, last_reindex_requested_at=now
        )

    logger.info("Connected {} ({}) as repository {} at {}", info.display_url, branch, repo.id, str(clone.path))
    return {
        "repository_id": repo.id,
        "path": str(clone.path),
        "branch": clone.branch,
        "head": clone.head,
        "fresh": clone.fresh,
        "provider": info.provider,
        "display_url": info.display_url,
        "reindex_job_id": reindex_job_id,
        "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
    }
