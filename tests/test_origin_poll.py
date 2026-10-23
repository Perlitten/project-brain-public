"""Index freshness that follows origin without webhooks and with a read-only source.

Local bare repositories only (no network); the origin state table lives in a
throwaway sqlite database, so neither Postgres nor Redis is needed.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.config.settings import settings
from brain.database.models import RepositoryOriginState
from brain.indexers import source_mirror
from brain.indexers.source_mirror import (
    OriginSpec,
    fetch_into_mirror,
    mirror_path,
    redact_url,
    remote_head,
    resolve_origin_spec,
)
from brain.indexers.source_sync import SourceSyncTransientError
from brain.memory import origin_state_store, repo_freshness
from brain.memory.freshness_envelope import derive_state, freshness_envelope
from brain.memory.repo_freshness import apply_origin_state
from brain.workers import origin_poll, scheduler, tasks
from brain.workers.errors import PermanentJobError

_IDENTITY = ["-c", "user.name=Tester", "-c", "user.email=tester@example.com"]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _commit_and_push(dev: Path, name: str, content: str) -> str:
    (dev / name).write_text(content, encoding="utf-8")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", f"add {name}")
    _git(dev, "push", "-q", "origin", "HEAD:main")
    return _git(dev, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> SimpleNamespace:
    """origin (bare) <- dev clone (pushes); origin -> server checkout (what Brain indexes)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", origin.as_posix(), dev.as_posix())
    _git(dev, "checkout", "-q", "-b", "main")
    first = _commit_and_push(dev, "a.py", "print('a')\n")
    server = tmp_path / "server"
    _git(tmp_path, "clone", "-q", origin.as_posix(), server.as_posix())
    return SimpleNamespace(origin=origin, dev=dev, server=server, first=first, tmp=tmp_path)


