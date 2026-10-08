import json
import subprocess
from pathlib import Path
from unittest.mock import patch
from brain.indexers.repo_indexer import RepoIndexer, classify_file, get_git_commit_hash, should_ignore_dir

def test_should_ignore_dir():
    assert should_ignore_dir("node_modules") is True
    assert should_ignore_dir(".git") is True
    assert should_ignore_dir("src") is False
    assert should_ignore_dir("build") is True

def test_classify_file():
    repo_path = Path("/mock/repo")
    assert classify_file(Path("/mock/repo/src/main.py"), repo_path) == "source_code"
    assert classify_file(Path("/mock/repo/package.json"), repo_path) == "config"
    assert classify_file(Path("/mock/repo/tests/test_app.py"), repo_path) == "test"
    assert classify_file(Path("/mock/repo/.github/workflows/ci.yml"), repo_path) == "ci_config"
    assert classify_file(Path("/mock/repo/docs/readme.md"), repo_path) == "documentation"
    assert classify_file(Path("/mock/repo/openapi.yaml"), repo_path) == "api_spec"


def test_evaluation_tools_keep_a_distinct_search_role():
    repo_path = Path("/mock/repo")
    for directory in ("eval", "benchmarks", "scripts", "bin"):
        assert classify_file(repo_path / directory / "profile.py", repo_path) == "script"
        assert classify_file(repo_path / directory / "settings.yaml", repo_path) == "config"
        assert classify_file(repo_path / directory / "README.md", repo_path) == "documentation"
        assert classify_file(repo_path / directory / "test_profile.py", repo_path) == "test"
    assert classify_file(repo_path / "src" / "evaluator.py", repo_path) == "source_code"

def test_get_git_commit_hash_trusts_only_resolved_repo(tmp_path):
    repo_dir = tmp_path / "snapshot"
    repo_dir.mkdir()
    commit = "a" * 40
    completed = [
        subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=f"{commit}\n", stderr=""),
    ]

    with patch("brain.indexers.repo_indexer.subprocess.run", side_effect=completed) as mock_run:
        assert get_git_commit_hash(repo_dir) == commit

    safe_arg = f"safe.directory={repo_dir.resolve()}"
    assert mock_run.call_args_list[0].args[0] == [
        "git", "-c", safe_arg, "rev-parse", "--is-inside-work-tree"
    ]
    assert mock_run.call_args_list[1].args[0] == ["git", "-c", safe_arg, "rev-parse", "HEAD"]
    assert all(call.kwargs["cwd"] == str(repo_dir.resolve()) for call in mock_run.call_args_list)


def test_get_git_commit_hash_uses_snapshot_manifest(tmp_path):
    repo_dir = tmp_path / "snapshot"
    repo_dir.mkdir()
    manifest = {
        "schema_version": 1,
        "revision": "snapshot:abc:" + "d" * 64,
        "content_digest": "d" * 64,
    }
    (repo_dir / ".brain-source-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    with patch(
        "brain.indexers.repo_indexer.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="not git"),
    ):
        assert get_git_commit_hash(repo_dir) == manifest["revision"]

def test_indexer_scan_temp_directory(tmp_path):
    # Setup mock repository files
    repo_dir = tmp_path / "mock_repo"
    repo_dir.mkdir()

    # Create source files
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("import os\nprint('Hello')", encoding="utf-8")
    (src_dir / "utils.js").write_text("console.log('utils'); // https://figma.com/file/12345", encoding="utf-8")

    # Create test files
    test_dir = repo_dir / "tests"
    test_dir.mkdir()
    (test_dir / "test_app.py").write_text("def test_app(): assert True", encoding="utf-8")

    # Create config files
    (repo_dir / "pyproject.toml").write_text("[project]\nname = 'mock'", encoding="utf-8")
    (repo_dir / "requirements.txt").write_text("fastapi==0.100.0\npytest", encoding="utf-8")

    # Create docs
    (repo_dir / "README.md").write_text("# Mock Repo", encoding="utf-8")

    # Create ignored folder
    node_modules = repo_dir / "node_modules"
    node_modules.mkdir()
    (node_modules / "some_pkg.js").write_text("console.log()", encoding="utf-8")

    # Run scan
    report_file = tmp_path / "test-audit.md"
    indexer = RepoIndexer(repo_dir)
    data = indexer.scan(repo_path=repo_dir, report_path=report_file)

    assert data["total_source_files"] == 3  # app.py, utils.js, test_app.py
    assert "Python" in data["languages"]
    assert "JavaScript" in data["languages"]
    assert "Pytest" in data["test_suites"]
    assert "Pip" in data["build_systems"]

    # Check if node_modules was ignored
    assert "node_modules" in data["ignored_directories"]

    # Check if figma URL was parsed
    assert len(data["figma_urls"]) == 1

    # Check report file generation
    assert report_file.exists()
    report_content = report_file.read_text(encoding="utf-8")
    assert "# Repository Initial Audit Report" in report_content
    assert "## Detected Languages" in report_content
    assert "Python" in report_content
    assert "JavaScript" in report_content
