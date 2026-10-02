"""Tests for the Change Laboratory — Workstream D.

Covers: workspace creation (worktree and copy), containment, patch ingestion
policy, patch application with rollback, declarative validation profiles,
process supervision, post-patch analysis, cleanup, quarantine, orphan recovery,
CLI, and API wiring.

Workspaces are created from real Git repositories, and validation runs real
child processes — the failures this workstream exists to catch only appear
against real processes and a real filesystem.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from brain.lab.engine import (
    LabSecurityError,
    PatchApplier,
    PatchValidator,
    PostPatchAnalyzer,
    ValidationRunner,
    WorkspaceGuard,
    WorkspaceManager,
)
from brain.lab.laboratory import ChangeLaboratory, ProfileNotFoundError
from brain.lab.models import (
    WORKSPACE_MARKER,
    CommandDef,
    PatchRecord,
    ValidationProfile,
    WorkspaceState,
)
from brain.lab.sandbox import network_isolation_status, resolve_sandbox_backend
from tests.support import git_fixtures as gf


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path) -> Path:
    return gf.make_python_repo(tmp_path, "lab-repo")


@pytest.fixture
def manager(tmp_path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "workspaces", tmp_path / "lab")


UTIL_PATCH = """\
diff --git a/pkg/util.py b/pkg/util.py
--- a/pkg/util.py
+++ b/pkg/util.py
@@ -1,2 +1,2 @@
 def helper(value):
