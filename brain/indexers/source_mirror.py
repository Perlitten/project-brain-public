"""Brain-owned bare mirrors of attached repositories.

Production mounts indexed checkouts read-only, and the primary deployment
indexes a tar-deployed ``/app`` with no ``.git`` at all. Neither can follow
origin by moving its own HEAD. This module keeps a bare clone per repository
under ``BRAIN_SOURCE_MIRROR_DIR`` (a writable data volume), fetches origin
into it and exports any commit from it, so the index follows origin while the
mounted source stays untouched.

Credentials stay server-held: the origin URL (which may embed a token, from
``BRAIN_ORIGIN_REMOTES`` or the checkout's ``remote.origin.url``) is passed on
the git command line only. It is never written to the mirror's config, and
:func:`redact_url` removes userinfo from every value that is stored, logged
or returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from brain.config.settings import settings
from brain.indexers.source_sync import SourceSyncError, SourceSyncTransientError

_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")
# Scheme-less remotes keep their credentials in front of the host too:
# "oauth2:TOKEN@gitlab.com/org/repo" (user:secret) or "TOKEN@github.com/org/repo"
# (a bare token, recognised by the "/" after the host). scp-style
# "git@github.com:org/repo" has neither shape and stays readable.
_BARE_USERINFO = re.compile(
    r"(?<![^\s'\"(<=,;])(?:[^\s/@:'\"]+:[^\s/@'\"]*|[^\s/@:'\"]+(?=@[^\s/@:'\"]+/))@"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MISSING_REF_MARKERS = ("couldn't find remote ref", "could not find remote ref")


def redact_url(text: str | None) -> str:
    """Drop ``user:password@`` from every URL in ``text``, with or without a scheme."""
    return _BARE_USERINFO.sub("***@", _USERINFO.sub(r"\g<scheme>***@", str(text or "")))


@dataclass(frozen=True)
class OriginSpec:
    """Where a repository's origin lives. ``url`` may carry credentials: never store it."""

    url: str
    branch: str
    configured: bool

    @property
    def display_url(self) -> str:
        return redact_url(self.url)

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"

    @property
    def tracking_ref(self) -> str:
        return f"refs/remotes/origin/{self.branch}"


@dataclass(frozen=True)
class MirrorFetch:
    mirror: Path
    head: str | None
    commit: str
    fetched: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "mirror": self.mirror.name,
            "mirror_head": self.head,
            "commit": self.commit,
            "fetched": self.fetched,
        }


def _env() -> dict[str, str]:
    env = os.environ.copy()
    # A worker must never block on a credential prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd: Path | None, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    prefix = ["git", "-c", "protocol.ext.allow=never"]
    if cwd is not None:
        prefix += ["-c", f"safe.directory={Path(cwd).resolve()}"]
    return subprocess.run(
        [*prefix, *args],
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
        env=_env(),
    )


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


# What git check-ref-format forbids anywhere in a ref name.
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{")


def valid_branch_ref(ref: str) -> bool:
    """``git check-ref-format`` rules for a ``refs/heads/<branch>`` ref."""
    if not ref.startswith("refs/heads/") or _REF_FORBIDDEN.search(ref) or ref.endswith("."):
        return False
    # No empty component (``//``, a trailing ``/``), none starting with ``.`` or ending with ``.lock``.
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in ref.split("/"))


def _default_branch() -> str:
    ref = str(settings.PROJECT_BRAIN_WEBHOOK_REF or "").strip()
    if valid_branch_ref(ref):
        return ref[len("refs/heads/"):]
    return "main"


def configured_remotes() -> dict[str, dict[str, str]]:
    """``BRAIN_ORIGIN_REMOTES`` parsed and keyed by normalised path; malformed entries are skipped."""
    raw = str(settings.BRAIN_ORIGIN_REMOTES or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("BRAIN_ORIGIN_REMOTES is not valid JSON; ignoring it")
        return {}
    if not isinstance(data, dict):
        logger.warning("BRAIN_ORIGIN_REMOTES must be a JSON object; ignoring it")
        return {}
    remotes: dict[str, dict[str, str]] = {}
    for path, value in data.items():
        if isinstance(value, str):
            entry = {"url": value}
        elif isinstance(value, dict) and isinstance(value.get("url"), str):
            entry = {"url": value["url"]}
            if isinstance(value.get("branch"), str) and value["branch"].strip():
                entry["branch"] = value["branch"].strip()
        else:
            continue
        if entry["url"].strip():
            remotes[_path_key(path)] = entry
    return remotes


def _valid_url(url: str) -> bool:
    # A leading dash would be parsed as an option by git.
    return bool(url) and not url.startswith("-") and "\n" not in url


def _checkout_origin_branch(repo_path: Path, *, timeout: float) -> str | None:
    result = _git(repo_path, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], timeout=timeout)
    value = (result.stdout or "").strip()
    if result.returncode == 0 and value.startswith("origin/"):
        return value[len("origin/"):]
    return None