@pytest.fixture(autouse=True)
def _origin_settings(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_SOURCE_MIRROR_DIR", "")
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES", "")
    monkeypatch.setattr(settings, "BRAIN_WEBHOOK_FETCH_SOURCE", True)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_REF", "refs/heads/main")


@pytest.fixture
def mirrors(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "BRAIN_SOURCE_MIRROR_DIR", root.as_posix())
    return root


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


# --- schedule ------------------------------------------------------------------


def test_poll_interval_maps_to_a_cron_and_zero_disables_it(monkeypatch):
    assert scheduler.origin_poll_cron(0) is None
    assert scheduler.origin_poll_cron(-5) is None
    assert scheduler.origin_poll_cron(30) == "* * * * *"
    assert scheduler.origin_poll_cron(60) == "* * * * *"
    assert scheduler.origin_poll_cron(300) == "*/5 * * * *"
    assert scheduler.origin_poll_cron(301) == "*/6 * * * *"
    assert scheduler.origin_poll_cron(3600) == "0 * * * *"
    assert scheduler.origin_poll_cron(7200) == "0 */2 * * *"
    assert scheduler.origin_poll_cron(86400 * 2) == "0 0 * * *"
    # Values from the environment that cron cannot repeat evenly round up to one it can.
    assert scheduler.origin_poll_cron(420) == "*/10 * * * *"
    assert scheduler.origin_poll_cron(2700) == "0 * * * *"
    assert scheduler.origin_poll_cron(5400) == "0 */2 * * *"
    assert scheduler.origin_poll_cron(5 * 3600) == "0 */6 * * *"
    for seconds in scheduler.ORIGIN_POLL_INTERVALS_S[1:]:
        step = seconds // 60 if seconds < 3600 else seconds // 3600
        assert (60 if seconds < 3600 else 24) % step == 0, seconds

    monkeypatch.setattr(settings, "BRAIN_ORIGIN_POLL_INTERVAL_S", 0)
    assert "origin_poll" not in {job.job_type for job in scheduler.active_schedules()}
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_POLL_INTERVAL_S", 300)
    polls = [job for job in scheduler.active_schedules() if job.job_type == "origin_poll"]
    assert len(polls) == 1 and polls[0].cron == "*/5 * * * *"


def test_every_scheduled_job_has_a_trigger_path_and_a_handler(monkeypatch):
    from apps.api.routers.jobs import router as jobs_router
    from apps.api.routers.scheduler import TRIGGER_PATHS

    monkeypatch.setattr(settings, "BRAIN_ORIGIN_POLL_INTERVAL_S", 300)
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATION_ENABLED", True)
    for job in scheduler.active_schedules():
        assert job.job_type in TRIGGER_PATHS
    assert TRIGGER_PATHS["origin_poll"] == "/jobs/origin-poll"
    assert "/jobs/origin-poll" in {getattr(route, "path", None) for route in jobs_router.routes}

    from brain.workers.queue import worker_pool_for_job

    assert worker_pool_for_job("origin_poll") == "fast"


# --- helpers -------------------------------------------------------------------


def test_redact_url_hides_credentials():
    text = "fatal: could not read from https://bot:s3cret@github.com/acme/repo.git"
    redacted = redact_url(text)
    assert "s3cret" not in redacted and "bot:" not in redacted
    assert "github.com/acme/repo.git" in redacted
    assert OriginSpec("https://x:tok@example.com/r.git", "main", True).display_url.count("tok") == 0


def test_redact_url_hides_scheme_less_credentials_and_keeps_scp_remotes():
    assert redact_url("oauth2:glpat-TOKEN@gitlab.com/acme/repo.git") == "***@gitlab.com/acme/repo.git"
    assert redact_url("ghp_TOKEN@github.com/acme/repo") == "***@github.com/acme/repo"
    assert redact_url("https://github.com/acme/repo.git?token=SECRET#private") == "https://github.com/acme/repo.git"
    assert redact_url("oauth2:TOKEN@gitlab.com/acme/repo.git?access_token=SECRET") == "***@gitlab.com/acme/repo.git"
    assert redact_url("git@github.com:acme/repo.git") == "git@github.com:acme/repo.git"
    assert redact_url("fatal: unable to access 'oauth2:TOKEN@gitlab.com/acme/repo.git/': 403") == (
        "fatal: unable to access '***@gitlab.com/acme/repo.git/': 403"
    )
    assert redact_url(redact_url("https://u:TOKEN@x.io/r oauth2:TOKEN@x.io/r")) == "https://***@x.io/r ***@x.io/r"
    assert OriginSpec("oauth2:TOKEN@gitlab.com/acme/repo.git", "main", True).display_url == "***@gitlab.com/acme/repo.git"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://user:SECRET@host/repo?token=SECRET#private", "https://***@host/repo"),
        ("(oauth2:SECRET@host/repo)", "(***@host/repo)"),
        ("https://host/path@revision", "https://host/path@revision"),
        ("https://bot:SECRET(foo)@host/repo", "https://***@host/repo"),
        ("https://bot:SEC'RET@host/repo", "https://***@host/repo"),
        ("git@host:org/repo", "git@host:org/repo"),
        ("x" * 100_000, "x" * 100_000),
        ("https://" + "x" * 100_000, "https://" + "x" * 100_000),
        ("https://" + "x" * 100_000 + "@host/repo", "https://***@host/repo"),
    ],
    ids=("credential-query", "wrapped-schemeless", "path-at", "password-paren", "password-quote",
         "scp", "long-plain", "long-host", "long-userinfo"),
)
def test_redact_url_linear_token_parsing(text, expected):
    assert redact_url(text) == expected
    assert redact_url(expected) == expected