-    return value * 2
+    return value * 3
"""


def _patch(content: str = UTIL_PATCH, **kwargs) -> PatchRecord:
    params = {"patch_id": "patch-test", "patch_content": content}
    params.update(kwargs)
    return PatchRecord(**params)


# ──────────────────────────────────────────────
# Phase D2 — workspace lifecycle
# ──────────────────────────────────────────────


def test_workspace_uses_git_worktree_and_is_marked(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)

    assert record.creation_method == "git_worktree"
    assert record.state == WorkspaceState.READY.value
    path = Path(record.workspace_root)
    assert path.is_dir()
    assert (path / "pkg" / "util.py").is_file()

    marker = json.loads((path / WORKSPACE_MARKER).read_text(encoding="utf-8"))
    assert marker["workspace_id"] == record.workspace_id
    assert marker["managed_by"] == "project-brain-change-laboratory"


def test_workspace_falls_back_to_copy_for_non_git_source(manager, tmp_path):
    plain = tmp_path / "plain-source"
    (plain / "pkg").mkdir(parents=True)
    (plain / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    record = manager.create_workspace("plain", "HEAD", plain)

    assert record.creation_method == "copy"
    assert (Path(record.workspace_root) / "pkg" / "mod.py").is_file()
    assert not (Path(record.workspace_root) / ".git").exists()


def test_workspace_copy_excludes_runtime_state(manager, repo):
    (repo / ".brain").mkdir(exist_ok=True)
    (repo / ".brain" / "state.json").write_text("{}", encoding="utf-8")
    (repo / "__pycache__").mkdir(exist_ok=True)
    (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    # Force the copy path by pointing at a directory whose worktree add fails.
    plain = repo.parent / "copy-source"
    plain.mkdir()
    (plain / ".brain").mkdir()
    (plain / ".brain" / "state.json").write_text("{}", encoding="utf-8")
    (plain / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")

    record = manager.create_workspace("copy", "HEAD", plain)
    root = Path(record.workspace_root)

    assert (root / "keep.py").is_file()
    assert not (root / ".brain").exists()


def test_workspace_isolation_source_untouched(manager, repo):
    original = (repo / "pkg" / "util.py").read_text(encoding="utf-8")
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)

    workspace_file = Path(record.workspace_root) / "pkg" / "util.py"
    workspace_file.write_text("def helper(v):\n    return 0\n", encoding="utf-8")

    assert (repo / "pkg" / "util.py").read_text(encoding="utf-8") == original


def test_workspace_expiry_marks_records(tmp_path, repo):
    manager = WorkspaceManager(tmp_path / "ws", tmp_path / "lab", ttl_seconds=60)
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)

    assert manager.expire_due() == []
    expired = manager.expire_due(now=time.time() + 3600)
    assert expired == [record.workspace_id]
    assert manager.get_workspace(record.workspace_id).state == WorkspaceState.EXPIRED.value


# ──────────────────────────────────────────────
# Containment
# ──────────────────────────────────────────────


def test_guard_rejects_path_outside_root(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    with pytest.raises(LabSecurityError):
        WorkspaceGuard.resolve_inside(root, tmp_path / "elsewhere" / "file.txt")


def test_guard_resolves_symlinks_before_containment(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    with pytest.raises(LabSecurityError):
        WorkspaceGuard.resolve_inside(root, link / "secret.txt")


def test_cleanup_refuses_directory_without_ownership_marker(manager, repo, tmp_path):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    path = Path(record.workspace_root)
    (path / WORKSPACE_MARKER).unlink()

    report = manager.clean_workspace(record.workspace_id)

    assert report.removed is False
    assert report.quarantined is True
    assert path.is_dir(), "an unowned directory must never be deleted"
    assert manager.get_workspace(record.workspace_id).state == WorkspaceState.QUARANTINED.value


def test_cleanup_refuses_path_outside_managed_root(manager, repo, tmp_path):
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "data.txt").write_text("do not delete", encoding="utf-8")

    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    # Simulate a tampered record pointing outside the managed root.
    manager.update_state(
        record.workspace_id, WorkspaceState.READY, workspace_root=str(victim)
    )

    report = manager.clean_workspace(record.workspace_id)

    assert report.quarantined is True
    assert (victim / "data.txt").is_file()


def test_cleanup_removes_owned_workspace(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    path = Path(record.workspace_root)

    report = manager.clean_workspace(record.workspace_id, reason="test")

    assert report.removed is True
    assert not path.exists()
    assert manager.get_workspace(record.workspace_id).state == WorkspaceState.CLEANED.value


def test_recover_orphans_reports_and_removes_marked_directories(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    path = Path(record.workspace_root)

    # An orphan directory that carries a valid marker but has no record.
    orphan = manager.managed_root / "ws-orphaned01"
    orphan.mkdir()
    (orphan / WORKSPACE_MARKER).write_text(
        json.dumps({"workspace_id": "ws-orphaned01"}), encoding="utf-8"
    )
    # A stray directory with no marker at all: reported, never deleted.
    stray = manager.managed_root / "not-a-workspace"
    stray.mkdir()
    (stray / "data.txt").write_text("keep", encoding="utf-8")

    report = manager.recover_orphans()

    assert str(orphan) in report.orphan_directories
    assert not orphan.exists()
    assert str(stray) in report.orphan_directories
    assert (stray / "data.txt").is_file()
    assert path.is_dir()


def test_recover_orphans_marks_vanished_workspaces(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    import shutil

    shutil.rmtree(Path(record.workspace_root), ignore_errors=True)

    report = manager.recover_orphans()

    assert record.workspace_id in report.missing_workspaces
    assert manager.get_workspace(record.workspace_id).state == WorkspaceState.CLEANED.value


# ──────────────────────────────────────────────
# Phase D3 — patch ingestion policy
# ──────────────────────────────────────────────


def test_patch_target_paths_parsed():
    assert PatchValidator.target_paths(UTIL_PATCH) == ["pkg/util.py"]


@pytest.mark.parametrize(
    "content,expected_fragment",
    [
        ("", "empty"),
        (
            "diff --git a/../outside.py b/../outside.py\n--- a/../outside.py\n+++ b/../outside.py\n",
            "traversal",
        ),
        (
            "diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n",
            ".git",
        ),
        (
            "diff --git a//etc/passwd b//etc/passwd\n--- a//etc/passwd\n+++ b//etc/passwd\n",
            "absolute",
        ),
        (
            "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n",
            "symlink",
        ),
    ],
)
def test_patch_validator_rejects_dangerous_patches(content, expected_fragment):
    reasons = PatchValidator.validate(_patch(content))
    assert any(expected_fragment in r.lower() for r in reasons), reasons


def test_patch_validator_rejects_oversized_patch():
    reasons = PatchValidator.validate(_patch(UTIL_PATCH, max_bytes=10))
    assert any("max bytes" in r for r in reasons)


def test_patch_validator_rejects_too_many_files():
    content = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n" for i in range(5)
    )
    reasons = PatchValidator.validate(_patch(content, max_files=2))
    assert any("too many files" in r.lower() for r in reasons)


def test_patch_validator_enforces_declared_scope():
    reasons = PatchValidator.validate(_patch(allowed_paths=["docs"]))
    assert any("outside declared scope" in r for r in reasons)

    assert PatchValidator.validate(_patch(allowed_paths=["pkg"])) == []


def test_patch_validator_rejects_marker_target():
    content = (
        f"diff --git a/{WORKSPACE_MARKER} b/{WORKSPACE_MARKER}\n"
        f"--- a/{WORKSPACE_MARKER}\n+++ b/{WORKSPACE_MARKER}\n"
    )
    reasons = PatchValidator.validate(_patch(content))
    assert any("ownership marker" in r for r in reasons)


def test_patch_validator_detects_stale_base_state(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)

    stale = _patch(expected_file_hashes={"pkg/util.py": "0" * 64})
    reasons = PatchValidator.validate(stale, workspace)
    assert any("stale" in r for r in reasons)

    import hashlib

    actual = hashlib.sha256((workspace / "pkg" / "util.py").read_bytes()).hexdigest()
    fresh = _patch(expected_file_hashes={"pkg/util.py": actual})
    assert PatchValidator.validate(fresh, workspace) == []


# ──────────────────────────────────────────────
# Phase D4 — patch application
# ──────────────────────────────────────────────


def test_patch_application_changes_only_the_workspace(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)
    source_before = (repo / "pkg" / "util.py").read_text(encoding="utf-8")

    result = PatchApplier.apply(workspace, _patch(), record.workspace_id)

    assert result.success is True
    assert result.affected_files == ["pkg/util.py"]
    assert result.before_hashes["pkg/util.py"] != result.after_hashes["pkg/util.py"]
    assert "value * 3" in (workspace / "pkg" / "util.py").read_text(encoding="utf-8")
    assert (repo / "pkg" / "util.py").read_text(encoding="utf-8") == source_before


def test_patch_application_leaves_no_patch_file_in_workspace(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)

    PatchApplier.apply(workspace, _patch(), record.workspace_id)

    assert list(workspace.glob("*.diff")) == []


def test_failed_patch_leaves_workspace_unchanged(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)
    before = (workspace / "pkg" / "util.py").read_text(encoding="utf-8")

    conflicting = UTIL_PATCH.replace("    return value * 2", "    return value * 999")
    result = PatchApplier.apply(workspace, _patch(conflicting), record.workspace_id)

    assert result.success is False
    assert result.rejected_reasons
    assert (workspace / "pkg" / "util.py").read_text(encoding="utf-8") == before


def test_patch_application_refuses_escaping_targets(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)

    escaping = (
        "diff --git a/../escape.py b/../escape.py\n"
        "--- a/../escape.py\n+++ b/../escape.py\n"
        "@@ -0,0 +1 @@\n+X = 1\n"
    )
    result = PatchApplier.apply(workspace, _patch(escaping), record.workspace_id)

    assert result.success is False
    assert any("outside the managed workspace" in r for r in result.rejected_reasons)
    assert not (workspace.parent / "escape.py").exists()


def test_rollback_restores_snapshot(tmp_path):
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    target = root / "pkg" / "a.py"
    target.write_text("original\n", encoding="utf-8")

    snapshot = PatchApplier._snapshot(root, [target, root / "pkg" / "new.py"])
    target.write_text("corrupted\n", encoding="utf-8")
    (root / "pkg" / "new.py").write_text("should not survive\n", encoding="utf-8")

    assert PatchApplier._rollback(root, snapshot) is True
    assert target.read_text(encoding="utf-8") == "original\n"
    assert not (root / "pkg" / "new.py").exists()


# ──────────────────────────────────────────────
# Phase D5 — validation profiles
# ──────────────────────────────────────────────


def _profile(argv, **kwargs) -> ValidationProfile:
    params = {
        "profile_name": "test",
        "timeout_seconds": 60,
        "commands": [CommandDef(argv=argv)],
    }
    params.update(kwargs)
    return ValidationProfile(**params)


def test_profile_rejects_non_allowlisted_executable():
    issues = ValidationRunner.validate_profile(_profile(["bash", "-c", "echo hi"]))
    assert any("not in allowlist" in i for i in issues)


def test_profile_rejects_repository_provided_executable_path():
    issues = ValidationRunner.validate_profile(_profile(["./tools/python", "-V"]))
    assert any("executable path not allowed" in i for i in issues)

    absolute = str(Path(sys.executable))
    issues = ValidationRunner.validate_profile(_profile([absolute, "-V"]))
    assert any("executable path not allowed" in i for i in issues)


def test_profile_rejects_secret_environment_and_oversized_timeout():
    issues = ValidationRunner.validate_profile(
        _profile(["python", "-V"], environment={"PROD_API_KEY": "x"}, timeout_seconds=99999)
    )
    assert any("secret-like" in i for i in issues)
    assert any("Timeout too large" in i for i in issues)


def test_profile_rejects_empty_and_unsupported_schema():
    empty = ValidationRunner.validate_profile(ValidationProfile(profile_name="empty", commands=[]))
    assert any("no commands" in i.lower() for i in empty)

    issues = ValidationRunner.validate_profile(_profile(["python", "-V"], schema_version=99))
    assert any("schema version" in i.lower() for i in issues)


def test_environment_allowlist_excludes_secrets(monkeypatch):
    monkeypatch.setenv("BRAIN_API_KEY", "super-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    env = ValidationRunner.build_environment({"CI": "1", "MY_TOKEN": "leak"})

    assert "BRAIN_API_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "MY_TOKEN" not in env
    assert env["CI"] == "1"
    assert "PATH" in env
    assert env["BRAIN_LAB_NETWORK_POLICY"] == "deny"


# ──────────────────────────────────────────────
# Phase D6 — supervised execution
# ──────────────────────────────────────────────


def test_validation_runs_and_reports_actual_isolation_mechanism(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)

    report = ValidationRunner.run_profile(
        workspace,
        _profile(["python", "-c", "import pkg.util; print(pkg.util.helper(2))"]),
        workspace_id=record.workspace_id,
    )

    assert report.passed is True
    # The report must name the mechanism actually used: enforced:<backend>
    # when a sandbox wrapped the command, "unverified" only when none did.
    assert report.network_isolation == network_isolation_status(resolve_sandbox_backend())
    assert report.network_isolation in {"unverified", "enforced:docker", "enforced:unshare"}
    assert report.results[0]["exit_code"] == 0
    assert "4" in report.results[0]["stdout_snippet"]


def test_validation_stops_at_first_required_failure(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    profile = ValidationProfile(
        profile_name="two-step",
        timeout_seconds=60,
        commands=[
            CommandDef(argv=["python", "-c", "raise SystemExit(3)"], required=True),
            CommandDef(argv=["python", "-c", "print('never')"], required=True),
        ],
    )

    report = ValidationRunner.run_profile(Path(record.workspace_root), profile)

    assert report.passed is False
    assert len(report.results) == 1
    assert report.results[0]["exit_code"] == 3


def test_validation_timeout_terminates_process(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    profile = _profile(["python", "-c", "import time; time.sleep(30)"], timeout_seconds=2)

    started = time.monotonic()
    report = ValidationRunner.run_profile(Path(record.workspace_root), profile)
    elapsed = time.monotonic() - started

    assert report.passed is False
    assert report.results[0]["timed_out"] is True
    assert elapsed < 25, "the supervisor must not wait for the child to finish"


def test_validation_cancellation(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    profile = _profile(["python", "-c", "import time; time.sleep(30)"], timeout_seconds=60)
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()

    started = time.monotonic()
    report = ValidationRunner.run_profile(
        Path(record.workspace_root), profile, cancel_event=cancel
    )
    elapsed = time.monotonic() - started

    assert elapsed < 25
    assert report.passed is False


def test_validation_output_is_redacted(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    profile = _profile(["python", "-c", "print('API_KEY=sk-live-abcdef123456')"])

    report = ValidationRunner.run_profile(Path(record.workspace_root), profile)

    assert "sk-live-abcdef123456" not in report.results[0]["stdout_snippet"]
    assert "<REDACTED>" in report.results[0]["stdout_snippet"]


def test_rejected_profile_never_executes(manager, repo, tmp_path):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    sentinel = tmp_path / "executed.txt"
    profile = _profile(["bash", "-c", f"touch {sentinel}"])

    report = ValidationRunner.run_profile(Path(record.workspace_root), profile)

    assert report.rejected_reasons
    assert report.results == []
    assert not sentinel.exists()


# ──────────────────────────────────────────────
# Phase D7 — post-patch analysis
# ──────────────────────────────────────────────


def test_post_patch_analysis_reports_changed_files(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)
    result = PatchApplier.apply(workspace, _patch(), record.workspace_id)

    report = PostPatchAnalyzer.analyze(workspace, result, declared_paths=["pkg/util.py"])

    assert report.analysis_method == "git_status"
    assert "pkg/util.py" in report.changed_files
    assert report.within_declared_scope is True
    assert WORKSPACE_MARKER not in report.changed_files + report.added_files


def test_post_patch_analysis_flags_out_of_scope_changes(manager, repo):
    record = manager.create_workspace("lab-repo", gf.head(repo), repo)
    workspace = Path(record.workspace_root)
    result = PatchApplier.apply(workspace, _patch(), record.workspace_id)
    (workspace / "pkg" / "sneaky.py").write_text("BACKDOOR = 1\n", encoding="utf-8")

    report = PostPatchAnalyzer.analyze(workspace, result, declared_paths=["pkg/util.py"])

    assert "pkg/sneaky.py" in report.unexpected_files
    assert report.within_declared_scope is False


def test_post_patch_analysis_falls_back_to_hashes(tmp_path):
    from brain.lab.models import PatchApplyResult

    workspace = tmp_path / "copied-workspace"
    workspace.mkdir()
    result = PatchApplyResult(
        workspace_id="ws-x",
        before_hashes={"a.py": "aaa", "gone.py": "bbb"},
        after_hashes={"a.py": "ccc", "new.py": "ddd"},
    )

    report = PostPatchAnalyzer.analyze(workspace, result)

    assert report.analysis_method == "hash_comparison"
    assert report.changed_files == ["a.py"]
    assert report.deleted_files == ["gone.py"]
    assert report.added_files == ["new.py"]


# ──────────────────────────────────────────────
# Phase D9 — laboratory sessions
# ──────────────────────────────────────────────


def test_session_passes_and_disposes_workspace(tmp_path, repo):
    lab = ChangeLaboratory(tmp_path / "brain")
    session = lab.run_session(
        repository_id="lab-repo",
        repo_path=repo,
        base_revision=gf.head(repo),
        patch=_patch(),
        profile_name="python-compile",
    )

    assert session["outcome"] == "passed"
    assert session["apply"]["success"] is True
    assert session["validation"]["passed"] is True
    assert session["cleanup"]["removed"] is True
    assert not Path(
        lab.manager.get_workspace(session["workspace_id"]).workspace_root
    ).exists()
    assert lab.get_session(session["session_id"])["outcome"] == "passed"


def test_session_reports_failing_validation(tmp_path, repo):
    lab = ChangeLaboratory(tmp_path / "brain")
    broken = """\
