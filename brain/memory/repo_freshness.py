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
from brain.indexers.source_mirror import redact_url
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


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _ref_exists(repository_path: Path, ref: str) -> bool:
    result = _run_git_command(repository_path, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _read_origin_ref(repository_path: Path, branch: str | None) -> str | None:
    """The remote-tracking ref freshness is measured against, e.g. ``origin/main``.

    Prefers the remote's default branch (``refs/remotes/origin/HEAD``), then the
    checked-out branch's counterpart, then the usual default names. None when
    the checkout has no ``origin`` or none of those refs exist locally.
    """
    try:
        result = _run_git_command(repository_path, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
        if result.returncode == 0:
            target = (result.stdout or "").strip()
            if target.startswith("refs/remotes/"):
                candidate = target[len("refs/remotes/"):]
                if _ref_exists(repository_path, candidate):
                    return candidate
        candidates = []
        if branch and branch != "HEAD":
            candidates.append(f"origin/{branch}")
        candidates.extend(["origin/main", "origin/master"])
        for candidate in candidates:
            if _ref_exists(repository_path, candidate):
                return candidate
        return None
    except Exception:
        return None


def _read_origin_head(repository_path: Path, origin_ref: str) -> str | None:
    """The commit ``origin_ref`` points at.

    Reads the local remote-tracking ref, which the webhook reindex refreshes with
    its fetch. With ``BRAIN_FRESHNESS_LS_REMOTE=true`` it asks the remote itself
    first (bounded by the same git timeout) and falls back to the local ref.
    """
    try:
        if _truthy(os.environ.get("BRAIN_FRESHNESS_LS_REMOTE")):
            remote, _, branch = origin_ref.partition("/")
            if remote and branch:
                result = _run_git_command(
                    repository_path, ["ls-remote", "--quiet", "--heads", remote, f"refs/heads/{branch}"]
                )
                if result.returncode == 0:
                    for line in (result.stdout or "").splitlines():
                        sha, _, ref = line.strip().partition("\t")
                        if ref == f"refs/heads/{branch}" and sha:
                            return sha
        result = _run_git_command(repository_path, ["rev-parse", "--verify", "--quiet", f"{origin_ref}^{{commit}}"])
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None
    except Exception:
        return None


def _origin_ahead_note(freshness: dict[str, Any]) -> str | None:
    behind = freshness.get("commits_behind_origin")
    if not isinstance(behind, int) or behind <= 0:
        return None
    return (
        f"{freshness.get('origin_ref')} is {behind} commit(s) ahead of the indexed commit; "
        "the checkout has not been moved to it yet."
    )


async def describe_origin(repository_path: Path, indexed_commit: str | None) -> dict[str, Any]:
    """Where origin stands relative to ``indexed_commit``: ref, head and the commit count behind.

    All values are None for a non-git source or a checkout without an origin.
    """
    origin: dict[str, Any] = {"origin_ref": None, "origin_head": None, "commits_behind_origin": None}
    if not await asyncio.to_thread(_is_git_worktree, repository_path):
        return origin
    branch = await asyncio.to_thread(_read_current_branch, repository_path)
    origin_ref = await asyncio.to_thread(_read_origin_ref, repository_path, branch)
    if not origin_ref:
        return origin
    origin["origin_ref"] = origin_ref
    origin_head = await asyncio.to_thread(_read_origin_head, repository_path, origin_ref)
    origin["origin_head"] = origin_head
    if origin_head and indexed_commit:
        origin["commits_behind_origin"] = await asyncio.to_thread(
            _read_commits_behind, repository_path, str(indexed_commit), origin_head
        )
    return origin


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


async def _load_origin_state(repository_id: int) -> dict[str, Any] | None:
    """The origin poll's last observation for the repository (None when never polled or unreadable)."""
    from brain.memory.origin_state_store import get_origin_state

    return await get_origin_state(repository_id)


# What the poll reports about its own last run stays meaningful after origin
# follow is switched off; everything else in the row describes a follow that
# no longer happens.
_POLL_RUN_FIELDS = ("poll_status", "polled_at", "poll_error")
_MIRROR_FIELDS = ("source_mode", "mirror_head")


def _origin_follow(repository_path: Path) -> tuple[bool, bool]:
    """(an origin is configured for the source, mirrors are enabled) under the current settings."""
    from brain.indexers.source_mirror import mirror_root, resolve_origin_spec

    try:
        spec = resolve_origin_spec(repository_path, timeout=GIT_TIMEOUT_SECONDS)
    except Exception:
        spec = None
    return spec is not None, mirror_root() is not None


def effective_origin_state(
    state: dict[str, Any] | None, *, origin_configured: bool, mirrors_enabled: bool
) -> dict[str, Any] | None:
    """The part of a stored origin-state row that still applies once origin follow changed.

    With no origin configured any more, only the poll's own run status is kept;
    with mirrors disabled, the repository is no longer served from a mirror.
    Without this a row persisted while following was on kept judging the index
    against an old mirror head after ``BRAIN_ORIGIN_REMOTES`` was cleared.
    """
    if not state:
        return state
    if not origin_configured:
        return {name: state.get(name) for name in _POLL_RUN_FIELDS}
    if not mirrors_enabled and state.get("source_mode") == "mirror":
        return {**state, **{name: None for name in _MIRROR_FIELDS}}
    return state


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    return str(value)


def _overlay_behind(state: dict[str, Any], indexed: str | None, remote: str,
                    local_head: Any, local_behind: Any) -> int | None:
    if indexed and revision_matches(remote, indexed):
        return 0
    stored = state.get("commits_behind")
    if (
        isinstance(stored, int)
        and state.get("commits_behind_head") == remote
        and indexed
        and revision_matches(str(state.get("commits_behind_base") or ""), indexed)
    ):
        return stored
    if local_head and revision_matches(remote, str(local_head)) and isinstance(local_behind, int):
        return local_behind
    return None


def apply_origin_state(freshness: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    """Fold the origin poll's observation (``repository_origin_state``) into a freshness dict.

    The poll asks origin itself (``git ls-remote``), so its head replaces the
    checkout's remote-tracking ref, which a read-only mount never refreshes.
    A repository served from a Brain-owned mirror is judged against the
    mirror's head rather than the stale mounted checkout.
    """
    if not state:
        return freshness
    out = dict(freshness)
    indexed = str(out.get("indexed_commit") or "").strip() or None
    out["origin_poll_status"] = state.get("poll_status")
    out["origin_polled_at"] = _iso_or_none(state.get("polled_at"))
    # Rows written before redaction covered scheme-less remotes may still hold a token.
    out["origin_poll_error"] = redact_url(state["poll_error"]) if state.get("poll_error") else None
    out["origin_remote"] = redact_url(state["remote_url"]) if state.get("remote_url") else None
    mode = state.get("source_mode")
    if mode:
        out["index_source"] = mode

    remote = str(state.get("remote_head") or "").strip()
    if remote:
        if state.get("tracked_branch"):
            out["origin_ref"] = f"origin/{state['tracked_branch']}"
        out["commits_behind_origin"] = _overlay_behind(
            state, indexed, remote, out.get("origin_head"), out.get("commits_behind_origin")
        )
        out["origin_head"] = remote

    status = out.get("status")
    mirror_head = str(state.get("mirror_head") or "").strip()
    if mode == "mirror":
        out["checkout_head_commit"] = out.get("source_head_commit")
        out["source_revision_kind"] = "mirror"
        if mirror_head:
            out["source_head_commit"] = mirror_head
            if indexed and status not in {"source_missing", "unknown", "unindexed"}:
                out["commits_behind"] = None
                if revision_matches(mirror_head, indexed):
                    stale_after_seconds = _days_to_seconds(os.environ.get("BRAIN_INDEX_STALE_AFTER_DAYS"))
                    age_seconds = out.get("age_seconds")
                    if age_seconds is not None and age_seconds > stale_after_seconds:
                        out["status"] = "stale"
                        out["note"] = (
                            "Indexed commit matches the source mirror head, but index age is older than "
                            f"{stale_after_seconds} seconds."
                        )
                    else:
                        out["status"] = "current"
                        out["note"] = None
                else:
                    out["status"] = "behind"
                    out["note"] = "Source mirror head differs from indexed commit."

    job_id = state.get("last_reindex_job_id")
    requested = str(state.get("last_reindex_head") or "").strip()
    if job_id and requested and not (indexed and revision_matches(requested, indexed)):
        out["reindex_job_id"] = job_id

    if out.get("status") == "current":
        out["note"] = _origin_ahead_note(out)
        if out["note"] is not None and mode == "mirror":
            out["note"] = out["note"].replace(
                "the checkout has not been moved to it yet.", "the source mirror has not been indexed at it yet."
            )
        elif out["note"] is None and remote and indexed and not revision_matches(remote, indexed):
            out["note"] = (
                f"{out.get('origin_ref') or 'origin'} is at {remote[:12]}, which differs from the indexed commit; "
                "the commit count is unknown until it is fetched."
            )
    return out


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
    repository_id = getattr(repo, "id", None)
    if isinstance(repository_id, int) and not isinstance(repository_id, bool):
        state = await _load_origin_state(repository_id)
        repo_path = _normalize_path(getattr(repo, "path", None))
        if state and repo_path is not None:
            origin_configured, mirrors_enabled = await asyncio.to_thread(_origin_follow, repo_path)
            state = effective_origin_state(
                state, origin_configured=origin_configured, mirrors_enabled=mirrors_enabled
            )
        freshness = apply_origin_state(freshness, state)
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
        # Measured against origin, not the checkout HEAD: a server that never
        # pulls looked "current" while the project moved on. ``origin_head`` is
        # the local remote-tracking ref (refreshed by the webhook fetch), unless
        # BRAIN_FRESHNESS_LS_REMOTE asks the remote directly.
        "origin_ref": None,
        "origin_head": None,
        "commits_behind_origin": None,
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
        if is_git:
            freshness.update(await describe_origin(repo_path, indexed_commit))

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
            freshness["note"] = _origin_ahead_note(freshness)
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