@pytest.mark.parametrize("url", ["https://oauth2:TOKEN@gitlab.com/acme/repo.git", "oauth2:TOKEN@gitlab.com/acme/repo.git"])
def test_remote_head_error_never_carries_the_token(monkeypatch, url):
    failed = subprocess.CompletedProcess([], 128, "", f"fatal: unable to access '{url}/': The requested URL returned 403")
    monkeypatch.setattr(source_mirror, "_git", lambda *a, **k: failed)
    with pytest.raises(SourceSyncTransientError) as raised:
        remote_head(OriginSpec(url, "main", True))
    assert "TOKEN" not in str(raised.value) and "gitlab.com/acme/repo.git" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://oauth2:TOKEN@gitlab.com/acme/repo.git", "oauth2:TOKEN@gitlab.com/acme/repo.git"])
async def test_poll_stores_and_shows_the_remote_and_error_without_the_token(tmp_path, origin_db, monkeypatch, url):
    monkeypatch.setattr(origin_poll, "resolve_origin_spec", lambda path: OriginSpec(url, "main", True))
    # A caller that forgets to redact: the store still never keeps the credential.
    monkeypatch.setattr(origin_poll, "remote_head", MagicMock(side_effect=SourceSyncTransientError(f"git ls-remote {url} failed")))
    report = await origin_poll.poll_repository(SimpleNamespace(id=6, path=tmp_path.as_posix(), last_indexed_commit=None))
    assert report["status"] == "error" and "TOKEN" not in report["error"]
    state = await origin_state_store.get_origin_state(6)
    assert state["remote_url"].endswith("***@gitlab.com/acme/repo.git") and "TOKEN" not in state["poll_error"]

    await origin_state_store.upsert_origin_state(6, remote_url=url, poll_error=f"fetch {url} failed")
    stored = await origin_state_store.get_origin_state(6)
    assert "TOKEN" not in stored["remote_url"] and "TOKEN" not in stored["poll_error"]

    shown = apply_origin_state({}, {"poll_status": "error", "remote_url": url, "poll_error": f"fetch {url} failed"})
    assert "TOKEN" not in shown["origin_remote"] and "TOKEN" not in shown["origin_poll_error"]


@pytest.mark.asyncio
async def test_poll_never_stores_query_or_fragment_credentials(tmp_path, origin_db, monkeypatch):
    url = "https://github.com/acme/repo.git?token=QUERY_SECRET#FRAGMENT_SECRET"
    monkeypatch.setattr(source_mirror, "_git", lambda *a, **k: subprocess.CompletedProcess([], 128, "", f"fetch {url} failed"))
    monkeypatch.setattr(origin_poll, "resolve_origin_spec", lambda path: OriginSpec(url, "main", True))

    report = await origin_poll.poll_repository(SimpleNamespace(id=7, path=tmp_path.as_posix(), last_indexed_commit=None))
    state = await origin_state_store.get_origin_state(7)

    assert "SECRET" not in str(report)
    assert "SECRET" not in state["remote_url"] and "SECRET" not in state["poll_error"]


def test_origin_spec_from_checkout_and_from_configuration(repos, tmp_path, monkeypatch):
    spec = resolve_origin_spec(repos.server)
    assert spec is not None and spec.branch == "main" and spec.configured is False
    assert spec.url.rstrip("/").endswith("origin.git")

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    assert resolve_origin_spec(snapshot) is None
    monkeypatch.setattr(
        settings, "BRAIN_ORIGIN_REMOTES",
        json.dumps({snapshot.as_posix(): {"url": repos.origin.as_posix(), "branch": "main"}}),
    )
    configured = resolve_origin_spec(snapshot)
    assert configured is not None and configured.configured is True
    assert remote_head(configured) == repos.first


