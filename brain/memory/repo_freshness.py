"""Repository freshness checks used by API and search prompts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os
import subprocess
import time

from sqlalchemy import func, select

from brain.database.models import File
from brain.database.session import async_session_factory
from brain.memory.source_manifest import read_source_manifest, revision_matches


def _normalize_path(raw_path: Any) -> Path | None:
    if not raw_path:
        return None
    try:
        return Path(str(raw_path))
    except Exception:
        return None


def _days_to_seconds(days_value: str | int | None) -> int:
    try:
        return max(int(days_value or 14), 0) * 24 * 60 * 60
    except Exception:
        return 14 * 24 * 60 * 60


# Freshness is computed inside request paths, so a git call that never returns
# would not just slow one search — subprocess.run blocks the event loop and
# stalls every concurrent request. Bound it, and treat a timeout as "unknown".
GIT_TIMEOUT_SECONDS = float(os.environ.get("BRAIN_FRESHNESS_GIT_TIMEOUT", "8"))


def _run_git_command(repository_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    safe_dir = str(repository_path.resolve())
    base = ["git", "-c", f"safe.directory={safe_dir}"]
    try:
        return subprocess.run(
            [*base, *args],
            cwd=str(repository_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(exc))


def _is_git_worktree(repository_path: Path) -> bool:
    result = _run_git_command(repository_path, ["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and (result.stdout or "").strip() == "true"


def _read_head_commit(repository_path: Path) -> str | None:
    try:
        result = _run_git_command(repository_path, ["rev-parse", "HEAD"])
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None
    except Exception:
        return None


def _read_current_branch(repository_path: Path) -> str | None:
    """The checked-out branch name, or None when git cannot name one.

    A detached HEAD legitimately answers "HEAD"; that is git's own answer and is
    passed through rather than being replaced with a guessed branch name.
    """
    try:
        result = _run_git_command(repository_path, ["rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None
    except Exception:
        return None


def _read_commits_behind(repository_path: Path, indexed_commit: str, head_commit: str) -> int | None:
    if not indexed_commit or not head_commit:
        return None
    try:
        result = _run_git_command(repository_path, ["rev-list", "--count", f"{indexed_commit}..{head_commit}"])
        if result.returncode != 0:
            return None
        count = (result.stdout or "").strip()
        if not count:
            return None
        return int(count)
    except Exception:
        return None


async def _count_indexed_files(repository: Any) -> int | None:
    # Touching a lazy relationship on an instance whose session has closed raises
    # (DetachedInstanceError / MissingGreenlet under async SQLAlchemy). That is
    # exactly how /repositories reports its rows, so this must never be a plain
    # attribute read — it made every repo come back as unindexed or stale.
    try:
        files = getattr(repository, "files", None)
        if isinstance(files, (list, tuple, set)):
            return len(files)
    except Exception:
        pass
    repository_id = getattr(repository, "id", None)
    if repository_id is None:
        return None
    try:
        async with async_session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(File).where(File.repository_id == repository_id)
            )
        return int(total or 0)
    except Exception:
        return None


def _seconds_since(datetime_value: datetime | None) -> int | None:
    if datetime_value is None:
        return None
    now = datetime.now(timezone.utc)
    if datetime_value.tzinfo is None:
        datetime_value = datetime_value.replace(tzinfo=timezone.utc)
    age = now - datetime_value
    return int(age.total_seconds())


def _to_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


_FRESHNESS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL_SECONDS = float(os.environ.get("BRAIN_FRESHNESS_CACHE_TTL", "60"))


def _cache_key(repo: Any) -> str:
    # Includes the row's own change markers so a re-index invalidates the entry
    # immediately rather than waiting out the TTL.
    return "|".join(
        str(getattr(repo, attr, None))
        for attr in ("path", "last_indexed_commit", "updated_at")
    )


async def assess_repository_freshness(repo: Any) -> dict[str, Any]:
    """Return freshness status for a repository without raising exceptions.

    Called on every search, so it is cached briefly and its git calls are both
    bounded and pushed off the event loop.
    """
    key = _cache_key(repo)
    cached = _FRESHNESS_CACHE.get(key)
    now_monotonic = time.monotonic()
    if cached and cached[0] > now_monotonic:
        return dict(cached[1])

    freshness = await _assess_repository_freshness_uncached(repo)
    if CACHE_TTL_SECONDS > 0:
        _FRESHNESS_CACHE[key] = (now_monotonic + CACHE_TTL_SECONDS, dict(freshness))
        if len(_FRESHNESS_CACHE) > 256:  # bounded: a handful of repos in practice
            for stale_key in [k for k, (exp, _) in _FRESHNESS_CACHE.items() if exp <= now_monotonic]:
                _FRESHNESS_CACHE.pop(stale_key, None)
    return freshness


async def _assess_repository_freshness_uncached(repo: Any) -> dict[str, Any]:
    freshness: dict[str, Any] = {
        "source_present": False,
        "source_head_commit": None,
        "source_manifest_digest": None,
        "source_revision_kind": None,
        # Named by the shell's repository switcher, which shows the branch beside
        # the behind-count. Always present so a caller never has to guess whether
        # a missing key means "not a git tree" or "this module is older".
        "branch": None,
        "indexed_commit": None,
        "last_indexed_at": None,
        "age_seconds": None,
        "commits_behind": None,
        "status": "source_missing",
        "note": None,
    }

    try:
        freshness["indexed_commit"] = getattr(repo, "last_indexed_commit", None)
        # The Repository model has no last_indexed_at column; updated_at is the
        # row's own write timestamp and is what indexing actually bumps. Without
        # this fallback age_seconds stays None and the age-based `stale` verdict
        # can never fire — the check would exist but never run.
        freshness["last_indexed_at"] = getattr(repo, "last_indexed_at", None) or getattr(
            repo, "updated_at", None
        )
        freshness["age_seconds"] = _seconds_since(_to_utc_datetime(freshness["last_indexed_at"]))

        repo_path = _normalize_path(getattr(repo, "path", None))
        if repo_path is None or not repo_path.is_dir():
            freshness["status"] = "source_missing"
            freshness["note"] = "Source path is not a directory."
            return freshness

        freshness["source_present"] = True
        is_git = await asyncio.to_thread(_is_git_worktree, repo_path)
        freshness["source_head_commit"] = (
            await asyncio.to_thread(_read_head_commit, repo_path) if is_git else None
        )
        freshness["branch"] = (
            await asyncio.to_thread(_read_current_branch, repo_path) if is_git else None
        )
        manifest = None if is_git else await asyncio.to_thread(read_source_manifest, repo_path)
        if manifest is not None:
            freshness["source_manifest_digest"] = manifest.get("content_digest")
            freshness["source_revision_kind"] = "snapshot_manifest"
            freshness["source_head_commit"] = manifest.get("revision")
        elif is_git:
            freshness["source_revision_kind"] = "git"
        indexed_commit = freshness["indexed_commit"]

        file_count = await _count_indexed_files(repo)
        if not indexed_commit:
            if file_count == 0:
                freshness["status"] = "unindexed"
                freshness["note"] = "No indexed commit and no indexed files were found."
                return freshness

        if not is_git:
            if manifest is None:
                freshness["status"] = "unverifiable"
                freshness["note"] = (
                    "Source is not a git work tree and has no valid "
                    ".brain-source-manifest.json; freshness cannot be proven."
                )
                return freshness
            source_revision = str(manifest["revision"])
            if not revision_matches(source_revision, indexed_commit):
                freshness["status"] = "behind"
                freshness["note"] = "Snapshot manifest revision differs from indexed revision."
                return freshness
            stale_after_seconds = _days_to_seconds(os.environ.get("BRAIN_INDEX_STALE_AFTER_DAYS"))
            age_seconds = freshness["age_seconds"]
            if age_seconds is not None and age_seconds > stale_after_seconds:
                freshness["status"] = "stale"
                freshness["note"] = (
                    "Snapshot revision matches, but index age exceeds "
                    f"{stale_after_seconds} seconds."
                )
            else:
                freshness["status"] = "current"
                freshness["note"] = None
            return freshness

        if (
            freshness["source_head_commit"]
            and indexed_commit
            and not revision_matches(freshness["source_head_commit"], indexed_commit)
        ):
            freshness["status"] = "behind"
            freshness["commits_behind"] = await asyncio.to_thread(
                _read_commits_behind,
                repo_path,
                indexed_commit,
                freshness["source_head_commit"],
            )
            freshness["note"] = (
                "Source head commit differs from indexed commit."
            )
            return freshness

        freshness["status"] = "current"
        if indexed_commit and revision_matches(freshness["source_head_commit"], indexed_commit):
            stale_after_seconds = _days_to_seconds(os.environ.get("BRAIN_INDEX_STALE_AFTER_DAYS"))
            age_seconds = freshness["age_seconds"]
            if age_seconds is not None and age_seconds > stale_after_seconds:
                freshness["status"] = "stale"
                freshness["note"] = (
                    f"Indexed commit matches source head, but index age is older than "
                    f"{stale_after_seconds} seconds."
                )
                return freshness
            freshness["note"] = None
            return freshness

        # If source head cannot be resolved or commit metadata is inconsistent,
        # we keep degraded behavior: the index cannot be trusted as fresh.
        freshness["status"] = "stale"
        if freshness["note"] is None:
            freshness["note"] = "Unable to validate freshness against source head commit."
        return freshness

    except Exception as exc:
        # A failure to assess is not a verdict about the data. Reporting it as
        # `unindexed` or `stale` let a bug in this module masquerade as a fact
        # about the index — every repository came back mislabelled that way.
        freshness["status"] = "unknown"
        freshness["note"] = f"Freshness could not be determined: {exc}"
        return freshness
