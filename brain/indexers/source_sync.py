"""Move an indexed git checkout to the revision a reindex job asks for.

The post-merge webhook names the commit that just landed on origin. Until now
the worker only compared that commit with the checkout HEAD and failed when
they differed, which is precisely the moment a new commit exists. This module
fetches origin and detaches the checkout at the requested revision so the
index can follow it.

Production mounts the indexed repository read-only. Nothing here writes to a
checkout that cannot be written, or to one with uncommitted changes: both are
reported as :class:`SourceSyncError` with a message that says what to do.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 120.0

_NO_REMOTE_MARKERS = (
    "does not appear to be a git repository",
    "no such remote",
    "not a valid remote name",
)


class SourceSyncError(RuntimeError):
    """The checkout cannot be moved to the requested revision; a retry will not help."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SourceSyncTransientError(RuntimeError):
    """The fetch failed for a reason that may clear on retry (network, timeout)."""


@dataclass(frozen=True)
class SourceSyncResult:
    requested: str
    previous_head: str | None
    head: str
    fetched: bool
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"status": "synced" if self.changed else "already_at_revision", **asdict(self)}


def _git(repo_path: Path, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    safe_dir = str(repo_path.resolve())
    env = os.environ.copy()
    # A fetch must never block on a credential prompt inside a worker.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", f"safe.directory={safe_dir}", *args],
        cwd=str(repo_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def _resolve_commit(repo_path: Path, revision: str, *, timeout: float) -> str | None:
    result = _git(repo_path, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"], timeout=timeout)
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _is_work_tree(repo_path: Path, *, timeout: float) -> bool:
    result = _git(repo_path, ["rev-parse", "--is-inside-work-tree"], timeout=timeout)
    return result.returncode == 0 and (result.stdout or "").strip() == "true"


def _git_dirs(repo_path: Path, *, timeout: float) -> list[Path]:
    """The directories a fetch and a checkout write to: the common git dir, the work tree's own."""
    dirs: list[Path] = []
    for flag in ("--git-common-dir", "--git-dir"):
        result = _git(repo_path, ["rev-parse", flag], timeout=timeout)
        if result.returncode != 0:
            continue
        raw = (result.stdout or "").strip()
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo_path / candidate
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _can_write(directory: Path) -> bool:
    """Create and remove a probe file: ``os.access`` says yes for root on a read-only mount."""
    probe = directory / f".brain-write-probe-{uuid.uuid4().hex}"
    try:
        with open(probe, "xb"):
            pass
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def checkout_is_writable(repo_path: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    targets = _git_dirs(repo_path, timeout=timeout) or [repo_path / ".git"]
    targets.append(repo_path)
    return all(_can_write(target) for target in targets)


def _uncommitted_changes(repo_path: Path, *, timeout: float) -> int:
    result = _git(repo_path, ["status", "--porcelain", "--untracked-files=no"], timeout=timeout)
    if result.returncode != 0:
        return 0
    return len([line for line in (result.stdout or "").splitlines() if line.strip()])


def _fetch(repo_path: Path, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return _git(repo_path, ["fetch", "--quiet", "--no-tags", "--prune", *args], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SourceSyncTransientError(
            f"git fetch {' '.join(args)} did not finish within {int(timeout)}s"
        ) from exc


def sync_checkout_to_revision(
    repo_path: Path,
    revision: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> SourceSyncResult:
    """Make ``repo_path`` HEAD equal to ``revision``, fetching origin when needed.

    Returns without touching the checkout when it already sits at ``revision``,
    so a read-only mount that is up to date still indexes. Raises
    :class:`SourceSyncError` for a non-git source, a read-only or dirty checkout,
    a missing ``origin`` remote and a revision origin does not have;
    :class:`SourceSyncTransientError` when the fetch itself fails or times out.
    """
    repo_path = Path(repo_path)
    requested = str(revision or "").strip()
    if not requested:
        raise SourceSyncError("No source revision was requested", reason="no_revision")
    if not _is_work_tree(repo_path, timeout=timeout):
        raise SourceSyncError(
            f"{repo_path} is not a git work tree; a snapshot source cannot be fetched. "
            "Replace the snapshot with one built from the requested commit.",
            reason="not_git",
        )

    previous_head = _resolve_commit(repo_path, "HEAD", timeout=timeout)
    target = _resolve_commit(repo_path, requested, timeout=timeout)
    if target and previous_head == target:
        return SourceSyncResult(requested, previous_head, target, fetched=False, changed=False)

    if not checkout_is_writable(repo_path, timeout=timeout):
        raise SourceSyncError(
            f"{repo_path} is read-only, so the checkout cannot be moved to {requested[:16]}. "
            "Update the mounted checkout on the host (git fetch && git checkout --detach "
            f"{requested[:16]}) or mount it writable.",
            reason="read_only",
        )
    dirty = _uncommitted_changes(repo_path, timeout=timeout)
    if dirty:
        raise SourceSyncError(
            f"{repo_path} has {dirty} uncommitted change(s); refusing to move HEAD to "
            f"{requested[:16]}. Commit or discard them first.",
            reason="dirty",
        )

    fetched = False
    if target is None:
        fetched = True
        result = _fetch(repo_path, ["origin"], timeout=timeout)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if any(marker in stderr.lower() for marker in _NO_REMOTE_MARKERS):
                raise SourceSyncError(
                    f"{repo_path} has no usable 'origin' remote: {stderr[:200]}",
                    reason="no_origin",
                )
            raise SourceSyncTransientError(f"git fetch origin failed: {stderr[:300] or 'unknown error'}")
        target = _resolve_commit(repo_path, requested, timeout=timeout)
        if target is None:
            # Commits not on any branch (a force-pushed ref, a tag-only commit)
            # can still be fetched by name when the server allows it.
            _fetch(repo_path, ["origin", requested], timeout=timeout)
            target = _resolve_commit(repo_path, requested, timeout=timeout)
        if target is None:
            raise SourceSyncError(
                f"Revision {requested[:16]} is not reachable from origin after fetch. "
                "Check that the webhook payload names a commit that exists on the remote.",
                reason="unreachable",
            )

    checkout = _git(repo_path, ["checkout", "--quiet", "--detach", target], timeout=timeout)
    if checkout.returncode != 0:
        raise SourceSyncError(
            f"git checkout --detach {target[:16]} failed: {(checkout.stderr or '').strip()[:300]}",
            reason="checkout_failed",
        )
    head = _resolve_commit(repo_path, "HEAD", timeout=timeout)
    if head != target:
        raise SourceSyncError(
            f"Checkout HEAD is {str(head)[:16]} after moving to {target[:16]}",
            reason="checkout_failed",
        )
    return SourceSyncResult(requested, previous_head, head, fetched=fetched, changed=True)