# --- origin poll ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_detects_a_new_remote_head_and_requests_one_reindex(repos, origin_db):
    new_sha = _commit_and_push(repos.dev, "b.py", "print('b')\n")
    repo = SimpleNamespace(id=1, path=repos.server.as_posix(), last_indexed_commit=repos.first)
    enqueue = AsyncMock(return_value="job-1")

    report = await origin_poll.poll_repository(repo, enqueue=enqueue)

    assert report["status"] == "ok"
    assert report["remote_head"] == new_sha
    assert report["reindex"] == "enqueued" and report["reindex_job_id"] == "job-1"
    enqueue.assert_awaited_once_with(repo, new_sha)
    state = await origin_state_store.get_origin_state(1)
    assert state["remote_head"] == new_sha
    assert state["tracked_branch"] == "main"
    assert state["poll_status"] == "ok"
    assert state["commits_behind_base"] == repos.first
    assert state["commits_behind_head"] == new_sha
    # The server checkout has not fetched the new commit, so the count is unknown, not zero.
    assert state["commits_behind"] is None
    assert state["last_reindex_job_id"] == "job-1" and state["last_reindex_head"] == new_sha

    # The next poll sees the same head and does not ask again.
    again = await origin_poll.poll_repository(repo, enqueue=enqueue)
    assert again["reindex"] == "already_requested"
    assert enqueue.await_count == 1

    # Once the index reaches origin nothing is requested and the count is zero.
    repo.last_indexed_commit = new_sha
    caught_up = await origin_poll.poll_repository(repo, enqueue=enqueue)
    assert caught_up["reindex"] == "not_needed" and caught_up["commits_behind"] == 0
    assert enqueue.await_count == 1


@pytest.mark.asyncio
async def test_poll_counts_commits_when_the_objects_are_local(repos, origin_db):
    _commit_and_push(repos.dev, "b.py", "b\n")
    new_sha = _commit_and_push(repos.dev, "c.py", "c\n")
    _git(repos.server, "fetch", "-q", "origin")
    repo = SimpleNamespace(id=2, path=repos.server.as_posix(), last_indexed_commit=repos.first)
    report = await origin_poll.poll_repository(repo, enqueue=AsyncMock(return_value="job-2"))
    assert report["remote_head"] == new_sha
    assert report["commits_behind"] == 2


@pytest.mark.asyncio
async def test_poll_without_an_origin_is_reported_as_unconfigured(tmp_path, origin_db):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    enqueue = AsyncMock()
    report = await origin_poll.poll_repository(
        SimpleNamespace(id=3, path=snapshot.as_posix(), last_indexed_commit=None), enqueue=enqueue
    )
    assert report["status"] == "unconfigured"
    assert "BRAIN_ORIGIN_REMOTES" in report["error"]
    enqueue.assert_not_awaited()
    assert (await origin_state_store.get_origin_state(3))["poll_status"] == "unconfigured"


@pytest.mark.asyncio
async def test_poll_after_origin_follow_is_switched_off_preserves_mirror_provenance(tmp_path, origin_db):
    snapshot = tmp_path / "app"
    snapshot.mkdir()
    head = "e" * 40
    await origin_state_store.upsert_origin_state(
        8, source_mode="mirror", mirror_head=head, remote_head=head, tracked_branch="main",
        remote_url="https://github.com/acme/app.git", commits_behind=0, last_reindex_head=head,
    )
    report = await origin_poll.poll_repository(
        SimpleNamespace(id=8, path=snapshot.as_posix(), last_indexed_commit=head), enqueue=AsyncMock()
    )
    assert report["status"] == "unconfigured"
    state = await origin_state_store.get_origin_state(8)
    assert state["poll_status"] == "unconfigured"
    assert state["source_mode"] == "mirror" and state["mirror_head"] == head
    assert state["remote_head"] is None
    assert state["last_reindex_head"] == head  # job history is kept


