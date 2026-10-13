"""Real Git fixtures for generated outputs, tracked source and path safety."""

import subprocess
from types import SimpleNamespace

import pytest

from brain.indexers.file_indexer import _scan_repo_files
from brain.indexers.reliability import git_source_is_clean


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
    )


def _commit(root):
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "source")


def test_next_generated_outputs_do_not_enter_the_committed_index(tmp_path):
    _git(tmp_path, "init")
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    source = web / "page.tsx"
    source.write_text("export const value = 1;\n")
    (web / ".gitignore").write_text("AGENTS.md\nnext-env.d.ts\n*.tsbuildinfo\n.next/\n")
    _git(tmp_path, "add", ".")
    _commit(tmp_path)
    before = _scan_repo_files(tmp_path)
    for name in ("AGENTS.md", "next-env.d.ts", "tsconfig.tsbuildinfo"):
        (web / name).write_text("generated\n")
    (web / ".next").mkdir()
    (web / ".next" / "generated.ts").write_text("generated\n")
    assert _scan_repo_files(tmp_path) == before
    assert source in before
    assert git_source_is_clean(tmp_path, _scan_repo_files(tmp_path))
    # Source fidelity is unchanged: dirty edits are refused, committed edits
    # remain discoverable after generated artifacts have appeared.
    source.write_text("export const value = 2;\n")
    assert not git_source_is_clean(tmp_path, _scan_repo_files(tmp_path))
    _git(tmp_path, "add", "apps/web/page.tsx")
    _commit(tmp_path)
    assert source in _scan_repo_files(tmp_path)
    assert git_source_is_clean(tmp_path, _scan_repo_files(tmp_path))


def test_tracked_source_is_not_hidden_by_gitignore(tmp_path):
    _git(tmp_path, "init")
    (tmp_path / ".gitignore").write_text("*.ts\n")
    tracked = tmp_path / "tracked.ts"
    tracked.write_text("export const value = 1;\n")
    (tmp_path / "generated.ts").write_text("generated\n")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "add", "-f", "tracked.ts")
    _commit(tmp_path)
    assert tracked in _scan_repo_files(tmp_path)
    assert tmp_path / "generated.ts" not in _scan_repo_files(tmp_path)


def test_git_paths_are_nul_delimited_and_symlinks_are_not_read(tmp_path):
    _git(tmp_path, "init")
    source = tmp_path / "space and\nnewline.py"
    source.write_text("value = 1\n")
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    outside.write_text("private = 1\n")
    (tmp_path / "escape.py").symlink_to(outside)
    (tmp_path / "alias.py").symlink_to(source)
    _git(tmp_path, "add", ".")
    _commit(tmp_path)
    assert _scan_repo_files(tmp_path) == [source]


def test_snapshot_walk_skips_file_and_directory_symlinks(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("value = 1\n")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    secret = outside / "private.py"
    secret.write_text("private = 1\n")
    (tmp_path / "escape.py").symlink_to(secret)
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    assert _scan_repo_files(tmp_path) == [source]


def test_broken_git_metadata_never_falls_back_to_snapshot(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(RuntimeError, match="Git checkout metadata"):
        _scan_repo_files(tmp_path)


def test_failed_git_listing_never_indexes_untracked_files(tmp_path, monkeypatch):
    responses = iter([
        SimpleNamespace(returncode=0, stdout=b"true\n"),
        SimpleNamespace(returncode=1, stdout=b""),
    ])
    monkeypatch.setattr("brain.indexers.file_indexer.subprocess.run", lambda *a, **kw: next(responses))
    with pytest.raises(RuntimeError, match="tracked Git source"):
        _scan_repo_files(tmp_path)


def test_snapshot_without_git_binary_and_checkout_requires_it(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    source = tmp_path / "service.py"
    source.write_text("value = 1\n")
    monkeypatch.setattr("brain.indexers.file_indexer.subprocess.run", missing)
    assert _scan_repo_files(tmp_path) == [source]
    (tmp_path / ".git").mkdir()
    with pytest.raises(RuntimeError, match="Git is required"):
        _scan_repo_files(tmp_path)
