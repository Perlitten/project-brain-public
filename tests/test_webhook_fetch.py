"""The webhook reindex follows origin: fetch + detached checkout at the delivered commit.

Uses a bare "origin" repository, a server-side checkout cloned from it and a
developer clone that pushes new commits. No Postgres or Redis required.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.indexers.source_sync import (
    SourceSyncError,
    SourceSyncTransientError,
    checkout_is_writable,
    sync_checkout_to_revision,
)
from brain.workers import tasks
from brain.workers.errors import PermanentJobError

_IDENTITY = ["-c", "user.name=Tester", "-c", "user.email=tester@example.com"]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_and_push(dev: Path, name: str, content: str) -> str:
    (dev / name).write_text(content, encoding="utf-8")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", f"add {name}")
    _git(dev, "push", "-q", "origin", "HEAD:main")
    return _git(dev, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> SimpleNamespace:
    """origin (bare) <- dev clone (pushes) ; origin -> server checkout (what Brain indexes)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")

    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", origin.as_posix(), dev.as_posix())
    _git(dev, "checkout", "-q", "-b", "main")
    first = _commit_and_push(dev, "a.py", "print('a')\n")

    server = tmp_path / "server"
    _git(tmp_path, "clone", "-q", origin.as_posix(), server.as_posix())
    assert _git(server, "rev-parse", "HEAD") == first
    return SimpleNamespace(origin=origin, dev=dev, server=server, first=first)


# --- sync_checkout_to_revision -------------------------------------------------


def test_sync_fetches_and_detaches_at_a_commit_missing_locally(repos):
    new_sha = _commit_and_push(repos.dev, "b.py", "print('b')\n")
    assert _git(repos.server, "rev-parse", "HEAD") == repos.first

    outcome = sync_checkout_to_revision(repos.server, new_sha)

    assert outcome.changed is True
    assert outcome.fetched is True
    assert outcome.previous_head == repos.first
    assert outcome.head == new_sha
    assert _git(repos.server, "rev-parse", "HEAD") == new_sha
    assert _git(repos.server, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached
    assert (repos.server / "b.py").is_file()
    assert outcome.to_dict()["status"] == "synced"


def test_sync_at_requested_revision_changes_nothing(repos, monkeypatch):
    # A read-only mount that already sits at the commit must still index: no
    # writability probe, no fetch, no checkout.
    monkeypatch.setattr(
        "brain.indexers.source_sync.checkout_is_writable",
        lambda *a, **k: pytest.fail("must not probe a checkout that is already current"),
    )
    outcome = sync_checkout_to_revision(repos.server, repos.first)
    assert outcome.changed is False
    assert outcome.fetched is False
    assert outcome.to_dict()["status"] == "already_at_revision"


def test_sync_accepts_a_short_sha(repos):
    new_sha = _commit_and_push(repos.dev, "c.py", "print('c')\n")
    outcome = sync_checkout_to_revision(repos.server, new_sha[:12])
    assert outcome.head == new_sha


def test_sync_unreachable_revision_is_permanent(repos):
    with pytest.raises(SourceSyncError) as excinfo:
        sync_checkout_to_revision(repos.server, "0123456789abcdef0123456789abcdef01234567")
    assert excinfo.value.reason == "unreachable"
    assert "not reachable from origin" in str(excinfo.value)
    assert _git(repos.server, "rev-parse", "HEAD") == repos.first


def test_sync_refuses_a_read_only_checkout(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "d.py", "print('d')\n")
    monkeypatch.setattr("brain.indexers.source_sync.checkout_is_writable", lambda *a, **k: False)
    with pytest.raises(SourceSyncError) as excinfo:
        sync_checkout_to_revision(repos.server, new_sha)
    assert excinfo.value.reason == "read_only"
    assert "read-only" in str(excinfo.value)
    assert new_sha[:16] in str(excinfo.value)
    assert _git(repos.server, "rev-parse", "HEAD") == repos.first


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores mode bits")
def test_checkout_is_writable_detects_a_read_only_git_dir(repos):
    git_dir = repos.server / ".git"
    original = stat.S_IMODE(git_dir.stat().st_mode)
    try:
        git_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        assert checkout_is_writable(repos.server) is False
    finally:
        git_dir.chmod(original)
    assert checkout_is_writable(repos.server) is True


def test_sync_refuses_a_dirty_checkout(repos):
    new_sha = _commit_and_push(repos.dev, "e.py", "print('e')\n")
    (repos.server / "a.py").write_text("print('edited locally')\n", encoding="utf-8")
    with pytest.raises(SourceSyncError) as excinfo:
        sync_checkout_to_revision(repos.server, new_sha)
    assert excinfo.value.reason == "dirty"
    assert "uncommitted change" in str(excinfo.value)


def test_sync_refuses_a_non_git_source(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(SourceSyncError) as excinfo:
        sync_checkout_to_revision(snapshot, "0123456789abcdef0123456789abcdef01234567")
    assert excinfo.value.reason == "not_git"


def test_sync_without_origin_remote_is_permanent(repos):
    _git(repos.server, "remote", "remove", "origin")
    with pytest.raises(SourceSyncError) as excinfo:
        sync_checkout_to_revision(repos.server, "0123456789abcdef0123456789abcdef01234567")
    assert excinfo.value.reason == "no_origin"


def test_sync_fetch_timeout_is_transient(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "f.py", "print('f')\n")

    def slow_git(repo_path, args, *, timeout):
        if args and args[0] == "fetch":
            raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=timeout)
        return original(repo_path, args, timeout=timeout)

    from brain.indexers import source_sync

    original = source_sync._git
    monkeypatch.setattr(source_sync, "_git", slow_git)
    with pytest.raises(SourceSyncTransientError):
        sync_checkout_to_revision(repos.server, new_sha, timeout=1)


# --- run_reindex ---------------------------------------------------------------


def _indexer_returning(repo_path: Path):
    """A FileIndexer stand-in that reports whatever the checkout HEAD is when it runs."""
    indexer = MagicMock()

    async def index_repository(path, clean=False, expected_revision=""):
        return SimpleNamespace(id=7, path=Path(path).as_posix(), last_indexed_commit=_git(repo_path, "rev-parse", "HEAD"))

    indexer.index_repository = AsyncMock(side_effect=index_repository)
    indexer.verification = {"status": "ok"}
    return indexer


@pytest.mark.asyncio
async def test_run_reindex_fetches_origin_when_the_webhook_sha_is_new(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "g.py", "print('g')\n")
    monkeypatch.setattr(tasks.settings, "BRAIN_WEBHOOK_FETCH_SOURCE", True)
    indexer = _indexer_returning(repos.server)
    with (
        patch.object(tasks.jobs, "init_db", new=AsyncMock()),
        patch.object(tasks.jobs, "FileIndexer", return_value=indexer),
    ):
        result = await tasks.run_reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha})

    assert _git(repos.server, "rev-parse", "HEAD") == new_sha
    assert result["commit_hash"] == new_sha
    assert result["source_revision_verified"] is True
    assert result["source_sync"]["status"] == "synced"
    assert result["source_sync"]["fetched"] is True
    assert result["source_sync"]["previous_head"] == repos.first
    assert indexer.index_repository.await_args.kwargs == {"clean": False, "expected_revision": new_sha}


@pytest.mark.asyncio
async def test_run_reindex_at_head_does_not_touch_the_checkout(repos, monkeypatch):
    monkeypatch.setattr(tasks.settings, "BRAIN_WEBHOOK_FETCH_SOURCE", True)
    monkeypatch.setattr(
        tasks._source_plan,
        "sync_checkout_to_revision",
        lambda *a, **k: pytest.fail("no sync when HEAD already matches"),
    )
    indexer = _indexer_returning(repos.server)
    with (
        patch.object(tasks.jobs, "init_db", new=AsyncMock()),
        patch.object(tasks.jobs, "FileIndexer", return_value=indexer),
    ):
        result = await tasks.run_reindex({"repo_path": repos.server.as_posix(), "source_revision": repos.first})
    assert result["source_sync"]["status"] == "already_at_revision"
    assert result["source_revision_verified"] is True


@pytest.mark.asyncio
async def test_run_reindex_unreachable_sha_is_a_permanent_failure(repos, monkeypatch):
    monkeypatch.setattr(tasks.settings, "BRAIN_WEBHOOK_FETCH_SOURCE", True)
    indexer = _indexer_returning(repos.server)
    bogus = "0123456789abcdef0123456789abcdef01234567"
    with (
        patch.object(tasks.jobs, "init_db", new=AsyncMock()),
        patch.object(tasks.jobs, "FileIndexer", return_value=indexer),
        pytest.raises(PermanentJobError) as excinfo,
    ):
        await tasks.run_reindex({"repo_path": repos.server.as_posix(), "source_revision": bogus})
    assert "not reachable from origin" in str(excinfo.value)
    assert excinfo.value.result["source_sync"]["reason"] == "unreachable"
    assert excinfo.value.result["source_revision_verified"] is False
    indexer.index_repository.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_reindex_read_only_checkout_fails_clearly(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "h.py", "print('h')\n")
    monkeypatch.setattr(tasks.settings, "BRAIN_WEBHOOK_FETCH_SOURCE", True)
    monkeypatch.setattr("brain.indexers.source_sync.checkout_is_writable", lambda *a, **k: False)
    indexer = _indexer_returning(repos.server)
    with (
        patch.object(tasks.jobs, "init_db", new=AsyncMock()),
        patch.object(tasks.jobs, "FileIndexer", return_value=indexer),
        pytest.raises(PermanentJobError) as excinfo,
    ):
        await tasks.run_reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha})
    assert "read-only" in str(excinfo.value)
    assert excinfo.value.result["source_sync"]["reason"] == "read_only"
    assert _git(repos.server, "rev-parse", "HEAD") == repos.first
    indexer.index_repository.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_reindex_with_fetch_disabled_keeps_the_old_mismatch_failure(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "i.py", "print('i')\n")
    monkeypatch.setattr(tasks.settings, "BRAIN_WEBHOOK_FETCH_SOURCE", False)
    indexer = _indexer_returning(repos.server)
    with (
        patch.object(tasks.jobs, "init_db", new=AsyncMock()),
        patch.object(tasks.jobs, "FileIndexer", return_value=indexer),
        pytest.raises(PermanentJobError) as excinfo,
    ):
        await tasks.run_reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha})
    assert "BRAIN_WEBHOOK_FETCH_SOURCE is off" in str(excinfo.value)
    assert excinfo.value.result["source_sync"]["status"] == "disabled"
    assert _git(repos.server, "rev-parse", "HEAD") == repos.first
    indexer.index_repository.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_reindex_transient_fetch_failure_is_retryable(repos, monkeypatch):
    new_sha = _commit_and_push(repos.dev, "j.py", "print('j')\n")
    monkeypatch.setattr(tasks.settings, "BRAIN_WEBHOOK_FETCH_SOURCE", True)

    def failing_sync(*_a, **_k):
        raise SourceSyncTransientError("git fetch origin failed: could not resolve host")

    monkeypatch.setattr(tasks._source_plan, "sync_checkout_to_revision", failing_sync)
    indexer = _indexer_returning(repos.server)
    with (
        patch.object(tasks.jobs, "init_db", new=AsyncMock()),
        patch.object(tasks.jobs, "FileIndexer", return_value=indexer),
        pytest.raises(SourceSyncTransientError),
    ):
        await tasks.run_reindex({"repo_path": repos.server.as_posix(), "source_revision": new_sha})
    indexer.index_repository.assert_not_awaited()