def resolve_origin_spec(repo_path: str | Path, *, timeout: float | None = None) -> OriginSpec | None:
    """The origin to follow for ``repo_path``: ``BRAIN_ORIGIN_REMOTES`` first, then ``remote.origin.url``.

    Returns None when neither names one (a snapshot with no configured remote,
    or a checkout whose root is not ``repo_path``).
    """
    timeout = float(timeout or settings.BRAIN_ORIGIN_POLL_TIMEOUT_S)
    path = Path(repo_path)
    entry = configured_remotes().get(_path_key(path))
    if entry is not None:
        url = entry["url"].strip()
        if not _valid_url(url):
            return None
        return OriginSpec(url=url, branch=entry.get("branch") or _default_branch(), configured=True)
    if not path.is_dir():
        return None
    try:
        top = _git(path, ["rev-parse", "--show-toplevel"], timeout=timeout)
        if top.returncode != 0 or _path_key((top.stdout or "").strip()) != _path_key(path):
            return None
        result = _git(path, ["config", "--get", "remote.origin.url"], timeout=timeout)
        url = (result.stdout or "").strip()
        if result.returncode != 0 or not _valid_url(url):
            return None
        branch = _checkout_origin_branch(path, timeout=timeout) or _default_branch()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return OriginSpec(url=url, branch=branch, configured=False)


