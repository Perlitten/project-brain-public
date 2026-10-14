"""A checkout with uncommitted edits is indexed from its committed HEAD."""

import subprocess
from pathlib import Path

from brain.indexers.file_indexer import _export_head, _scan_repo_files, _uncommitted_tracked_changes
from brain.indexers.reliability import git_source_is_clean


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("VERSION = 1\n")
    (repo / "lib.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    return repo


def test_counts_only_tracked_changes(tmp_path):
    repo = _repo(tmp_path)
    assert _uncommitted_tracked_changes(repo) == 0
    (repo / "notes.txt").write_text("untracked\n")
    assert _uncommitted_tracked_changes(repo) == 0
    (repo / "app.py").write_text("VERSION = 2\n")
    assert _uncommitted_tracked_changes(repo) == 1


def test_head_snapshot_has_committed_content_and_is_clean(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("VERSION = 2  # work in progress\n")
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _export_head(repo, snapshot)

    assert (snapshot / "app.py").read_text() == "VERSION = 1\n"
    assert not (snapshot / ".brain-head.tar").exists()
    files = _scan_repo_files(snapshot)
    assert {f.relative_to(snapshot).as_posix() for f in files} == {"app.py", "lib.py"}
    assert git_source_is_clean(snapshot, files)
    # The user's checkout is untouched.
    assert (repo / "app.py").read_text() == "VERSION = 2  # work in progress\n"


def test_index_run_shape_reports_uncommitted_changes():
    from types import SimpleNamespace

    from apps.api.routers.web import shape_index_run

    run = SimpleNamespace(id=1, repository_id=1, commit_hash="abc", status="completed", progress={}, file_counts={},
                          verification={"indexed_from": "HEAD", "uncommitted_changes": 3}, started_at=None, completed_at=None)
    assert shape_index_run(run)["uncommitted_changes"] == 3
    run.verification = {}
    assert shape_index_run(run)["uncommitted_changes"] == 0