diff --git a/pkg/util.py b/pkg/util.py
--- a/pkg/util.py
+++ b/pkg/util.py
@@ -1,2 +1,2 @@
 def helper(value):
-    return value * 2
+    return value *
"""
    session = lab.run_session(
        repository_id="lab-repo",
        repo_path=repo,
        base_revision=gf.head(repo),
        patch=_patch(broken),
        profile_name="python-compile",
    )

    assert session["outcome"] == "failed"
    assert session["validation"]["passed"] is False


def test_session_rejects_inadmissible_patch_without_creating_workspace(tmp_path, repo):
    lab = ChangeLaboratory(tmp_path / "brain")
    session = lab.run_session(
        repository_id="lab-repo",
        repo_path=repo,
        base_revision=gf.head(repo),
        patch=_patch("diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n"),
    )

    assert session["outcome"] == "rejected"
    assert "workspace_id" not in session
    assert lab.manager.list_workspaces() == []


def test_session_rejects_profile_not_allowed_for_repository(tmp_path, repo):
    lab = ChangeLaboratory(tmp_path / "brain")
    session = lab.run_session(
        repository_id="lab-repo",
        repo_path=repo,
        base_revision=gf.head(repo),
        patch=_patch(),
        profile_name="python-standard",
        allowed_profiles=["python-compile"],
    )

    assert session["outcome"] == "rejected"
    assert "not allowed" in session["rejected_reasons"][0]


def test_unknown_profile_is_reported(tmp_path):
    lab = ChangeLaboratory(tmp_path / "brain")
    with pytest.raises(ProfileNotFoundError):
        lab.load_profile("does-not-exist")
    assert "python-compile" in lab.list_profiles()


def test_on_disk_profile_overrides_builtin(tmp_path):
    lab = ChangeLaboratory(tmp_path / "brain")
    lab.profiles_dir.mkdir(parents=True, exist_ok=True)
    (lab.profiles_dir / "python-compile.json").write_text(
        json.dumps(
            {
                "profile_name": "python-compile",
                "timeout_seconds": 30,
                "commands": [{"argv": ["python", "-V"], "required": True}],
            }
        ),
        encoding="utf-8",
    )

    profile = lab.load_profile("python-compile")
    assert profile.timeout_seconds == 30
    assert profile.commands[0].argv == ["python", "-V"]


# ──────────────────────────────────────────────
# CLI and API
# ──────────────────────────────────────────────


def _register(registry_dir: Path, repo: Path, capabilities=None, profiles=None):
    from brain.workspace.capabilities import CapabilitiesManager
    from brain.workspace.models import RepositoryRecord, TrustLevel, _generate_repository_id
    from brain.workspace.path_sandbox import PathSandbox
    from brain.workspace.registry_store import RegistryStore

    canonical = str(repo.resolve())
    record = RepositoryRecord(
        repository_id=_generate_repository_id(canonical),
        display_name=repo.name,
        canonical_root=canonical,
        normalized_root_identity=PathSandbox.get_normalized_identity(repo.resolve()),
        repository_type="git",
        default_branch="main",
        current_revision=gf.head(repo),
        trust_level=TrustLevel.TRUSTED_INTERNAL.value,
        organization="brain-tests",
        owner="platform",
        capabilities=(
            capabilities
            if capabilities is not None
            else CapabilitiesManager.get_defaults(TrustLevel.TRUSTED_INTERNAL)
        ),
        allowed_validation_profiles=profiles or ["python-compile"],
    )
    return RegistryStore(registry_dir).register(record, actor="test", source="test")


def test_cli_validate_and_profiles(tmp_path, repo, capsys):
    from brain.lab import cli as lab_cli

    registry_dir = tmp_path / "registry"
    record = _register(registry_dir, repo)
    patch_file = tmp_path / "candidate.diff"
    patch_file.write_text(UTIL_PATCH, encoding="utf-8")

    lab_cli.main(["--brain-dir", str(tmp_path / "brain"), "profiles"])
    assert "python-compile" in json.loads(capsys.readouterr().out)["profiles"]

    lab_cli.main(
        [
            "--brain-dir",
            str(tmp_path / "brain"),
            "--registry-dir",
            str(registry_dir),
            "validate",
            "--repository",
            record.repository_id,
            "--patch-file",
            str(patch_file),
            "--profile",
            "python-compile",
            "--allow-path",
            "pkg",
        ]
    )
    session = json.loads(capsys.readouterr().out)
    assert session["outcome"] == "passed"
    assert session["cleanup"]["removed"] is True


def test_cli_validate_requires_capability(tmp_path, repo, capsys):
    from brain.lab import cli as lab_cli

    registry_dir = tmp_path / "registry"
    record = _register(registry_dir, repo, capabilities=["read_source"])
    patch_file = tmp_path / "candidate.diff"
    patch_file.write_text(UTIL_PATCH, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        lab_cli.main(
            [
                "--brain-dir",
                str(tmp_path / "brain"),
                "--registry-dir",
                str(registry_dir),
                "validate",
                "--repository",
                record.repository_id,
                "--patch-file",
                str(patch_file),
            ]
        )
    assert exc.value.code == 1
    assert "lacks capability" in capsys.readouterr().err


def test_cli_recover_and_clean(tmp_path, repo, capsys):
    from brain.lab import cli as lab_cli

    lab = ChangeLaboratory(tmp_path / "brain")
    record = lab.manager.create_workspace("lab-repo", gf.head(repo), repo)

    lab_cli.main(
        ["--brain-dir", str(tmp_path / "brain"), "clean", "--workspace", record.workspace_id]
    )
    assert json.loads(capsys.readouterr().out)["removed"] is True

    lab_cli.main(["--brain-dir", str(tmp_path / "brain"), "recover"])
    payload = json.loads(capsys.readouterr().out)
    assert "orphan_directories" in payload
    assert "expired_workspaces" in payload


def test_lab_router_is_mounted():
    from apps.api.main import app

    paths = set(app.openapi()["paths"])
    assert "/lab/profiles" in paths
    assert "/lab/validate" in paths
    assert "/lab/workspaces/{workspace_id}" in paths
    assert "/lab/workspaces/{workspace_id}/cleanup" in paths