@pytest.mark.asyncio
async def test_poll_records_an_unreachable_origin_as_an_error(tmp_path, origin_db, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    monkeypatch.setattr(
        settings, "BRAIN_ORIGIN_REMOTES", json.dumps({snapshot.as_posix(): (tmp_path / "missing.git").as_posix()})
    )
    enqueue = AsyncMock()
    report = await origin_poll.poll_repository(
        SimpleNamespace(id=4, path=snapshot.as_posix(), last_indexed_commit="a" * 40), enqueue=enqueue
    )
    assert report["status"] == "error" and report["error"]
    enqueue.assert_not_awaited()
    assert (await origin_state_store.get_origin_state(4))["poll_status"] == "error"


@pytest.mark.asyncio
async def test_poll_with_fetch_disabled_records_but_does_not_reindex(repos, origin_db, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "b.py", "b\n")
    monkeypatch.setattr(settings, "BRAIN_WEBHOOK_FETCH_SOURCE", False)
    enqueue = AsyncMock()
    report = await origin_poll.poll_repository(
        SimpleNamespace(id=5, path=repos.server.as_posix(), last_indexed_commit=repos.first), enqueue=enqueue
    )
    assert report["reindex"] == "skipped" and report["remote_head"] == new_sha
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_keeps_a_mirror_served_repository_current(repos, origin_db, mirrors):
    new_sha = _commit_and_push(repos.dev, "b.py", "b\n")
    await origin_state_store.upsert_origin_state(6, source_mode="mirror")
    report = await origin_poll.poll_repository(
        SimpleNamespace(id=6, path=repos.server.as_posix(), last_indexed_commit=repos.first),
        enqueue=AsyncMock(return_value="job-6"),
    )
    assert report["reindex"] == "enqueued"
    assert report["commits_behind"] == 1  # counted from the freshly fetched mirror
    state = await origin_state_store.get_origin_state(6)
    assert state["mirror_head"] == new_sha


@pytest.mark.asyncio
async def test_run_origin_poll_filters_by_repo_path_and_isolates_failures(repos, origin_db, monkeypatch):
    good = SimpleNamespace(id=7, path=repos.server.as_posix(), last_indexed_commit=repos.first)
    other = SimpleNamespace(id=8, path=(repos.tmp / "other").as_posix(), last_indexed_commit=None)
    monkeypatch.setattr(origin_poll, "init_db", AsyncMock())
    monkeypatch.setattr(origin_poll, "list_all_repositories", AsyncMock(return_value=[good, other]))
    monkeypatch.setattr(origin_poll, "resolve_repo_path", lambda raw: Path(raw))

    only = await origin_poll.run_origin_poll({"repo_path": repos.server.as_posix()}, enqueue=AsyncMock())
    assert only["polled"] == 1 and only["repositories"][0]["repository_id"] == 7

    async def boom(repo, *, enqueue):
        if repo.id == 8:
            raise RuntimeError("https://u:pw@host/x.git exploded")
        return {"repository_id": repo.id, "status": "ok", "reindex": "enqueued"}

    monkeypatch.setattr(origin_poll, "poll_repository", boom)
    both = await origin_poll.run_origin_poll({}, enqueue=AsyncMock())
    assert both["polled"] == 2 and both["errors"] == 1 and both["reindex_enqueued"] == 1
    assert "pw" not in json.dumps(both)


@pytest.mark.asyncio
async def test_enqueue_reindex_shares_the_signed_webhook_idempotency_key(monkeypatch):
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="job-9")
    monkeypatch.setattr("brain.workers.queue.queue_for_job", lambda *a, **k: queue)
    job_id = await origin_poll.enqueue_reindex(SimpleNamespace(path="/indexed/app"), "f" * 40)
    assert job_id == "job-9"
    args, kwargs = queue.enqueue.await_args
    assert args[0] == "reindex"
    assert args[1]["source_revision"] == "f" * 40 and args[1]["repo_path"] == "/indexed/app"
    assert kwargs["idempotency_key"] == f"git-reindex:signed:{'f' * 40}"


# --- reindex through the mirror -------------------------------------------------


