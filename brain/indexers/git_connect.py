"""Connect a repository by URL: validate the remote, probe it, clone it into the managed directory.

The web UI's "Connect repository" takes a git URL (GitHub, GitLab, Bitbucket, Azure
DevOps, any self-hosted server over https or ssh), probes it with ``git ls-remote``
to list branches, and clones the chosen branch into ``BRAIN_MANAGED_REPOS_DIR`` so
the clone can be registered, indexed and polled like any other attached
repository (``brain/indexers/source_mirror.py`` reads its ``origin``).

Authenticated clone URLs stay in private hand-off files and the clone's own git
configuration for subsequent fetches. Queue metadata uses :func:`transport_url`;
displayed values use :func:`redact_url`. ``GIT_TERMINAL_PROMPT=0`` and
``protocol.ext.allow=never`` are inherited from :func:`brain.indexers.source_mirror._git`.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from loguru import logger

from brain.config.settings import settings
from brain.indexers.source_mirror import _git, redact_url, valid_branch_ref
from brain.indexers.source_sync import SourceSyncError, SourceSyncTransientError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SCHEME = re.compile(r"^(?P<scheme>https?|ssh)://", re.IGNORECASE)
# scp-like: git@github.com:owner/repo.git (no scheme, a colon after the host, no port).
_SCP = re.compile(r"^(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9.-]+):(?!//)(?P<path>[^:]+)$")
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
_SLUG_BAD = re.compile(r"[^A-Za-z0-9._-]+")
HANDOFF_TTL_S = 3600
MAX_URL_LEN = 1000

# Markers git prints when the remote wants credentials or denies them.
_AUTH_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied",
    "repository not found",
    "http 401",
    "http 403",
    "403 forbidden",
    "401 unauthorized",
    "not authorized",
    "access denied",
    "invalid username or password",
    "invalid username or token",
)

_PROVIDER_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
    "dev.azure.com": "azure",
    "ssh.dev.azure.com": "azure",
    "vs-ssh.visualstudio.com": "azure",
}
# The username GitHub, GitLab and Bitbucket expect next to a personal/app token over https.
_TOKEN_USERS = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth", "azure": "pat"}


class GitUrlError(ValueError):
    """The URL is not one Brain will hand to git."""


@dataclass(frozen=True)
class RemoteInfo:
    """What a remote URL says about itself, before any network call."""

    url: str          # as given, credentials included if any: never store
    display_url: str  # redacted: safe to store and show
    kind: str         # https | ssh
    host: str
    provider: str     # github | gitlab | bitbucket | azure | other
    name: str         # repository name derived from the path, e.g. "project-brain-public"
    path: str         # the path part, e.g. "Perlitten/project-brain-public"

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out.pop("url")
        return out


def _provider_for(host: str) -> str:
    host = host.lower()
    if host in _PROVIDER_HOSTS:
        return _PROVIDER_HOSTS[host]
    for marker, provider in (("github", "github"), ("gitlab", "gitlab"), ("bitbucket", "bitbucket"),
                             ("visualstudio.com", "azure")):
        if marker in host:
            return provider
    return "other"


def _name_from_path(path: str) -> str:
    cleaned = path.strip("/").removesuffix(".git").rstrip("/")
    # Azure DevOps: org/project/_git/repo
    if "/_git/" in cleaned:
        cleaned = cleaned.rsplit("/_git/", 1)[1]
    name = cleaned.rsplit("/", 1)[-1] if cleaned else ""
    return name or "repository"


def parse_remote_url(url: str) -> RemoteInfo:
    """Accept ``https://``, ``http://``, ``ssh://`` and scp-style ``user@host:path`` URLs only.

    Raises :class:`GitUrlError` for anything git could misread as an option or a
    local path (a leading ``-``, control characters, ``file://``, ``ext::``,
    bare paths), and for URLs without a host or a repository path.
    """
    raw = str(url or "").strip()
    if not raw:
        raise GitUrlError("Repository URL is required")
    if len(raw) > MAX_URL_LEN:
        raise GitUrlError("Repository URL is too long")
    if raw.startswith("-"):
        raise GitUrlError("Repository URL must not start with '-'")
    if _CONTROL.search(raw):
        raise GitUrlError("Repository URL must not contain spaces or control characters")
    if "::" in raw:
        raise GitUrlError("Remote helper syntax is not allowed")
    scheme_match = _SCHEME.match(raw)
    if scheme_match:
        try:
            parts = urlsplit(raw)
            parts.port  # Validate malformed/out-of-range ports before calling git.
        except ValueError:
            raise GitUrlError("Repository URL has an invalid host or port") from None
        kind = "ssh" if parts.scheme.lower() == "ssh" else "https"
        if kind == "ssh" and parts.password is not None:
            raise GitUrlError("SSH URLs use a server key; put HTTPS credentials in the token field")
        host = (parts.hostname or "").lower()
        if not host:
            raise GitUrlError("Repository URL has no host")
        path = parts.path or ""
        if path.strip("/") in ("", ".", ".."):
            raise GitUrlError("Repository URL has no repository path")
        display = redact_url(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
        return RemoteInfo(url=raw, display_url=display, kind=kind, host=host, provider=_provider_for(host),
                          name=_name_from_path(path), path=path.strip("/").removesuffix(".git"))
    if "://" in raw:
        raise GitUrlError("Only https://, ssh:// and git@host:path URLs are supported")
    scp = _SCP.match(raw)
    if scp:
        host = scp.group("host").lower()
        path = scp.group("path")
        if path.strip("/") in ("", ".", "..") or "." not in host and host != "localhost":
            raise GitUrlError("Only https://, ssh:// and git@host:path URLs are supported")
        return RemoteInfo(url=raw, display_url=redact_url(raw), kind="ssh", host=host, provider=_provider_for(host),
                          name=_name_from_path(path), path=path.strip("/").removesuffix(".git"))
    raise GitUrlError("Only https://, ssh:// and git@host:path URLs are supported")


def transport_url(info: RemoteInfo) -> str:
    """Credential-free queue URL, preserving the SSH login required by git.

    Display redaction replaces userinfo with *** and is never a transport URL.
    Authentication-bearing URLs travel through the private hand-off instead.
    """
    if not _SCHEME.match(info.url):
        return info.url  # scp-style SSH login is routing information, not a token.
    parts = urlsplit(info.url)
    netloc = parts.netloc if info.kind == "ssh" else parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def with_token(info: RemoteInfo, token: Optional[str]) -> str:
    """The URL git should use: ``token`` embedded as userinfo for an https remote.

    A token containing ``:`` is taken as ``user:secret``; otherwise the
    provider's conventional token username is used. Tokens cannot be applied to
    ssh remotes (ssh authenticates with keys), so the URL is returned unchanged.
    The result must only ever reach a git command line.
    """
    token = (token or "").strip()
    if not token or info.kind != "https":
        return info.url
    if _CONTROL.search(token):
        raise GitUrlError("Access token must not contain spaces or control characters")
    parts = urlsplit(info.url)
    if "@" in parts.netloc:
        raise GitUrlError("The URL already carries credentials; give the token in one place only")
    if ":" in token:
        user, _, secret = token.partition(":")
    else:
        user, secret = _TOKEN_USERS.get(info.provider, "oauth2"), token
    userinfo = f"{quote(user, safe='')}:{quote(secret, safe='')}"
    netloc = f"{userinfo}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass
class ProbeResult:
    reachable: bool
    needs_auth: bool = False
    default_branch: Optional[str] = None
    head: Optional[str] = None
    branches: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _looks_like_auth(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _AUTH_MARKERS)


def _looks_like_missing_branch(stderr: str) -> bool:
    low = stderr.lower()
    return "remote branch" in low and "not found" in low or "couldn't find remote ref" in low


def probe_remote(url: str, *, timeout: Optional[float] = None, max_branches: int = 200) -> ProbeResult:
    """``git ls-remote --symref`` the remote: its default branch, its branches and their heads.

    ``url`` may carry credentials (see :func:`with_token`); nothing from it is
    stored and the error text is redacted.
    """
    timeout = float(timeout or settings.BRAIN_ORIGIN_POLL_TIMEOUT_S or 30)
    try:
        result = _git(None, ["ls-remote", "--symref", "--", url, "HEAD", "refs/heads/*"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return ProbeResult(reachable=False, error=f"git ls-remote did not finish within {int(timeout)}s")
    except OSError as exc:
        return ProbeResult(reachable=False, error=f"git could not be started: {exc}")
    if result.returncode != 0:
        stderr = redact_url((result.stderr or "").strip())[:400]
        return ProbeResult(reachable=False, needs_auth=_looks_like_auth(stderr), error=stderr or "unknown error")
    default_branch: Optional[str] = None
    head_sha: Optional[str] = None
    branches: Dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if line.startswith("ref: "):
            # ref: refs/heads/main<TAB>HEAD
            target, _, ref = line[5:].partition("\t")
            if ref.strip() == "HEAD" and target.startswith("refs/heads/"):
                default_branch = target[len("refs/heads/"):]
            continue
        sha, _, ref = line.partition("\t")
        sha, ref = sha.strip(), ref.strip()
        if not _SHA.match(sha):
            continue
        if ref == "HEAD":
            head_sha = sha
        elif ref.startswith("refs/heads/"):
            branches[ref[len("refs/heads/"):]] = sha
    if default_branch is None and head_sha is not None:
        # Old servers without symref support: the branch whose head is HEAD.
        for candidate in ("main", "master", *sorted(branches)):
            if branches.get(candidate) == head_sha:
                default_branch = candidate
                break
    if default_branch is None and branches:
        default_branch = "main" if "main" in branches else "master" if "master" in branches else sorted(branches)[0]
    head = branches.get(default_branch) if default_branch else head_sha
    ordered = sorted(branches, key=lambda b: (b != default_branch, b))[:max_branches]
    return ProbeResult(reachable=True, default_branch=default_branch, head=head,
                       branches=[{"name": b, "head": branches[b]} for b in ordered])


# ---------------------------------------------------------------------------
# Managed clones

def managed_root() -> Optional[Path]:
    raw = str(settings.BRAIN_MANAGED_REPOS_DIR or "").strip()
    return Path(raw) if raw else None


def _slug(name: str) -> str:
    slug = _SLUG_BAD.sub("-", name).strip("-.").lower()
    return slug[:60] or "repository"


def clone_path(info: RemoteInfo, branch: str) -> Optional[Path]:
    """``<managed root>/<name>-<hash>``: stable for the same host, repository path and branch.

    Credentials, the scheme, the ``.git`` suffix, trailing slashes and letter case
    of the host never change the path, so the same repository lands in one place.
    """
    root = managed_root()
    if root is None:
        return None
    repo_path = info.path.strip("/").removesuffix(".git")
    parts = urlsplit(info.url) if _SCHEME.match(info.url) else None
    port = parts.port if parts else None
    endpoint = info.host.lower()
    if port and parts and port != {"http": 80, "https": 443, "ssh": 22}[parts.scheme.lower()]:
        endpoint += f":{port}"
    key = f"{endpoint}/{repo_path}\n{branch}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return root / f"{_slug(info.name)}-{digest}"


def ensure_branch(branch: str) -> str:
    branch = str(branch or "").strip()
    if not branch or not valid_branch_ref(f"refs/heads/{branch}") or branch.startswith("-"):
        raise GitUrlError(f"{branch!r} is not a valid branch name")
    return branch


@dataclass(frozen=True)
class CloneResult:
    path: Path
    branch: str
    head: str
    fresh: bool  # False when the directory already held this clone and was only fetched


def clone_repository(url: str, branch: str, dest: Path, *, timeout: Optional[float] = None) -> CloneResult:
    """Clone ``branch`` of ``url`` into ``dest`` (a full single-branch clone); fetch when it is already there.

    Raises :class:`SourceSyncError` on a permanent failure (bad URL, refused
    credentials, unknown branch) and :class:`SourceSyncTransientError` when the
    remote cannot be reached in time. Both messages are redacted.
    """
    branch = ensure_branch(branch)
    timeout = float(timeout or settings.BRAIN_WEBHOOK_FETCH_TIMEOUT_S or 600)
    dest = Path(dest)
    display = redact_url(url)
    try:
        if (dest / ".git").exists():
            fresh = False
            result = _git(dest, ["fetch", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                          timeout=timeout)
            if result.returncode == 0:
                result = _git(dest, ["checkout", "--force", "-B", branch, f"origin/{branch}"], timeout=timeout)
        else:
            fresh = True
            dest.parent.mkdir(parents=True, exist_ok=True)
            result = _git(None, ["clone", "--branch", branch, "--single-branch", "--", url, str(dest)],
                          timeout=timeout)
    except subprocess.TimeoutExpired:
        # TimeoutExpired includes argv: its traceback would disclose URL credentials.
        raise SourceSyncTransientError(f"git clone {display} did not finish within {int(timeout)}s") from None
    if result.returncode != 0:
        stderr = redact_url((result.stderr or "").strip())[:500]
        low = stderr.lower()
        if "could not resolve host" in low or "connection timed out" in low or "could not connect" in low:
            raise SourceSyncTransientError(f"git clone {display} failed: {stderr}")
        if _looks_like_auth(stderr):
            reason = "auth_refused"
        elif _looks_like_missing_branch(stderr):
            reason = "no_branch"
        else:
            reason = "clone_failed"
        raise SourceSyncError(f"git clone {display} failed: {stderr or 'unknown error'}", reason=reason)
    head = _git(dest, ["rev-parse", "HEAD"], timeout=30)
    sha = (head.stdout or "").strip()
    if head.returncode != 0 or not _SHA.match(sha):
        raise SourceSyncError(f"clone of {display} has no HEAD: {redact_url((head.stderr or '').strip())[:300]}",
                              reason="clone_failed")
    logger.info("Cloned {} ({}) into {} at {}", display, branch, dest, sha[:12])
    return CloneResult(path=dest, branch=branch, head=sha, fresh=fresh)


# ---------------------------------------------------------------------------
# Token hand-off between the API and the worker
#
# A job's params are stored in Redis and returned by GET /jobs/{id}, so a token
# never goes there. The API writes it to a private file under the managed root
# that only the worker reads and deletes after the clone's final outcome. A
# transient retry retains it until success or expiry. The managed root is a volume
# both containers mount.

_HANDOFF_NAME = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _handoff_dir(root: Path) -> Path:
    path = root / ".handoff"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_handoff(token: str) -> str:
    """Store ``token`` for the worker; returns the hand-off name to put in the job params."""
    root = managed_root()
    if root is None:
        raise GitUrlError("BRAIN_MANAGED_REPOS_DIR is not set")
    directory = _handoff_dir(root)
    purge_handoffs(directory)
    name = secrets.token_urlsafe(24)
    path = directory / name
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return name


def read_handoff(name: Optional[str], *, consume: bool = True) -> Optional[str]:
    """Read private clone credentials; retain them for a transient clone retry when requested."""
    if not name or not _HANDOFF_NAME.fullmatch(name):
        return None
    root = managed_root()
    if root is None:
        return None
    path = root / ".handoff" / name
    expired = False
    try:
        stat = path.stat()
        expired = time.time() - stat.st_mtime > HANDOFF_TTL_S
        if expired:
            return None
        token = path.read_text(encoding="utf-8")
    except OSError:
        return None
    finally:
        if consume or expired:
            try:
                path.unlink()
            except OSError:
                pass
    return token.strip() or None


def purge_handoffs(directory: Optional[Path] = None) -> int:
    """Delete hand-off files older than :data:`HANDOFF_TTL_S` (a job that never ran)."""
    if directory is None:
        root = managed_root()
        if root is None:
            return 0
        directory = root / ".handoff"
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - HANDOFF_TTL_S
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed
