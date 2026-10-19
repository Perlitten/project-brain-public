import os
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from brain.memory.repo_freshness import assess_repository_freshness
from brain.memory.source_manifest import revision_matches
from scripts.write_source_manifest import build_manifest


def _init_git_repo(base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(base), check=True, capture_output=True, text=True)


def _write_repo_file(base: Path, name: str, content: str) -> None:
    (base / name).write_text(content, encoding="utf-8")


def _commit(base: Path, message: str) -> None:
    subprocess.run(
        ["git", "add", "."],
        cwd=str(base),
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=tester@example.com", "commit", "-m", message],
        cwd=str(base),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Tester"
    env["GIT_AUTHOR_EMAIL"] = "tester@example.com"
    env["GIT_COMMITTER_NAME"] = "Tester"
    env["GIT_COMMITTER_EMAIL"] = "tester@example.com"
    return env


def _git_hash(base: Path, rev: str = "HEAD") -> str:
    result = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=str(base),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_two_commit_repo(base: Path) -> tuple[str, str]:
    _init_git_repo(base)
    _write_repo_file(base, "file_a.txt", "first")
    _commit(base, "first")
    first_commit = _git_hash(base)

    _write_repo_file(base, "file_b.txt", "second")
    _commit(base, "second")
    return first_commit, _git_hash(base)


def _make_one_commit_repo(base: Path) -> str:
    _init_git_repo(base)
    _write_repo_file(base, "file_a.txt", "first")
    _commit(base, "first")
    return _git_hash(base)


@pytest.mark.asyncio
async def test_source_missing_marked_as_source_missing(tmp_path: Path):
    result = await assess_repository_freshness(
        SimpleNamespace(
            path=str(tmp_path / "missing-repo"),
            last_indexed_commit="abc123",
            last_indexed_at=datetime.now(timezone.utc),
        )
    )
    assert result["status"] == "source_missing"
    assert result["source_present"] is False
    assert result["source_head_commit"] is None


@pytest.mark.asyncio
async def test_unindexed_when_no_indexed_commit_and_no_files(tmp_path: Path):
    repo_path = tmp_path / "unindexed-repo"
    repo_path.mkdir()
    result = await assess_repository_freshness(
        SimpleNamespace(
            path=str(repo_path),
            files=[],
            last_indexed_commit=None,
            last_indexed_at=datetime.now(timezone.utc),
        )
    )
    assert result["status"] == "unindexed"


@pytest.mark.asyncio
async def test_behind_status_includes_commits_behind(tmp_path: Path):
    repo_path = tmp_path / "repo-ahead"
    old_commit, _ = _make_two_commit_repo(repo_path)
    result = await assess_repository_freshness(
        SimpleNamespace(
            path=str(repo_path),
            last_indexed_commit=old_commit,
            last_indexed_at=datetime.now(timezone.utc),
        )
    )
    assert result["status"] == "behind"
    assert result["commits_behind"] == 1
    assert result["source_head_commit"] != old_commit


@pytest.mark.asyncio
async def test_behind_status_without_count_when_rev_list_fails(tmp_path: Path, monkeypatch):
    repo_path = tmp_path / "repo-behind-no-count"
    old_commit, _ = _make_two_commit_repo(repo_path)
    original_run = subprocess.run

    def fail_on_rev_list(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "rev-list" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="fatal")
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr("brain.memory.repo_freshness.subprocess.run", fail_on_rev_list)
    result = await assess_repository_freshness(
        SimpleNamespace(
            path=str(repo_path),
            last_indexed_commit=old_commit,
            last_indexed_at=datetime.now(timezone.utc),
        )
    )
    assert result["status"] == "behind"
    assert result["commits_behind"] is None


@pytest.mark.asyncio
async def test_stale_by_age_is_not_current(tmp_path: Path, monkeypatch):
    repo_path = tmp_path / "repo-stale"
    head_commit = _make_one_commit_repo(repo_path)
    monkeypatch.setenv("BRAIN_INDEX_STALE_AFTER_DAYS", "1")
    result = await assess_repository_freshness(
        SimpleNamespace(
            path=str(repo_path),
            last_indexed_commit=head_commit,
            last_indexed_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
    )
    assert result["status"] == "stale"
    assert result["source_head_commit"] == head_commit


@pytest.mark.asyncio
async def test_current_when_index_matches_head(tmp_path: Path):
    repo_path = tmp_path / "repo-current"
    head_commit = _make_one_commit_repo(repo_path)
    result = await assess_repository_freshness(
        SimpleNamespace(
            path=str(repo_path),
            last_indexed_commit=head_commit,
            last_indexed_at=datetime.now(timezone.utc),
        )
    )
    assert result["status"] == "current"
    assert result["commits_behind"] is None


# --- cases added by the orchestrator after reviewing the first implementation ---
# The original version read repo.last_indexed_at, a column the Repository model
# does not have, so age was always None and the age-based verdict never fired;
# and any non-git source (e.g. a tree baked into the image) was branded stale
# forever, which would have attached a freshness warning to every answer.


@pytest.mark.asyncio
async def test_non_git_source_without_manifest_is_unverifiable(tmp_path: Path):
    freshness = await assess_repository_freshness(
        SimpleNamespace(
            id=None,
            path=str(tmp_path),
            last_indexed_commit="deadbeef",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            files=["a.py"],
        )
    )
    assert freshness["status"] == "unverifiable"
    assert ".brain-source-manifest.json" in (freshness["note"] or "")


@pytest.mark.asyncio
async def test_non_git_manifest_source_goes_stale_once_old(tmp_path: Path, monkeypatch):
    (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
    manifest = build_manifest(tmp_path)
    (tmp_path / ".brain-source-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_INDEX_STALE_AFTER_DAYS", "14")
    freshness = await assess_repository_freshness(
        SimpleNamespace(
            id=None,
            path=str(tmp_path),
            last_indexed_commit=manifest["revision"],
            updated_at=datetime.now(timezone.utc) - timedelta(days=40),
            files=["a.py"],
        )
    )
    assert freshness["status"] == "stale"
    assert freshness["age_seconds"] > 14 * 24 * 3600


@pytest.mark.asyncio
async def test_non_git_manifest_revision_proves_current(tmp_path: Path):
    (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
    manifest = build_manifest(tmp_path, source_revision="source-123")
    (tmp_path / ".brain-source-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    freshness = await assess_repository_freshness(
        SimpleNamespace(
            id=None,
            path=str(tmp_path),
            last_indexed_commit=manifest["revision"],
            updated_at=datetime.now(timezone.utc),
            files=["a.py"],
        )
    )
    assert freshness["status"] == "current"
    assert freshness["source_manifest_digest"] == manifest["content_digest"]
    assert freshness["source_revision_kind"] == "snapshot_manifest"


def test_bare_git_sha_matches_content_bound_snapshot_revision_only_by_source():
    sha = "a" * 40
    snapshot = f"snapshot:{sha}:{'b' * 64}"
    assert revision_matches(sha, snapshot) is True
    assert revision_matches(snapshot, sha) is True
    assert revision_matches(snapshot, f"snapshot:{sha}:{'c' * 64}") is False
    assert revision_matches("different", snapshot) is False


@pytest.mark.asyncio
async def test_age_falls_back_to_updated_at(tmp_path: Path):
    # No last_indexed_at attribute at all — exactly what the real model looks like.
    freshness = await assess_repository_freshness(
        SimpleNamespace(
            id=None,
            path=str(tmp_path),
            last_indexed_commit="deadbeef",
            updated_at=datetime.now(timezone.utc) - timedelta(days=3),
            files=["a.py"],
        )
    )
    assert freshness["age_seconds"] is not None
    assert 2 * 24 * 3600 < freshness["age_seconds"] < 4 * 24 * 3600


@pytest.mark.asyncio
async def test_repeated_assessment_is_served_from_cache(tmp_path: Path, monkeypatch):
    from brain.memory import repo_freshness as module

    calls = {"n": 0}
    original = module._assess_repository_freshness_uncached

    async def counting(repo):
        calls["n"] += 1
        return await original(repo)

    monkeypatch.setattr(module, "_assess_repository_freshness_uncached", counting)
    module._FRESHNESS_CACHE.clear()

    repo = SimpleNamespace(
        id=None,
        path=str(tmp_path),
        last_indexed_commit="deadbeef",
        updated_at=datetime.now(timezone.utc),
        files=["a.py"],
    )
    first = await module.assess_repository_freshness(repo)
    second = await module.assess_repository_freshness(repo)
    assert calls["n"] == 1, "second assessment should come from the cache"
    assert first == second


def test_git_command_is_bounded_by_a_timeout():
    # A git call that never returns would block the event loop for every request,
    # so the timeout is part of the contract, not an optimisation.
    from brain.memory import repo_freshness as module

    assert module.GIT_TIMEOUT_SECONDS > 0
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(cmd="git", timeout=module.GIT_TIMEOUT_SECONDS)

    original_run = subprocess.run
    subprocess.run = fake_run
    try:
        result = module._run_git_command(Path("."), ["rev-parse", "HEAD"])
    finally:
        subprocess.run = original_run

    assert captured.get("timeout") == module.GIT_TIMEOUT_SECONDS
    assert result.returncode == 1, "a timed-out git call must degrade, not raise"


@pytest.mark.asyncio
async def test_lazy_relationship_failure_does_not_become_a_data_verdict(tmp_path: Path):
    # Mirrors a detached SQLAlchemy instance: touching .files raises. The result
    # must be `unknown`, never `unindexed`/`stale`, so a bug here cannot be read
    # as a fact about the index.
    class Detached:
        id = None
        path = str(tmp_path)
        last_indexed_commit = "deadbeef"
        updated_at = datetime.now(timezone.utc)

        @property
        def files(self):
            raise RuntimeError("Instance is not bound to a Session")

    freshness = await assess_repository_freshness(Detached())
    assert freshness["status"] == "unverifiable", freshness
    assert freshness["status"] != "unindexed"


@pytest.mark.asyncio
async def test_unexpected_failure_reports_unknown(tmp_path: Path, monkeypatch):
    from brain.memory import repo_freshness as module

    module._FRESHNESS_CACHE.clear()

    def boom(*_args, **_kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(module, "_is_git_worktree", boom)
    freshness = await assess_repository_freshness(
        SimpleNamespace(
            id=None,
            path=str(tmp_path),
            last_indexed_commit="deadbeef",
            updated_at=datetime.now(timezone.utc),
            files=["a.py"],
        )
    )
    assert freshness["status"] == "unknown"
    assert "git exploded" in freshness["note"]


# --- freshness relative to origin, not just the checkout HEAD ------------------
# A server that never pulls looked "current" forever: its HEAD matched the index
# while the project moved on. origin_head / commits_behind_origin make that visible.


def _git_cmd(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=tester@example.com", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _origin_with_checkout(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return (dev clone that pushes, server checkout, first commit)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git_cmd(origin, "init", "-q", "--bare", "-b", "main")
    dev = tmp_path / "dev"
    _git_cmd(tmp_path, "clone", "-q", origin.as_posix(), dev.as_posix())
    _git_cmd(dev, "checkout", "-q", "-b", "main")
    _write_repo_file(dev, "a.txt", "first")
    _git_cmd(dev, "add", ".")
    _git_cmd(dev, "commit", "-q", "-m", "first")
    _git_cmd(dev, "push", "-q", "origin", "HEAD:main")
    server = tmp_path / "server"
    _git_cmd(tmp_path, "clone", "-q", origin.as_posix(), server.as_posix())
    return dev, server, _git_cmd(server, "rev-parse", "HEAD")


def _push_commit(dev: Path, name: str) -> str:
    _write_repo_file(dev, name, name)
    _git_cmd(dev, "add", ".")
    _git_cmd(dev, "commit", "-q", "-m", name)
    _git_cmd(dev, "push", "-q", "origin", "HEAD:main")
    return _git_cmd(dev, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_current_checkout_reports_commits_behind_origin_after_a_push(tmp_path: Path):
    dev, server, first = _origin_with_checkout(tmp_path)
    second = _push_commit(dev, "b.txt")
    # The webhook reindex fetches; here we fetch without moving HEAD, which is
    # exactly the "server checkout nobody pulls" shape.
    _git_cmd(server, "fetch", "-q", "origin")

    result = await assess_repository_freshness(
        SimpleNamespace(path=str(server), last_indexed_commit=first, last_indexed_at=datetime.now(timezone.utc))
    )
    assert result["status"] == "current"  # HEAD still matches the index
    assert result["commits_behind"] is None
    assert result["origin_ref"] == "origin/main"
    assert result["origin_head"] == second
    assert result["commits_behind_origin"] == 1
    assert "origin/main is 1 commit(s) ahead" in (result["note"] or "")


@pytest.mark.asyncio
async def test_origin_fields_are_zero_when_index_matches_origin(tmp_path: Path):
    _dev, server, first = _origin_with_checkout(tmp_path)
    result = await assess_repository_freshness(
        SimpleNamespace(path=str(server), last_indexed_commit=first, last_indexed_at=datetime.now(timezone.utc))
    )
    assert result["status"] == "current"
    assert result["origin_head"] == first
    assert result["commits_behind_origin"] == 0
    assert result["note"] is None


@pytest.mark.asyncio
async def test_origin_head_from_ls_remote_when_enabled(tmp_path: Path, monkeypatch):
    dev, server, first = _origin_with_checkout(tmp_path)
    second = _push_commit(dev, "c.txt")
    # No fetch on the server: the local tracking ref still says `first`.
    monkeypatch.setenv("BRAIN_FRESHNESS_LS_REMOTE", "true")
    result = await assess_repository_freshness(
        SimpleNamespace(path=str(server), last_indexed_commit=first, last_indexed_at=datetime.now(timezone.utc))
    )
    assert result["origin_head"] == second
    # The remote head is known but its commits are not local, so the count
    # stays unknown rather than guessed; the webhook fetch fills it in.
    assert result["commits_behind_origin"] is None

    monkeypatch.delenv("BRAIN_FRESHNESS_LS_REMOTE")
    stale_view = await assess_repository_freshness(
        SimpleNamespace(path=str(server), last_indexed_commit=first, updated_at=datetime.now(timezone.utc))
    )
    assert stale_view["origin_head"] == first
    assert stale_view["commits_behind_origin"] == 0


@pytest.mark.asyncio
async def test_origin_fields_are_none_without_a_remote(tmp_path: Path):
    repo_path = tmp_path / "local-only"
    head = _make_one_commit_repo(repo_path)
    result = await assess_repository_freshness(
        SimpleNamespace(path=str(repo_path), last_indexed_commit=head, last_indexed_at=datetime.now(timezone.utc))
    )
    assert result["status"] == "current"
    assert result["origin_ref"] is None
    assert result["origin_head"] is None
    assert result["commits_behind_origin"] is None


@pytest.mark.asyncio
async def test_describe_origin_for_a_non_git_directory(tmp_path: Path):
    from brain.memory.repo_freshness import describe_origin

    assert await describe_origin(tmp_path, "deadbeef") == {
        "origin_ref": None,
        "origin_head": None,
        "commits_behind_origin": None,
    }


# Origin follow switched off (BRAIN_ORIGIN_REMOTES no longer names the source,
# or BRAIN_SOURCE_MIRROR_DIR is blank) must not leave the repository judged
# against a mirror it is no longer indexed from. Prod /app: the snapshot was
# reindexed from its manifest, yet the persisted row kept it "behind" forever.


def _snapshot_with_manifest(root: Path, source_revision: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("print('a')", encoding="utf-8")
    manifest = build_manifest(root, source_revision=source_revision)
    (root / ".brain-source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _stale_mirror_row(mirror_head: str) -> dict:
    return {
        "source_mode": "mirror", "mirror_head": mirror_head, "remote_head": mirror_head,
        "tracked_branch": "main", "remote_url": "https://github.com/acme/app.git", "poll_status": "ok",
        "commits_behind": 0, "commits_behind_base": mirror_head, "commits_behind_head": mirror_head,
    }


async def _assess_with_row(monkeypatch, root: Path, indexed: str, row: dict) -> dict:
    from unittest.mock import AsyncMock

    from brain.memory import repo_freshness

    repo_freshness._FRESHNESS_CACHE.clear()
    monkeypatch.setattr(repo_freshness, "_load_origin_state", AsyncMock(return_value=row))
    try:
        return await assess_repository_freshness(
            SimpleNamespace(id=7, path=str(root), last_indexed_commit=indexed,
                            updated_at=datetime.now(timezone.utc), files=["a.py"])
        )
    finally:
        repo_freshness._FRESHNESS_CACHE.clear()


@pytest.mark.asyncio
async def test_stale_mirror_row_is_ignored_once_origin_follow_is_off(tmp_path: Path, monkeypatch):
    from brain.config.settings import settings

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES", "")
    monkeypatch.setattr(settings, "BRAIN_SOURCE_MIRROR_DIR", "")
    deployed, old_mirror = "d" * 40, "0" * 40
    root = tmp_path / "app"
    manifest = _snapshot_with_manifest(root, deployed)

    freshness = await _assess_with_row(monkeypatch, root, manifest["revision"], _stale_mirror_row(old_mirror))

    assert freshness["status"] == "current"
    assert freshness["source_revision_kind"] == "snapshot_manifest"
    assert freshness.get("index_source") != "mirror"
    assert freshness["origin_head"] is None and freshness["commits_behind_origin"] is None


@pytest.mark.asyncio
async def test_mirror_row_still_applies_while_origin_follow_is_on(tmp_path: Path, monkeypatch):
    from brain.config.settings import settings

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "app"
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES",
                        json.dumps({root.as_posix(): "https://github.com/acme/app.git"}))
    monkeypatch.setattr(settings, "BRAIN_SOURCE_MIRROR_DIR", (tmp_path / "mirrors").as_posix())
    mirror_head = "e" * 40
    _snapshot_with_manifest(root, "d" * 40)

    freshness = await _assess_with_row(monkeypatch, root, mirror_head, _stale_mirror_row(mirror_head))

    assert freshness["index_source"] == "mirror"
    assert freshness["source_head_commit"] == mirror_head
    assert freshness["status"] == "current"


@pytest.mark.asyncio
async def test_mirror_marking_is_ignored_when_mirrors_are_disabled(tmp_path: Path, monkeypatch):
    from brain.config.settings import settings

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "app"
    monkeypatch.setattr(settings, "BRAIN_ORIGIN_REMOTES",
                        json.dumps({root.as_posix(): "https://github.com/acme/app.git"}))
    monkeypatch.setattr(settings, "BRAIN_SOURCE_MIRROR_DIR", "")
    deployed = "d" * 40
    manifest = _snapshot_with_manifest(root, deployed)

    freshness = await _assess_with_row(monkeypatch, root, manifest["revision"], _stale_mirror_row("0" * 40))

    # The origin is still followed (its head is reported), but nothing is served from a mirror.
    assert freshness["status"] == "current"
    assert freshness.get("index_source") != "mirror"
    assert freshness["source_revision_kind"] == "snapshot_manifest"
    assert freshness["origin_head"] == "0" * 40