def _mirror_indexer(expect_file: str | None = None):
    """A FileIndexer stand-in for the mirror path: it reads ``source_root`` at ``source_commit``."""
    indexer = MagicMock()
    seen: dict = {}

    async def index_repository(path, clean=False, expected_revision="", source_root=None, source_commit=None):
        seen.update(path=Path(path), source_root=source_root, source_commit=source_commit,
                    expected_revision=expected_revision)
        if expect_file is not None:
            assert source_root is not None and (Path(source_root) / expect_file).is_file()
        return SimpleNamespace(id=7, path=Path(path).as_posix(), last_indexed_commit=source_commit)

    indexer.index_repository = AsyncMock(side_effect=index_repository)
    indexer.verification = {"status": "ok"}
    indexer.seen = seen
    return indexer


async def _reindex(params, indexer):
    with (
        patch.object(tasks, "init_db", new=AsyncMock()),
        patch.object(tasks, "FileIndexer", return_value=indexer),
        patch.object(tasks, "upsert_origin_state", new=AsyncMock(return_value=True)) as upsert,
    ):
        result = await tasks.run_reindex(params)
    return result, upsert


@pytest.mark.asyncio
async def test_read_only_checkout_is_indexed_from_the_mirror(repos, mirrors, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "h.py", "print('h')\n")
    monkeypatch.setattr("brain.indexers.source_sync.checkout_is_writable", lambda *a, **k: False)
    indexer = _mirror_indexer(expect_file="h.py")

    result, upsert = await _reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha}, indexer)

    assert result["commit_hash"] == new_sha and result["source_revision_verified"] is True
    sync = result["source_sync"]
    assert sync["status"] == "mirror" and sync["fallback_reason"] == "read_only"
    assert sync["commit"] == new_sha and sync["previous_head"] == repos.first
    assert indexer.seen["source_commit"] == new_sha
    assert indexer.seen["path"] == repos.server  # the index keeps the checkout's identity
    assert _git(repos.server, "rev-parse", "HEAD") == repos.first  # never touched
    mirror = mirror_path(repos.server)
    assert mirror is not None and mirror.is_dir() and mirror.parent == mirrors
    assert "origin" not in _git(mirror, "config", "--list")  # the URL is never stored
    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs["source_mode"] == "mirror"

    # A later run without a revision (nightly, self-heal) keeps reading the mirror
    # instead of moving the index back to the stale checkout.
    later = _mirror_indexer()
    with (
        patch.object(tasks, "get_repository_by_path", new=AsyncMock(return_value=SimpleNamespace(id=7))),
        patch.object(tasks, "get_origin_state", new=AsyncMock(return_value={"source_mode": "mirror"})),
    ):
        again, _ = await _reindex({"repo_path": repos.server.as_posix()}, later)
    assert later.seen["source_commit"] == new_sha
    assert again["source_sync"]["status"] == "mirror"


@pytest.mark.asyncio
async def test_checkout_without_origin_uses_the_configured_remote(repos, mirrors, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "n.py", "n\n")
    _git(repos.server, "remote", "remove", "origin")
    monkeypatch.setattr(
        settings, "BRAIN_ORIGIN_REMOTES", json.dumps({repos.server.as_posix(): repos.origin.as_posix()})
    )
    indexer = _mirror_indexer(expect_file="n.py")
    result, _ = await _reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha}, indexer)
    assert result["source_sync"]["status"] == "mirror"
    assert result["source_sync"]["fallback_reason"] == "no_origin"
    assert result["commit_hash"] == new_sha


@pytest.mark.asyncio
async def test_snapshot_source_without_git_uses_the_configured_remote(repos, mirrors, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "s.py", "s\n")
    snapshot = repos.tmp / "app"
    snapshot.mkdir()
    (snapshot / "a.py").write_text("print('a')\n", encoding="utf-8")
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES", json.dumps({snapshot.as_posix(): repos.origin.as_posix()}))
    indexer = _mirror_indexer(expect_file="s.py")
    result, _ = await _reindex({"repo_path": snapshot.as_posix(), "source_revision": new_sha}, indexer)
    assert result["source_sync"]["fallback_reason"] == "not_git"
    assert result["commit_hash"] == new_sha


