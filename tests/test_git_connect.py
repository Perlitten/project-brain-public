"""Connect a repository by URL: URL rules, token handling, probe/clone, the hand-off file, the job and the API.

Local bare repositories only (no network); the origin state table lives in a
throwaway sqlite database, the registry in a temporary ``.brain/workspace``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi import HTTPException
from fastapi.testclient import TestClient

from brain.config.settings import settings
from brain.auth.principals import ResolvedPrincipal
from brain.database.models import RepositoryOriginState
from brain.indexers import git_connect as gc
from brain.indexers.git_connect import (
    HANDOFF_TTL_S,
    GitUrlError,
    ProbeResult,
    RemoteInfo,
    clone_path,
    clone_repository,
    ensure_branch,
    parse_remote_url,
    probe_remote,
    purge_handoffs,
    read_handoff,
    transport_url,
    with_token,
    write_handoff,
)
from brain.indexers.source_sync import SourceSyncError, SourceSyncTransientError
from brain.memory import origin_state_store
from brain.workers import git_connect as worker
from brain.workers.errors import PermanentJobError

_IDENTITY = ["-c", "user.name=Tester", "-c", "user.email=tester@example.com"]
SECRET = "ghp_verySecretToken123"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> SimpleNamespace:
    """A bare origin with ``main`` (two commits) and ``feature`` (one more)."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", bare.as_posix(), dev.as_posix())
    _git(dev, "checkout", "-q", "-b", "main")
    (dev / "a.py").write_text("print('a')\n", encoding="utf-8")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "a")
    (dev / "b.py").write_text("print('b')\n", encoding="utf-8")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "b")
    _git(dev, "push", "-q", "origin", "HEAD:main")
    main = _git(dev, "rev-parse", "HEAD")
    _git(dev, "checkout", "-q", "-b", "feature")
    (dev / "c.py").write_text("print('c')\n", encoding="utf-8")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "c")
    _git(dev, "push", "-q", "origin", "HEAD:feature")
    feature = _git(dev, "rev-parse", "HEAD")
    return SimpleNamespace(bare=bare, dev=dev, main=main, feature=feature)


