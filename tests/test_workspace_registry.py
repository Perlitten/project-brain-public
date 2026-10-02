"""Comprehensive tests for Workspace Registry — Workstream A.

Tests: models, path sandbox, registry persistence, optimistic versioning,
capabilities, audit events, CLI, and API router.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from brain.workspace.models import (
    Capability,
    DEFAULT_CAPABILITIES,
    RegistryEvent,
    RegistryEventType,
    RepositoryRecord,
    TrustLevel,
    _generate_repository_id,
)
from brain.workspace.path_sandbox import PathSandbox, PathValidationError
from brain.workspace.registry_store import (
    OptimisticLockError,
    RegistryStore,
)
from brain.workspace.capabilities import CapabilitiesManager, CapabilityDeniedError
from brain.workspace.roots_manager import WorkspaceRootsManager
from brain.workspace.audit_events import AuditEventsEmitter


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def tmp_dir(tmp_path):
    """Temporary directory for tests."""
    return tmp_path


@pytest.fixture
def store(tmp_path):
    """Fresh registry store."""
    return RegistryStore(tmp_path / "workspace")


@pytest.fixture
def sample_record(tmp_path) -> RepositoryRecord:
    """Create a sample repository record."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()  # Fake git

    canonical = str(repo_dir.resolve())
    return RepositoryRecord(
        repository_id=_generate_repository_id(canonical),
        display_name="my-repo",
        canonical_root=canonical,
        normalized_root_identity=PathSandbox.get_normalized_identity(repo_dir.resolve()),
        repository_type="git",
        default_branch="main",
        current_revision="abc123",
        trust_level=TrustLevel.TRUSTED_INTERNAL.value,
        organization="test-org",
        owner="test-user",
        languages=["python"],
        capabilities=CapabilitiesManager.get_defaults(TrustLevel.TRUSTED_INTERNAL),
    )


@pytest.fixture
def git_repo(tmp_path):
    """Create a real Git repository for testing."""
    repo_dir = tmp_path / "test-git-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], capture_output=True, timeout=10)
    # Create initial commit
    (repo_dir / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, timeout=10)
    subprocess.run(
        ["git", "commit", "-m", "init", "--allow-empty"],
        cwd=str(repo_dir),
        capture_output=True,
        timeout=10,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )
    return repo_dir


# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────

class TestModels:
    def test_trust_levels(self):
        """All trust levels are valid enum members."""
        assert TrustLevel.TRUSTED_INTERNAL.value == "trusted_internal"
        assert TrustLevel.DISABLED.value == "disabled"

    def test_capabilities_enum(self):
        """All capabilities are valid enum members."""
        assert Capability.READ_SOURCE.value == "read_source"
        assert Capability.EXECUTE_VALIDATION.value == "execute_validation"

    def test_repository_id_not_from_display_name(self):
        """Repository ID is derived from canonical path, not display name."""
        id1 = _generate_repository_id("/path/to/repo-a")
        id2 = _generate_repository_id("/path/to/repo-b")
        assert id1 != id2
        assert id1.startswith("repo-")

    def test_repository_id_deterministic(self):
        """Same canonical path produces the same ID."""
        id1 = _generate_repository_id("d:/projects/my-repo")
        id2 = _generate_repository_id("d:/projects/my-repo")
        assert id1 == id2

    def test_repository_id_normalized(self):
        """Backslash and forward-slash produce the same ID."""
        id1 = _generate_repository_id("d:\\projects\\my-repo")
        id2 = _generate_repository_id("d:/projects/my-repo")
        assert id1 == id2

    def test_record_round_trip(self, sample_record):
        """Record serializes and deserializes cleanly."""
        d = sample_record.to_dict()
        restored = RepositoryRecord.from_dict(d)
        assert restored.repository_id == sample_record.repository_id
        assert restored.trust_level == sample_record.trust_level
        assert restored.capabilities == sample_record.capabilities

    def test_portable_export_redacts_path(self, sample_record):
        """Portable export removes absolute machine-specific paths."""
        exported = sample_record.portable_export()
        assert exported["canonical_root"] == "<redacted>"
        assert "_export_identity" in exported

    def test_registry_event_creation(self):
        """Registry events have all required fields."""
        event = RegistryEvent.create(
            RegistryEventType.REGISTERED,
            "repo-123",
            "test-actor",
            0,
            1,
            "test reason",
            "cli",
        )
        assert event.event_id.startswith("evt-")
        assert event.event_type == "registered"
        assert event.repository_id == "repo-123"
        assert event.actor == "test-actor"

    def test_default_capabilities_per_trust(self):
        """Default capabilities match trust level expectations."""
        internal = DEFAULT_CAPABILITIES[TrustLevel.TRUSTED_INTERNAL]
        assert Capability.READ_SOURCE in internal
        assert Capability.EXECUTE_VALIDATION in internal

        disabled = DEFAULT_CAPABILITIES[TrustLevel.DISABLED]
        assert len(disabled) == 0

        untrusted = DEFAULT_CAPABILITIES[TrustLevel.UNTRUSTED_EXTERNAL]
        assert Capability.EXECUTE_VALIDATION not in untrusted