def remote_head(spec: OriginSpec, *, timeout: float | None = None) -> str | None:
    """``git ls-remote`` the tracked branch. None when the branch does not exist on origin.

    Raises :class:`SourceSyncTransientError` when origin cannot be reached.
    """
    timeout = float(timeout or settings.BRAIN_ORIGIN_POLL_TIMEOUT_S)
    try:
        result = _git(None, ["ls-remote", "--", spec.url, spec.ref], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SourceSyncTransientError(
            f"git ls-remote {spec.display_url} did not finish within {int(timeout)}s"
        ) from exc
    if result.returncode != 0:
        stderr = redact_url((result.stderr or "").strip())[:300]
        raise SourceSyncTransientError(f"git ls-remote {spec.display_url} failed: {stderr or 'unknown error'}")
    for line in (result.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == spec.ref and _SHA.match(sha.strip()):
            return sha.strip()
    return None


def mirror_root() -> Path | None:
    raw = str(settings.BRAIN_SOURCE_MIRROR_DIR or "").strip()
    return Path(raw) if raw else None


def mirror_path(repo_path: str | Path) -> Path | None:
    """``<BRAIN_SOURCE_MIRROR_DIR>/<name>-<hash>.git`` for ``repo_path``; None when mirrors are disabled."""
    root = mirror_root()
    if root is None:
        return None
    path = Path(repo_path)
    digest = hashlib.sha256(Path(repo_path).resolve().as_posix().encode()).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name or "repo").strip("-.") or "repo"
    return root / f"{name}-{digest}.git"


def resolve_commit(git_dir: Path, revision: str, *, timeout: float | None = None) -> str | None:
    timeout = float(timeout or settings.BRAIN_ORIGIN_POLL_TIMEOUT_S)
    if not git_dir.exists() or not revision:
        return None
    try:
        result = _git(git_dir, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def mirror_head(repo_path: str | Path, spec: OriginSpec, *, timeout: float | None = None) -> str | None:
    """The commit the mirror last fetched for the tracked branch, without touching the network."""
    mirror = mirror_path(repo_path)
    if mirror is None:
        return None
    return resolve_commit(mirror, spec.tracking_ref, timeout=timeout)


def count_behind(git_dir: Path, base: str | None, head: str | None, *, timeout: float | None = None) -> int | None:
    """Commits in ``head`` that ``base`` lacks, from local objects only; None when either is unknown."""
    if not base or not head:
        return None
    if base == head:
        return 0
    timeout = float(timeout or settings.BRAIN_ORIGIN_POLL_TIMEOUT_S)
    try:
        result = _git(git_dir, ["rev-list", "--count", f"{base}..{head}"], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return int((result.stdout or "").strip()) if result.returncode == 0 else None
    except ValueError:
        return None


def _ensure_mirror(mirror: Path, *, timeout: float) -> None:
    if (mirror / "HEAD").is_file():
        return
    try:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        result = _git(None, ["init", "--bare", "--quiet", str(mirror)], timeout=timeout)
    except OSError as exc:
        raise SourceSyncError(
            f"Cannot create the source mirror under {mirror.parent}: {exc}. "
            "BRAIN_SOURCE_MIRROR_DIR must be a writable directory.",
            reason="mirror_unwritable",
        ) from exc
    if result.returncode != 0:
        raise SourceSyncError(
            f"git init --bare {mirror.name} failed: {(result.stderr or '').strip()[:300]}. "
            "BRAIN_SOURCE_MIRROR_DIR must be a writable directory.",
            reason="mirror_unwritable",
        )


def _mirror_fetch(mirror: Path, spec: OriginSpec, refspecs: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return _git(mirror, ["fetch", "--quiet", "--no-tags", "--", spec.url, *refspecs], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SourceSyncTransientError(
            f"git fetch {spec.display_url} did not finish within {int(timeout)}s"
        ) from exc


def fetch_into_mirror(
    repo_path: str | Path,
    spec: OriginSpec,
    revision: str | None = None,
    *,
    timeout: float | None = None,
) -> MirrorFetch:
    """Fetch origin's tracked branch into the repository's mirror; resolve ``revision`` (default: its head).

    Skips the network when ``revision`` is already in the mirror. Raises
    :class:`SourceSyncError` when mirrors are disabled, the mirror directory is
    not writable, or origin does not have ``revision``;
    :class:`SourceSyncTransientError` when the fetch itself fails.
    """
    timeout = float(timeout or settings.BRAIN_WEBHOOK_FETCH_TIMEOUT_S)
    mirror = mirror_path(repo_path)
    if mirror is None:
        raise SourceSyncError(
            "Source mirrors are disabled; set BRAIN_SOURCE_MIRROR_DIR to a writable directory "
            "so a read-only or origin-less source can follow origin.",
            reason="mirror_disabled",
        )
    _ensure_mirror(mirror, timeout=timeout)
    requested = str(revision or "").strip()
    if requested:
        existing = resolve_commit(mirror, requested, timeout=timeout)
        if existing:
            return MirrorFetch(mirror, resolve_commit(mirror, spec.tracking_ref, timeout=timeout), existing, False)

    result = _mirror_fetch(mirror, spec, [f"+{spec.ref}:{spec.tracking_ref}"], timeout=timeout)
    if result.returncode != 0:
        stderr = redact_url((result.stderr or "").strip())[:300]
        if any(marker in stderr.lower() for marker in _MISSING_REF_MARKERS):
            raise SourceSyncError(
                f"origin {spec.display_url} has no branch {spec.branch!r}. Set the branch in BRAIN_ORIGIN_REMOTES.",
                reason="no_branch",
            )
        raise SourceSyncTransientError(f"git fetch {spec.display_url} failed: {stderr or 'unknown error'}")
    head = resolve_commit(mirror, spec.tracking_ref, timeout=timeout)
    if not requested:
        if not head:
            raise SourceSyncError(
                f"origin {spec.display_url} branch {spec.branch!r} did not resolve to a commit after fetch",
                reason="unreachable",
            )
        return MirrorFetch(mirror, head, head, True)

    commit = resolve_commit(mirror, requested, timeout=timeout)
    if commit is None and _SHA.match(requested):
        # A commit off the tracked branch (force-pushed away, another branch)
        # can still be fetched by name when the server allows it.
        _mirror_fetch(mirror, spec, [requested], timeout=timeout)
        commit = resolve_commit(mirror, requested, timeout=timeout)
    if commit is None:
        raise SourceSyncError(
            f"Revision {requested[:16]} is not reachable from {spec.display_url} after fetch. "
            "Check that it names a commit that exists on the remote.",
            reason="unreachable",
        )
    return MirrorFetch(mirror, head, commit, True)


def export_commit(mirror: Path, commit: str, target: Path, *, timeout: float = 600) -> None:
    """Write ``commit``'s tree into ``target`` (an empty directory) without a work tree."""
    archive = target / ".brain-mirror.tar"
    try:
        result = _git(mirror, ["archive", "--format=tar", "-o", str(archive), commit], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SourceSyncTransientError(f"git archive {commit[:16]} did not finish within {int(timeout)}s") from exc
    if result.returncode != 0:
        raise SourceSyncError(
            f"git archive {commit[:16]} failed in mirror {mirror.name}: {(result.stderr or '').strip()[:300]}",
            reason="export_failed",
        )
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")
    archive.unlink()