@pytest.fixture
def managed(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "managed"
    root.mkdir()
    monkeypatch.setattr(settings, "BRAIN_MANAGED_REPOS_DIR", root.as_posix())
    return root


@pytest.fixture(autouse=True)
def _quiet_settings(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_MANAGED_REPOS_DIR", None)
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_POLL_TIMEOUT_S", 30)


@pytest_asyncio.fixture
async def origin_db(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'origin.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: RepositoryOriginState.__table__.create(conn))  # type: ignore[attr-defined]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(origin_state_store, "async_session_factory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


# --- URL rules -----------------------------------------------------------------


def test_remote_url_accepts_https_ssh_and_scp_forms_and_names_the_provider():
    gh = parse_remote_url("  https://github.com/example/sample-repo.git ")
    assert (gh.kind, gh.host, gh.provider, gh.name) == ("https", "github.com", "github", "sample-repo")
    assert gh.path == "example/sample-repo"
    assert gh.display_url == "https://github.com/example/sample-repo.git"
    assert "url" not in gh.to_dict()

    scp = parse_remote_url("git@gitlab.com:group/sub/repo.git")
    assert (scp.kind, scp.provider, scp.name, scp.path) == ("ssh", "gitlab", "repo", "group/sub/repo")
    assert parse_remote_url("ssh://git@bitbucket.org/team/thing").provider == "bitbucket"
    azure = parse_remote_url("https://dev.azure.com/org/project/_git/repo")
    assert (azure.provider, azure.name) == ("azure", "repo")
    assert parse_remote_url("https://gitlab.example.com/x/y.git").provider == "gitlab"
    assert parse_remote_url("https://git.example.internal/x/y.git").provider == "other"
    assert parse_remote_url("git@localhost:repo.git").kind == "ssh"
    assert parse_remote_url("http://127.0.0.1:8080/r.git").kind == "https"


@pytest.mark.parametrize("url", [
    "", "   ", "-oProxyCommand=evil", "--upload-pack=evil https://h/r", "https://h/a b", "https://h/a\tb",
    "file:///etc", "ext::sh -c evil", "git://host/repo", "ftp://host/repo", "/srv/repo", "C:\\repos\\thing",
    "repo.git", "https://", "https://github.com/", "https://github.com/..", "localhost-noslash:path",
    "x" * 1001,
])
def test_remote_url_rejects_what_git_could_misread(url):
    with pytest.raises(GitUrlError):
        parse_remote_url(url)


def test_remote_url_redacts_embedded_credentials_everywhere_but_the_raw_url():
    info = parse_remote_url("https://me:ghp_secret@github.com/me/private.git")
    assert info.url == "https://me:ghp_secret@github.com/me/private.git"
    assert "ghp_secret" not in info.display_url
    assert "ghp_secret" not in json.dumps(info.to_dict())


def test_queue_transport_preserves_ssh_login_and_excludes_https_credentials():
    ssh = parse_remote_url("ssh://git@github.com/owner/repo.git")
    assert transport_url(ssh) == ssh.url and "***" not in transport_url(ssh)
    assert "git@" not in ssh.display_url
    https = parse_remote_url(f"https://me:{SECRET}@github.com/o/r.git?token=hidden#secret")
    assert transport_url(https) == "https://github.com/o/r.git"
    for bad in ("https://host:abc/o/r", "https://host:70000/o/r", "https://[broken/r", "ssh://me:secret@host/r"):
        with pytest.raises(GitUrlError):
            parse_remote_url(bad)


def test_token_is_embedded_with_the_provider_username_and_never_for_ssh():
    gh = parse_remote_url("https://github.com/o/r.git")
    assert with_token(gh, SECRET) == f"https://x-access-token:{SECRET}@github.com/o/r.git"
    assert with_token(parse_remote_url("https://gitlab.com/o/r.git"), SECRET).startswith("https://oauth2:")
    assert with_token(parse_remote_url("https://bitbucket.org/o/r.git"), SECRET).startswith("https://x-token-auth:")
    assert with_token(parse_remote_url("https://dev.azure.com/o/p/_git/r"), SECRET).startswith("https://pat:")
    assert with_token(parse_remote_url("https://git.corp/o/r.git"), "alice:s3cret/x") == (
        "https://alice:s3cret%2Fx@git.corp/o/r.git")
    assert with_token(gh, None) == gh.url and with_token(gh, "   ") == gh.url
    ssh = parse_remote_url("git@github.com:o/r.git")
    assert with_token(ssh, SECRET) == ssh.url
    with pytest.raises(GitUrlError):
        with_token(parse_remote_url("https://me:x@github.com/o/r.git"), SECRET)
    with pytest.raises(GitUrlError):
        with_token(gh, "bad token")


def test_branch_names_follow_git_check_ref_format():
    assert ensure_branch(" main ") == "main"
    assert ensure_branch("release/2026.10") == "release/2026.10"
    for bad in ("", "-x", "a..b", "a b", "a/", ".hidden", "x.lock", "a//b", "HEAD~1"):
        with pytest.raises(GitUrlError):
            ensure_branch(bad)


# --- probe and clone (local bare repository) --------------------------------------


def test_probe_lists_branches_with_the_default_first(origin):
    result = probe_remote(origin.bare.as_posix())
    assert result.reachable and not result.needs_auth and result.error is None
    assert result.default_branch == "main" and result.head == origin.main
    assert result.branches == [{"name": "main", "head": origin.main}, {"name": "feature", "head": origin.feature}]
    assert result.to_dict()["branches"][0]["name"] == "main"


def test_probe_of_a_missing_remote_is_an_error_not_an_exception(tmp_path):
    result = probe_remote((tmp_path / "nowhere.git").as_posix())
    assert not result.reachable and result.error


def test_probe_recognises_an_auth_refusal():
    assert gc._looks_like_auth("fatal: Authentication failed for 'https://github.com/o/r.git/'")
    assert gc._looks_like_auth("remote: HTTP Basic: Access denied")
    assert not gc._looks_like_auth("fatal: could not resolve host: github.com")


def test_clone_then_refetch_tracks_the_chosen_branch(origin, managed):
    dest = managed / "repo-abc"
    first = clone_repository(origin.bare.as_posix(), "feature", dest)
    assert first.fresh and first.head == origin.feature and first.branch == "feature"
    assert (dest / "c.py").exists()
    assert _git(dest, "rev-parse", "--abbrev-ref", "HEAD") == "feature"

    # Origin moves on; a second call fetches instead of cloning again.
    (origin.dev / "d.py").write_text("print('d')\n", encoding="utf-8")
    _git(origin.dev, "add", ".")
    _git(origin.dev, "commit", "-q", "-m", "d")
    _git(origin.dev, "push", "-q", "origin", "HEAD:feature")
    moved = _git(origin.dev, "rev-parse", "HEAD")
    second = clone_repository(origin.bare.as_posix(), "feature", dest)
    assert not second.fresh and second.head == moved and (dest / "d.py").exists()


def test_clone_of_an_unknown_branch_is_a_permanent_redacted_error(origin, managed):
    with pytest.raises(SourceSyncError) as excinfo:
        clone_repository(origin.bare.as_posix(), "nope", managed / "x")
    assert "nope" in str(excinfo.value)
    assert excinfo.value.reason == "no_branch"
    # A closed port is transient (the worker retries); the message still hides the token.
    with pytest.raises((SourceSyncError, SourceSyncTransientError)) as redacted:
        clone_repository(f"https://x-access-token:{SECRET}@127.0.0.1:9/o/r.git", "main", managed / "y")
    assert SECRET not in str(redacted.value)


def test_clone_path_is_stable_per_url_and_branch_and_lives_under_the_root(managed):
    a = parse_remote_url("https://github.com/example/sample-repo.git")
    b = parse_remote_url("https://GitHub.com/example/sample-repo")
    with_creds = parse_remote_url("https://me:tok@github.com/example/sample-repo.git")
    ssh = parse_remote_url("git@github.com:example/sample-repo.git")
    assert clone_path(a, "main") == clone_path(b, "main") == clone_path(with_creds, "main") == clone_path(ssh, "main")
    assert clone_path(a, "main") != clone_path(a, "dev")
    assert clone_path(parse_remote_url("https://git.example/Org/Repo"), "main") != clone_path(
        parse_remote_url("https://git.example/Org/repo"), "main")
    assert clone_path(parse_remote_url("https://git.example:8443/Org/repo"), "main") != clone_path(
        parse_remote_url("https://git.example:9443/Org/repo"), "main")
    path = clone_path(a, "main")
    assert path is not None and path.parent == managed and path.name.startswith("sample-repo-")
    weird = RemoteInfo(url="u", display_url="u", kind="https", host="h", provider="other", name="../../etc/passwd",
                       path="p")
    assert clone_path(weird, "main").parent == managed  # type: ignore[union-attr]
    settings.BRAIN_MANAGED_REPOS_DIR = None
    assert clone_path(a, "main") is None


# --- token hand-off ----------------------------------------------------------------


def test_handoff_is_read_once_and_expires(managed):
    with pytest.raises(GitUrlError):
        settings.BRAIN_MANAGED_REPOS_DIR = None
        write_handoff(SECRET)
    settings.BRAIN_MANAGED_REPOS_DIR = managed.as_posix()

    name = write_handoff(SECRET)
    path = managed / ".handoff" / name
    assert path.exists() and path.read_text(encoding="utf-8") == SECRET
    assert read_handoff(name, consume=False) == SECRET
    assert read_handoff(name, consume=False) == SECRET
    assert read_handoff(name) == SECRET
    assert not path.exists()
    assert read_handoff(name) is None

    assert read_handoff(None) is None and read_handoff("../../etc/passwd") is None and read_handoff("short") is None

    stale = write_handoff("old-token")
    old = time.time() - HANDOFF_TTL_S - 60
    os.utime(managed / ".handoff" / stale, (old, old))
    assert read_handoff(stale, consume=False) is None
    assert not (managed / ".handoff" / stale).exists()

    stale2 = write_handoff("old-token-2")
    os.utime(managed / ".handoff" / stale2, (old, old))
    fresh = write_handoff("fresh")
    assert not (managed / ".handoff" / stale2).exists(), "write_handoff purges expired files"
    assert purge_handoffs() == 0 and (managed / ".handoff" / fresh).exists()


# --- the worker job ------------------------------------------------------------------


def _info_for(origin_path: Path) -> RemoteInfo:
    # The job's URL rules forbid local paths, so the test substitutes a parsed remote that points at the bare repo.
    return RemoteInfo(url=origin_path.as_posix(), display_url="https://github.com/acme/thing.git", kind="ssh",
                      host="github.com", provider="github", name="thing", path="acme/thing")


@pytest.mark.asyncio
async def test_git_connect_job_clones_records_origin_and_queues_the_reindex(origin, managed, origin_db):
    dest = managed / "thing-0123456789ab"
    handoff = write_handoff(origin.bare.as_posix())
    repo = SimpleNamespace(id=7)
    enqueue = AsyncMock(return_value="job-reindex-1")
    with (
        patch.object(worker, "parse_remote_url", return_value=_info_for(origin.bare)),
        patch.object(worker, "init_db", AsyncMock()),
        patch.object(worker, "_ensure_repository_row", AsyncMock(return_value=repo)) as ensure_row,
        patch.object(worker, "_enqueue_reindex", enqueue),
    ):
        result = await worker.run_git_connect({
            "url": "https://github.com/acme/thing.git", "branch": "main", "dest": str(dest),
            "display_name": "Thing", "index_now": True, "handoff": handoff,
        })
    assert result["repository_id"] == 7 and result["head"] == origin.main and result["fresh"] is True
    assert result["path"] == str(dest) and result["reindex_job_id"] == "job-reindex-1"
    assert SECRET not in json.dumps(result)
    assert (dest / "b.py").exists() and not (dest / "c.py").exists()
    assert not (managed / ".handoff" / handoff).exists(), "the token file is consumed"
    ensure_row.assert_awaited_once_with(dest, "Thing")
    enqueue.assert_awaited_once_with(7, dest, origin.main)

    state = await origin_state_store.get_origin_state(7)
    assert state is not None
    assert state["remote_url"] == "https://github.com/acme/thing.git" and state["tracked_branch"] == "main"
    assert state["remote_head"] == origin.main and state["poll_status"] == "ok"
    assert state["last_reindex_job_id"] == "job-reindex-1" and state["last_reindex_head"] == origin.main


@pytest.mark.asyncio
async def test_private_clone_retry_retains_credentials_until_clone_succeeds(managed, origin_db):
    url = "https://github.com/acme/private.git"
    credential_url = with_token(parse_remote_url(url), SECRET)
    handoff = write_handoff(credential_url)
    dest = managed / "private"
    params = {"url": url, "branch": "main", "dest": str(dest), "index_now": False, "handoff": handoff}
    clone = gc.CloneResult(path=dest, branch="main", head="a" * 40, fresh=True)
    with (
        patch.object(worker, "init_db", AsyncMock()),
        patch.object(worker, "clone_repository", side_effect=[SourceSyncTransientError("network"), clone]) as cloning,
        patch.object(worker, "_ensure_repository_row", AsyncMock(return_value=SimpleNamespace(id=7))),
    ):
        with pytest.raises(SourceSyncTransientError):
            await worker.run_git_connect(params)
        assert read_handoff(handoff, consume=False) == credential_url
        result = await worker.run_git_connect(params)
    assert [call.args[0] for call in cloning.call_args_list] == [credential_url, credential_url]
    assert read_handoff(handoff) is None and SECRET not in json.dumps(result)


def test_clone_timeout_traceback_never_contains_authenticated_argv(managed):
    url = f"https://me:{SECRET}@github.com/o/private.git"
    with patch.object(gc, "_git", side_effect=subprocess.TimeoutExpired(["git", "clone", url], 1)):
        with pytest.raises(SourceSyncTransientError) as error:
            clone_repository(url, "main", managed / "private", timeout=1)
    assert SECRET not in "".join(traceback.format_exception(error.value))


@pytest.mark.asyncio
async def test_git_connect_job_refuses_destinations_outside_the_managed_root(origin, managed, tmp_path):
    with pytest.raises(PermanentJobError):
        await worker.run_git_connect({"url": "https://github.com/a/b.git", "branch": "main",
                                      "dest": str(tmp_path / "elsewhere")})
    with pytest.raises(PermanentJobError):
        await worker.run_git_connect({"url": "https://github.com/a/b.git", "branch": "main", "dest": str(managed)})
    with pytest.raises(PermanentJobError):
        await worker.run_git_connect({"url": "file:///etc", "branch": "main", "dest": str(managed / "x")})
    with pytest.raises(PermanentJobError):
        await worker.run_git_connect({"url": "https://github.com/a/b.git", "branch": "", "dest": str(managed / "x")})


@pytest.mark.asyncio
async def test_git_connect_job_records_a_clone_failure_on_the_origin_state(origin, managed, origin_db):
    dest = managed / "thing-failed"
    repo = SimpleNamespace(id=9)
    with (
        patch.object(worker, "parse_remote_url", return_value=_info_for(origin.bare)),
        patch.object(worker, "init_db", AsyncMock()),
        patch.object(worker, "get_repository_by_path", AsyncMock(return_value=repo)),
        patch.object(worker, "_enqueue_reindex", AsyncMock()) as enqueue,
    ):
        with pytest.raises(PermanentJobError):
            await worker.run_git_connect({"url": "https://github.com/acme/thing.git", "branch": "missing",
                                          "dest": str(dest)})
    assert not dest.exists(), "an empty failed destination is removed"
    enqueue.assert_not_awaited()
    state = await origin_state_store.get_origin_state(9)
    assert state is not None and state["poll_status"] == "error" and "missing" in (state["poll_error"] or "")


def test_git_connect_is_a_registered_job_type():
    from brain.workers.tasks import execute_job  # noqa: F401  (import side effects register handlers)
    from brain.workers import tasks
    from brain.workers.tasks import dispatch

    handlers = getattr(tasks, "JOB_HANDLERS", None) or getattr(tasks, "_HANDLERS", None)
    source = Path(dispatch.__file__).read_text(encoding="utf-8")
    assert handlers is None or "git_connect" in handlers
    assert '"git_connect": run_git_connect' in source


# --- the API ---------------------------------------------------------------------------


client = TestClient(app, raise_server_exceptions=False)


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list = []

    async def enqueue(self, job_type, params=None, **kwargs):
        self.calls.append((job_type, dict(params or {}), kwargs))
        return "job-git-1"


@pytest.fixture
def api(tmp_path: Path, monkeypatch):
    """Solo state, dev-open access, the registry file store in a temporary cwd, the queue faked."""
    monkeypatch.chdir(tmp_path)
    queue = _FakeQueue()
    with (
        patch("apps.api.team_access.current_team_state", AsyncMock(return_value="solo")),
        patch("apps.api.activity_log.record_job_queued", AsyncMock()),
        patch("apps.api.routers.git_connect._queue", return_value=queue),
    ):
        yield SimpleNamespace(queue=queue, tmp=tmp_path)


def test_probe_endpoint_rejects_bad_urls_and_reports_the_remote_without_the_token(api, managed):
    assert client.post("/git/probe", json={"url": "file:///etc"}).status_code == 400
    assert client.post("/git/probe", json={"url": "-oProxyCommand=x"}).status_code == 400
    assert client.post("/git/probe", json={"url": ""}).status_code == 422

    probe = ProbeResult(reachable=True, default_branch="main", head="a" * 40,
                        branches=[{"name": "main", "head": "a" * 40}, {"name": "dev", "head": "b" * 40}])
    with patch("apps.api.routers.git_connect.probe_remote", return_value=probe) as probed:
        response = client.post("/git/probe", json={"url": "https://github.com/acme/thing.git", "token": SECRET})
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert probed.call_args.args[0] == f"https://x-access-token:{SECRET}@github.com/acme/thing.git"
    body = response.json()
    assert body["ok"] is True and body["provider"] == "github" and body["name"] == "thing"
    assert body["display_url"] == "https://github.com/acme/thing.git"
    assert body["default_branch"] == "main" and [b["name"] for b in body["branches"]] == ["main", "dev"]
    assert body["managed_root_configured"] is True and body["managed_root_writable"] is True

    refused = ProbeResult(reachable=False, needs_auth=True, error="Authentication failed")
    settings.BRAIN_MANAGED_REPOS_DIR = None
    with patch("apps.api.routers.git_connect.probe_remote", return_value=refused):
        body = client.post("/git/probe", json={"url": "https://github.com/acme/private.git"}).json()
    assert body["ok"] is False and body["needs_auth"] is True
    assert body["managed_root_configured"] is False and body["managed_root_writable"] is False


def test_connect_endpoint_needs_a_managed_root(api):
    response = client.post("/git/connect", json={"url": "https://github.com/acme/thing.git"})
    assert response.status_code == 409 and "Clones directory" in response.text


def test_connect_endpoint_registers_queues_and_hands_the_token_over_privately(api, managed):
    probe = ProbeResult(reachable=True, default_branch="main", head="c" * 40, branches=[{"name": "main", "head": "c" * 40}])
    with patch("apps.api.routers.git_connect.probe_remote", return_value=probe):
        response = client.post("/git/connect", json={
            "url": "https://github.com/acme/thing.git", "token": SECRET, "display_name": "Thing", "index_now": True,
        })
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    body = response.json()
    assert body["job_id"] == "job-git-1" and body["status"] == "queued" and body["branch"] == "main"
    assert body["provider"] == "github" and body["name"] == "Thing"
    assert body["display_url"] == "https://github.com/acme/thing.git"
    assert Path(body["path"]).parent.resolve() == managed.resolve()
    assert body["repository"]["display_name"] == "Thing" and body["repository"]["default_branch"] == "main"
    assert body["repository"]["current_revision"] == "c" * 40

    (job_type, params, kwargs) = api.queue.calls[-1]
    assert job_type == "git_connect"
    assert SECRET not in json.dumps(params) and SECRET not in json.dumps(kwargs, default=str)
    assert params["url"] == "https://github.com/acme/thing.git" and params["branch"] == "main"
    assert params["dest"] == params["repo_path"] == body["path"] and params["index_now"] is True
    assert kwargs["max_attempts"] == 2 and kwargs["timeout_seconds"] == 1800
    handoff = params["handoff"]
    authenticated_url = f"https://x-access-token:{SECRET}@github.com/acme/thing.git"
    assert (managed / ".handoff" / handoff).read_text(encoding="utf-8") == authenticated_url
    assert read_handoff(handoff) == authenticated_url

    # The registry knows it; connecting the same URL again before the clone exists re-queues the job,
    # once the clone exists it is a conflict that points at Sync.
    listing = client.get("/workspace/repositories")
    assert listing.status_code == 200 and any(r["display_name"] == "Thing" for r in listing.json()["repositories"])
    with patch("apps.api.routers.git_connect.probe_remote", return_value=probe):
        again = client.post("/git/connect", json={"url": "https://github.com/acme/thing.git", "branch": "main"})
    assert again.status_code == 200 and "handoff" not in api.queue.calls[-1][1]
    (Path(body["path"]) / ".git").mkdir(parents=True)
    with patch("apps.api.routers.git_connect.probe_remote", return_value=probe):
        conflict = client.post("/git/connect", json={"url": "https://github.com/acme/thing.git", "branch": "main"})
    assert conflict.status_code == 409 and "already connected" in conflict.text


def test_connect_endpoint_validates_branch_and_surfaces_probe_failures(api, managed):
    bad_branch = client.post("/git/connect", json={"url": "https://github.com/acme/thing.git", "branch": "-x"})
    assert bad_branch.status_code == 400
    refused = ProbeResult(reachable=False, needs_auth=True, error=f"Authentication failed for {SECRET}")
    with patch("apps.api.routers.git_connect.probe_remote", return_value=refused):
        response = client.post("/git/connect", json={"url": "https://github.com/acme/private.git", "token": SECRET})
    assert response.status_code == 401
    assert SECRET not in response.text
    assert api.queue.calls == []
    # The probe answer is the other place an error text leaves the API.
    with patch("apps.api.routers.git_connect.probe_remote", return_value=refused):
        probe = client.post("/git/probe", json={"url": "https://github.com/acme/private.git", "token": SECRET})
    assert probe.status_code == 200
    assert probe.json()["needs_auth"] is True
    assert SECRET not in probe.text
    handoff_dir = managed / ".handoff"
    assert not handoff_dir.exists() or not [p for p in handoff_dir.iterdir() if p.is_file()]
    assert client.post("/git/connect", json={"url": "https://github.com/a/b.git", "branch": "main",
                                             "project_id": "not-a-uuid"}).status_code == 422


@pytest.mark.parametrize("url,queued_url", [
    ("ssh://git@github.com/acme/thing.git", "ssh://git@github.com/acme/thing.git"),
    (f"https://me:{SECRET}@github.com/acme/thing.git?access_token={SECRET}",
     "https://github.com/acme/thing.git"),
])
def test_connect_queue_uses_routable_urls_and_stages_embedded_credentials(api, managed, url, queued_url):
    response = client.post("/git/connect", json={"url": url, "branch": "main", "index_now": False})
    assert response.status_code == 200, response.text
    params = api.queue.calls[-1][1]
    assert params["url"] == queued_url and "***" not in params["url"]
    assert SECRET not in json.dumps(params) and SECRET not in response.text
    if "handoff" in params:
        assert read_handoff(params["handoff"]) == url


def test_clone_permission_denial_precedes_registration_even_without_indexing(api, managed):
    with (
        patch("apps.api.routers.git_connect._authorize_clone", AsyncMock(
            side_effect=HTTPException(status_code=403, detail="run.request required"))),
        patch("apps.api.routers.git_connect.workspace_registry", AsyncMock()) as registry,
    ):
        response = client.post("/git/connect", json={"url": "https://github.com/acme/thing.git",
                                                    "branch": "main", "index_now": False})
    assert response.status_code == 403 and api.queue.calls == []
    registry.assert_not_awaited()


@pytest.mark.asyncio
async def test_clone_checks_selected_project_run_permission_before_it_exists():
    from apps.api.routers.git_connect import _authorize_clone
    from brain.team.authorization import Permission, TeamDenied

    principal = ResolvedPrincipal(1, "human", "Owner", scopes=frozenset({"team:admin"}), credential_id=2)
    project = uuid.uuid4()
    context = AsyncMock()
    with (
        patch("apps.api.team_access.current_team_state", AsyncMock(return_value="team")),
        patch("apps.api.team_access.async_session_factory", return_value=context),
        patch("apps.api.routers.git_connect.authorize", AsyncMock(side_effect=TeamDenied("Policy denied"))) as authorize,
    ):
        with pytest.raises(HTTPException) as denied:
            await _authorize_clone(principal, project, "/managed/new-clone")
    assert denied.value.status_code == 403
    assert authorize.await_args.args[1:] == (principal, project, Permission.REQUEST_RUN)


def test_connect_requires_job_scope_in_addition_to_registration_scope(api, managed):
    principal = ResolvedPrincipal(1, "human", "Registrar",
                                  scopes=frozenset({"workspace:read", "workspace:write"}), credential_id=2)
    with patch("apps.api.auth.resolve_api_key", AsyncMock(return_value=principal)):
        response = client.post("/git/connect", headers={"X-API-Key": "registrar-only"},
                               json={"url": "https://github.com/acme/thing.git", "branch": "main"})
    assert response.status_code == 403 and api.queue.calls == []


def test_connect_endpoint_releases_the_token_when_the_queue_is_down(api, managed):
    class _Down:
        async def enqueue(self, *args, **kwargs):
            raise ConnectionError("redis down")

    probe = ProbeResult(reachable=True, default_branch="main", head="d" * 40, branches=[{"name": "main", "head": "d" * 40}])
    with (
        patch("apps.api.routers.git_connect._queue", return_value=_Down()),
        patch("apps.api.routers.git_connect.probe_remote", return_value=probe),
    ):
        response = client.post("/git/connect", json={"url": "https://github.com/acme/other.git", "token": SECRET,
                                                      "branch": "main"})
    assert response.status_code == 503 and SECRET not in response.text
    assert not [p for p in (managed / ".handoff").iterdir() if p.is_file()]


def test_capabilities_advertise_git_connect():
    from apps.api.routers.core_lifecycle import router as lifecycle_router

    source = Path(__import__("apps.api.routers.core_lifecycle", fromlist=["router"]).__file__).read_text(encoding="utf-8")
    assert "git_connect" in source and lifecycle_router is not None