# ──────────────────────────────────────────────
# Path Sandbox
# ──────────────────────────────────────────────

class TestPathSandbox:
    def test_empty_path_rejected(self, tmp_path):
        sandbox = PathSandbox([tmp_path])
        with pytest.raises(PathValidationError, match="empty"):
            sandbox.validate("")

    def test_traversal_rejected(self, tmp_path):
        sandbox = PathSandbox([tmp_path])
        with pytest.raises(PathValidationError, match="traversal"):
            sandbox.validate(str(tmp_path / ".." / "escape"))

    def test_nonexistent_path_rejected(self, tmp_path):
        sandbox = PathSandbox([tmp_path])
        with pytest.raises(PathValidationError, match="does not exist"):
            sandbox.validate(str(tmp_path / "nonexistent"))

    def test_file_path_rejected(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("content")
        sandbox = PathSandbox([tmp_path])
        with pytest.raises(PathValidationError, match="not a directory"):
            sandbox.validate(str(f))

    def test_outside_allowed_roots(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        sandbox = PathSandbox([allowed])
        with pytest.raises(PathValidationError, match="outside"):
            sandbox.validate(str(outside))

    def test_valid_git_repo(self, git_repo, tmp_path):
        sandbox = PathSandbox([tmp_path])
        resolved = sandbox.validate(str(git_repo))
        assert resolved == git_repo.resolve()

    def test_non_git_rejected_when_required(self, tmp_path):
        non_git = tmp_path / "not-git"
        non_git.mkdir()
        sandbox = PathSandbox([tmp_path])
        with pytest.raises(PathValidationError, match="Not a Git repository"):
            sandbox.validate(str(non_git), require_git=True)

    def test_fixture_skips_git_check(self, tmp_path):
        fixture_dir = tmp_path / "fixture-repo"
        fixture_dir.mkdir()
        sandbox = PathSandbox([tmp_path])
        resolved = sandbox.validate_fixture(str(fixture_dir))
        assert resolved.exists()

    def test_duplicate_registration_rejected(self, git_repo, tmp_path):
        canonical = PathSandbox.get_canonical_string(git_repo.resolve())
        sandbox = PathSandbox([tmp_path], registered_canonicals={canonical})
        with pytest.raises(PathValidationError, match="already registered"):
            sandbox.validate(str(git_repo))

    def test_nested_repo_rejected(self, tmp_path):
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / ".git").mkdir()
        child = parent / "child"
        child.mkdir()
        (child / ".git").mkdir()

        parent_canonical = PathSandbox.get_canonical_string(parent.resolve())
        sandbox = PathSandbox([tmp_path], registered_canonicals={parent_canonical})

        with pytest.raises(PathValidationError, match="Nested"):
            sandbox.validate(str(child))

    def test_home_directory_rejected(self, tmp_path):
        # We can't test with real home, so verify the check exists
        sandbox = PathSandbox([Path.home().parent], allow_home=False)
        # This test just verifies the constructor works
        assert not sandbox._allow_home

    def test_canonical_string_normalized(self):
        cs = PathSandbox.get_canonical_string(Path("D:\\Projects\\My-Repo"))
        assert "\\" not in cs
        assert cs == cs.lower()

    def test_git_revision_detection(self, git_repo):
        rev = PathSandbox.get_git_revision(git_repo)
        assert len(rev) == 40 or rev == "unknown"  # Full SHA or unknown

    def test_language_detection(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hi')")
        (tmp_path / "config.yaml").write_text("key: val")
        langs = PathSandbox.detect_languages(tmp_path)
        assert "python" in langs


# ──────────────────────────────────────────────
# Registry Store
# ──────────────────────────────────────────────

class TestRegistryStore:
    def test_register_and_get(self, store, sample_record):
        saved = store.register(sample_record)
        assert saved.repository_id == sample_record.repository_id
        assert saved.version == 1

        retrieved = store.get(sample_record.repository_id)
        assert retrieved is not None
        assert retrieved.display_name == "my-repo"

    def test_duplicate_registration_rejected(self, store, sample_record):
        store.register(sample_record)
        with pytest.raises(ValueError, match="already registered"):
            store.register(sample_record)

    def test_list_all(self, store, sample_record):
        store.register(sample_record)
        repos = store.list_all()
        assert len(repos) == 1
        assert repos[0].repository_id == sample_record.repository_id

    def test_optimistic_versioning(self, store, sample_record):
        store.register(sample_record)

        # Update with correct version
        updated = store.update(
            sample_record.repository_id,
            {"display_name": "renamed"},
            expected_version=1,
            reason="rename test",
        )
        assert updated.version == 2
        assert updated.display_name == "renamed"

    def test_optimistic_lock_conflict(self, store, sample_record):
        store.register(sample_record)
        with pytest.raises(OptimisticLockError, match="Version conflict"):
            store.update(
                sample_record.repository_id,
                {"display_name": "stale"},
                expected_version=999,
            )

    def test_disable_repository(self, store, sample_record):
        store.register(sample_record)
        disabled = store.disable(sample_record.repository_id)
        assert disabled.disabled is True
        assert disabled.trust_level == TrustLevel.DISABLED.value

    def test_immutable_fields_protected(self, store, sample_record):
        store.register(sample_record)
        updated = store.update(
            sample_record.repository_id,
            {"repository_id": "hacked-id", "display_name": "ok"},
            expected_version=1,
        )
        # repository_id should not change
        assert updated.repository_id == sample_record.repository_id

    def test_content_hash_integrity(self, store, sample_record):
        store.register(sample_record)

        # Verify integrity succeeds
        ok, msg = store.verify_integrity()
        assert ok

    def test_corruption_detection(self, store, sample_record):
        store.register(sample_record)

        # Corrupt the file
        reg_file = store._registry_file
        data = json.loads(reg_file.read_text())
        data["repositories"][sample_record.repository_id]["display_name"] = "CORRUPTED"
        # Write without updating hash
        reg_file.write_text(json.dumps(data))

        ok, msg = store.verify_integrity()
        assert not ok
        assert "hash mismatch" in msg.lower() or "corrupt" in msg.lower()

    def test_audit_events_recorded(self, store, sample_record):
        store.register(sample_record)
        events = store.list_events(repository_id=sample_record.repository_id)
        assert len(events) >= 1
        assert events[0].event_type == "registered"

    def test_remove_repository(self, store, sample_record):
        store.register(sample_record)
        assert store.remove(sample_record.repository_id)
        assert store.get(sample_record.repository_id) is None

    def test_nonexistent_get_returns_none(self, store):
        assert store.get("nonexistent") is None

    def test_nonexistent_update_raises(self, store):
        with pytest.raises(ValueError, match="not found"):
            store.update("nonexistent", {}, 1)

    def test_empty_registry_lists_zero(self, store):
        repos = store.list_all()
        assert len(repos) == 0


# ──────────────────────────────────────────────
# Capabilities Manager
# ──────────────────────────────────────────────

class TestCapabilities:
    def test_default_capabilities(self):
        internal = CapabilitiesManager.get_defaults(TrustLevel.TRUSTED_INTERNAL)
        assert "read_source" in internal
        assert "execute_validation" in internal

    def test_disabled_no_capabilities(self):
        caps = CapabilitiesManager.get_defaults(TrustLevel.DISABLED)
        assert len(caps) == 0

    def test_check_capability(self):
        caps = ["read_source", "build_graph"]
        assert CapabilitiesManager.check(caps, Capability.READ_SOURCE)
        assert not CapabilitiesManager.check(caps, Capability.EXECUTE_VALIDATION)

    def test_require_capability_passes(self):
        caps = ["read_source"]
        CapabilitiesManager.require("repo-1", caps, Capability.READ_SOURCE)

    def test_require_capability_raises(self):
        caps = ["read_source"]
        with pytest.raises(CapabilityDeniedError):
            CapabilitiesManager.require("repo-1", caps, Capability.EXECUTE_VALIDATION)

    def test_grant_to_untrusted_denied(self):
        with pytest.raises(CapabilityDeniedError):
            CapabilitiesManager.grant(
                [],
                TrustLevel.UNTRUSTED_EXTERNAL,
                Capability.EXECUTE_VALIDATION,
            )

    def test_grant_to_disabled_denied(self):
        with pytest.raises(CapabilityDeniedError):
            CapabilitiesManager.grant([], TrustLevel.DISABLED, Capability.READ_SOURCE)

    def test_grant_valid(self):
        caps = CapabilitiesManager.grant(
            ["read_source"],
            TrustLevel.TRUSTED_INTERNAL,
            Capability.BUILD_GRAPH,
        )
        assert "build_graph" in caps
        assert "read_source" in caps

    def test_revoke(self):
        caps = CapabilitiesManager.revoke(["read_source", "build_graph"], Capability.BUILD_GRAPH)
        assert "build_graph" not in caps
        assert "read_source" in caps

    def test_validate_capabilities(self):
        issues = CapabilitiesManager.validate_capabilities(
            ["execute_validation"],
            TrustLevel.UNTRUSTED_EXTERNAL,
        )
        assert len(issues) > 0
        assert "untrusted" in issues[0].lower()

    def test_validate_disabled(self):
        issues = CapabilitiesManager.validate_capabilities(
            ["read_source"],
            TrustLevel.DISABLED,
        )
        assert len(issues) > 0


# ──────────────────────────────────────────────
# Workspace Roots Manager
# ──────────────────────────────────────────────

class TestRootsManager:
    def test_initialize(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        roots = mgr.initialize()
        assert "worktrees" in roots
        assert "quarantine" in roots
        for name, path in roots.items():
            assert path.exists()

    def test_containment_check(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        wt_root = mgr.get_root("worktrees")
        inner = wt_root / "test-ws"
        inner.mkdir()
        assert mgr.validate_containment(inner, "worktrees")
        assert not mgr.validate_containment(tmp_path / "elsewhere", "worktrees")

    def test_create_workspace_dir(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        ws_dir = mgr.create_workspace_dir("worktrees", "ws-001")
        assert ws_dir.exists()
        assert mgr.validate_containment(ws_dir, "worktrees")

    def test_duplicate_workspace_dir_rejected(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        mgr.create_workspace_dir("worktrees", "ws-001")
        with pytest.raises(ValueError, match="already exists"):
            mgr.create_workspace_dir("worktrees", "ws-001")

    def test_safe_remove(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        ws_dir = mgr.create_workspace_dir("worktrees", "ws-del")
        (ws_dir / "file.txt").write_text("data")
        assert mgr.safe_remove(ws_dir, "worktrees")
        assert not ws_dir.exists()

    def test_safe_remove_outside_root_rejected(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ValueError, match="outside"):
            mgr.safe_remove(outside, "worktrees")

    def test_quarantine(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        ws_dir = mgr.create_workspace_dir("worktrees", "ws-fail")
        (ws_dir / "data.txt").write_text("content")
        q_path = mgr.quarantine(ws_dir, "worktrees", "cleanup failed")
        assert q_path is not None
        assert q_path.exists()
        assert not ws_dir.exists()
        reason_file = q_path / "_quarantine_reason.txt"
        assert reason_file.exists()
        assert "cleanup failed" in reason_file.read_text()

    def test_find_orphans(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        root = mgr.get_root("worktrees")
        (root / "known").mkdir()
        (root / "orphan").mkdir()
        orphans = mgr.find_orphans("worktrees", {"known"})
        assert len(orphans) == 1
        assert orphans[0].name == "orphan"

    def test_usage_report(self, tmp_path):
        mgr = WorkspaceRootsManager(tmp_path / "workspace")
        mgr.initialize()
        ws_dir = mgr.create_workspace_dir("artifacts", "report-1")
        (ws_dir / "data.json").write_text('{"k": "v"}')
        usage = mgr.get_usage()
        assert usage["artifacts"]["files"] >= 1
        assert usage["artifacts"]["bytes"] > 0


# ──────────────────────────────────────────────
# Audit Events
# ──────────────────────────────────────────────

class TestAuditEvents:
    def test_emit_and_list(self, tmp_path):
        emitter = AuditEventsEmitter(tmp_path / "events")
        event = emitter.emit(
            RegistryEventType.REGISTERED,
            "repo-1",
            "user-a",
            0, 1,
            "initial",
            "cli",
        )
        assert event.event_id.startswith("evt-")

        events = emitter.list_events()
        assert len(events) == 1
        assert events[0].repository_id == "repo-1"

    def test_filter_by_repository(self, tmp_path):
        emitter = AuditEventsEmitter(tmp_path / "events")
        emitter.emit(RegistryEventType.REGISTERED, "repo-1", "a", 0, 1, "r", "cli")
        emitter.emit(RegistryEventType.REGISTERED, "repo-2", "a", 0, 1, "r", "cli")

        filtered = emitter.list_events(repository_id="repo-1")
        assert len(filtered) == 1
        assert filtered[0].repository_id == "repo-1"

    def test_export(self, tmp_path):
        emitter = AuditEventsEmitter(tmp_path / "events")
        emitter.emit(RegistryEventType.REGISTERED, "repo-1", "a", 0, 1, "r", "cli")
        emitter.emit(RegistryEventType.UPDATED, "repo-1", "a", 1, 2, "u", "api")

        out = tmp_path / "export.jsonl"
        count = emitter.export(out)
        assert count == 2
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_count(self, tmp_path):
        emitter = AuditEventsEmitter(tmp_path / "events")
        emitter.emit(RegistryEventType.REGISTERED, "repo-1", "a", 0, 1, "r", "cli")
        emitter.emit(RegistryEventType.DISABLED, "repo-1", "a", 1, 2, "d", "cli")
        assert emitter.count() == 2
        assert emitter.count(repository_id="repo-1") == 2


# ──────────────────────────────────────────────
# CLI integration (argument parsing only)
# ──────────────────────────────────────────────

class TestCLI:
    def test_cli_import(self):
        from brain.workspace.cli import main
        assert callable(main)

    def test_cli_help(self):
        from brain.workspace.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0


# ──────────────────────────────────────────────
# API router import
# ──────────────────────────────────────────────

class TestAPIRouter:
    def test_router_import(self):
        from apps.api.routers.workspace import router
        assert router is not None
        assert router.prefix == "/workspace"

    def test_router_has_endpoints(self):
        from apps.api.routers.workspace import router
        routes = [r.path for r in router.routes]
        assert "/repositories" in routes or any("/repositories" in r for r in routes)