@pytest.mark.asyncio
async def test_no_origin_and_no_configured_remote_fails_clearly(repos, mirrors):
    new_sha = _commit_and_push(repos.dev, "x.py", "x\n")
    _git(repos.server, "remote", "remove", "origin")
    indexer = _mirror_indexer()
    with pytest.raises(PermanentJobError) as excinfo:
        await _reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha}, indexer)
    assert "BRAIN_ORIGIN_REMOTES" in str(excinfo.value)
    assert excinfo.value.result["source_sync"]["reason"] == "no_origin"
    indexer.index_repository.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_checkout_without_mirrors_names_the_setting(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "r.py", "r\n")
    monkeypatch.setattr("brain.indexers.source_sync.checkout_is_writable", lambda *a, **k: False)
    indexer = _mirror_indexer()
    with pytest.raises(PermanentJobError) as excinfo:
        await _reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha}, indexer)
    assert "read-only" in str(excinfo.value) and "BRAIN_SOURCE_MIRROR_DIR" in str(excinfo.value)
    assert excinfo.value.result["source_sync"]["reason"] == "read_only"


@pytest.mark.asyncio
async def test_mirror_unreachable_sha_is_permanent_and_fetch_failure_is_retryable(repos, mirrors, monkeypatch):
    monkeypatch.setattr("brain.indexers.source_sync.checkout_is_writable", lambda *a, **k: False)
    bogus = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(PermanentJobError) as excinfo:
        await _reindex({"repo_path": repos.server.as_posix(), "source_revision": bogus}, _mirror_indexer())
    assert excinfo.value.result["source_sync"]["reason"] == "unreachable"
    assert excinfo.value.result["source_sync"]["mode"] == "mirror"

    # An origin that cannot be reached right now is not a verdict about the sha.
    snapshot = repos.tmp / "offline"
    snapshot.mkdir()
    monkeypatch.setattr(
        settings, "BRAIN_ORIGIN_REMOTES", json.dumps({snapshot.as_posix(): (repos.tmp / "gone.git").as_posix()})
    )
    with pytest.raises(SourceSyncTransientError):
        await _reindex({"repo_path": snapshot.as_posix(), "source_revision": bogus}, _mirror_indexer())


def test_fetch_into_mirror_skips_the_network_when_the_commit_is_present(repos, mirrors):
    spec = resolve_origin_spec(repos.server)
    first = fetch_into_mirror(repos.server, spec, repos.first)
    assert first.fetched is True and first.commit == repos.first
    again = fetch_into_mirror(repos.server, spec, repos.first)
    assert again.fetched is False and again.commit == repos.first


def test_mirror_disabled_is_a_clear_error(repos):
    from brain.indexers.source_sync import SourceSyncError

    with pytest.raises(SourceSyncError) as excinfo:
        fetch_into_mirror(repos.server, resolve_origin_spec(repos.server), repos.first)
    assert excinfo.value.reason == "mirror_disabled"
    assert source_mirror.mirror_root() is None


# --- freshness envelope -----------------------------------------------------------


def _current(indexed: str, **extra) -> dict:
    return {
        "status": "current", "indexed_commit": indexed, "source_head_commit": indexed,
        "source_revision_kind": "git", "age_seconds": 60, "last_indexed_at": "2026-10-18T00:00:00+00:00",
        "origin_ref": None, "origin_head": None, "commits_behind_origin": None, "note": None, **extra,
    }


def test_envelope_reports_origin_ahead_with_the_requested_reindex():
    indexed, remote = "a" * 40, "b" * 40
    state = {
        "remote_url": "https://github.com/acme/repo.git", "tracked_branch": "main", "remote_head": remote,
        "poll_status": "ok", "polled_at": datetime(2026, 10, 18, tzinfo=timezone.utc),
        "commits_behind": 3, "commits_behind_base": indexed, "commits_behind_head": remote,
        "last_reindex_job_id": "job-42", "last_reindex_head": remote, "source_mode": "checkout",
    }
    freshness = apply_origin_state(_current(indexed), state)
    assert freshness["origin_ref"] == "origin/main"
    assert freshness["origin_head"] == remote and freshness["commits_behind_origin"] == 3
    assert freshness["reindex_job_id"] == "job-42"
    assert freshness["origin_poll_status"] == "ok" and freshness["origin_polled_at"].startswith("2026-10-18")
    assert "3 commit(s) ahead" in freshness["note"]

    envelope = freshness_envelope(freshness)
    assert envelope["state"] == "stale_served" and envelope["reason"] == "origin_ahead_of_index"
    assert envelope["reindex_job_id"] == "job-42"
    assert envelope["origin_head"] == remote and envelope["commits_behind_origin"] == 3
    assert envelope["origin_remote"] == "https://github.com/acme/repo.git"
    assert envelope["index_source"] == "checkout"


def test_envelope_is_fresh_when_origin_matches_and_unknown_count_still_flags_origin_ahead():
    indexed = "a" * 40
    caught_up = apply_origin_state(
        _current(indexed), {"tracked_branch": "main", "remote_head": indexed, "poll_status": "ok",
                            "last_reindex_job_id": "old", "last_reindex_head": indexed}
    )
    assert caught_up["commits_behind_origin"] == 0 and "reindex_job_id" not in caught_up
    assert derive_state(caught_up) == ("fresh", "index_matches_source")

    unknown = apply_origin_state(_current(indexed), {"tracked_branch": "main", "remote_head": "c" * 40,
                                                     "poll_status": "ok"})
    assert unknown["commits_behind_origin"] is None
    assert "commit count is unknown" in unknown["note"]
    assert derive_state(unknown) == ("stale_served", "origin_ahead_of_index")


def test_mirror_served_repository_is_judged_against_the_mirror_head():
    old, indexed = "1" * 40, "2" * 40
    checkout_view = {**_current(indexed), "status": "behind", "source_head_commit": old,
                     "note": "Source head commit differs from indexed commit.", "commits_behind": 1}
    state = {"source_mode": "mirror", "mirror_head": indexed, "remote_head": indexed, "tracked_branch": "main",
             "poll_status": "ok"}
    freshness = apply_origin_state(checkout_view, state)
    assert freshness["status"] == "current" and freshness["note"] is None
    assert freshness["source_head_commit"] == indexed and freshness["checkout_head_commit"] == old
    assert freshness["source_revision_kind"] == "mirror" and freshness["index_source"] == "mirror"
    assert freshness_envelope(freshness)["state"] == "fresh"

    behind = apply_origin_state({**checkout_view, "indexed_commit": old}, state)
    assert behind["status"] == "behind" and "mirror" in behind["note"]

    assert apply_origin_state(checkout_view, None) is checkout_view


@pytest.mark.asyncio
async def test_assessment_overlays_the_stored_origin_state(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "z.py", "z\n")
    repo_freshness._FRESHNESS_CACHE.clear()
    monkeypatch.setattr(repo_freshness, "_count_indexed_files", AsyncMock(return_value=1))
    monkeypatch.setattr(
        repo_freshness, "_load_origin_state",
        AsyncMock(return_value={"tracked_branch": "main", "remote_head": new_sha, "poll_status": "ok",
                                "polled_at": datetime.now(timezone.utc) - timedelta(minutes=1)}),
    )
    repo = SimpleNamespace(id=11, path=repos.server.as_posix(), last_indexed_commit=repos.first,
                           last_indexed_at=datetime.now(timezone.utc), updated_at=None)
    freshness = await repo_freshness.assess_repository_freshness(repo)
    repo_freshness._FRESHNESS_CACHE.clear()
    # The checkout's remote-tracking ref still points at the first commit; the poll knows better.
    assert freshness["origin_head"] == new_sha
    assert freshness["status"] == "current"
    assert freshness_envelope(freshness)["reason"] == "origin_ahead_of_index"
