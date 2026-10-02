"""Tests for incremental intelligence and freshness — Workstream C.

Covers: incremental update planning, generation derivation, tombstones, rename
and deletion handling, fallback to full rebuild, interrupted builds, activation
rollback, freshness invalidation, cache invalidation, CLI, and API wiring.

Every graph-level test runs against a real Git repository with real commits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.freshness.generation_deriver import GenerationDeriver
from brain.freshness.incremental_planner import IncrementalPlanner
from brain.freshness.models import (
    ArtifactType,
    FreshnessState,
    IncrementalPlan,
    InvalidationEvent,
    InvalidationEventType,
    RebuildDecision,
)
from brain.freshness.tracker import FreshnessTracker, IncrementalCache, InvalidationBus
from brain.graph.builder_v2 import GraphBuilderV2
from brain.graph.generation_manager import GraphGenerationManager
from tests.support import git_fixtures as gf


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path) -> Path:
    return gf.make_python_repo(tmp_path, "inc-repo")


@pytest.fixture
def built_repo(repo) -> tuple[Path, str, str]:
    """Repository with an active generation-1 graph. Returns (repo, gen_id, rev)."""
    builder = GraphBuilderV2(repo)
    meta, _report = builder.build_generation(gf.head(repo))
    mgr = GraphGenerationManager(repo / ".brain")
    assert mgr.activate_generation(meta.generation_id)
    return repo, meta.generation_id, gf.head(repo)


# ──────────────────────────────────────────────
# Phase C1 — incremental planning
# ──────────────────────────────────────────────


def test_plan_detects_modification(built_repo):
    repo, gen_id, base = built_repo
    candidate = gf.commit_files(repo, {"pkg/util.py": "def helper(v):\n    return v * 3\n"}, "mod")

    plan = IncrementalPlanner().plan_update(repo, base, candidate, repository_id="inc-repo")

    assert plan.modified_files == ["pkg/util.py"]
    assert plan.decision == RebuildDecision.INCREMENTAL.value
    assert "pkg/util.py" in plan.re_extraction_scope
    assert plan.fallback_reason is None


def test_plan_detects_rename(built_repo):
    repo, gen_id, base = built_repo
    gf.rename_file(repo, "pkg/util.py", "pkg/helpers.py")
    candidate = gf.commit_all(repo, "rename util -> helpers")

    plan = IncrementalPlanner().plan_update(repo, base, candidate, repository_id="inc-repo")

    assert plan.renamed_files == [("pkg/util.py", "pkg/helpers.py")]
    assert "pkg/helpers.py" in plan.re_extraction_scope
    assert "pkg/util.py" in plan.affected_paths


def test_plan_detects_deletion(built_repo):
    repo, gen_id, base = built_repo
    gf.delete_files(repo, ["pkg/util.py"])
    candidate = gf.commit_all(repo, "delete util")

    plan = IncrementalPlanner().plan_update(repo, base, candidate, repository_id="inc-repo")

    assert plan.deleted_files == ["pkg/util.py"]
    assert "pkg/util.py" not in plan.re_extraction_scope


def test_plan_counts_stale_entities_from_base_store(built_repo):
    repo, gen_id, base = built_repo
    candidate = gf.commit_files(repo, {"pkg/core.py": "class Core:\n    pass\n"}, "shrink core")

    mgr = GraphGenerationManager(repo / ".brain")
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="inc-repo", base_store=mgr.get_active_graph_store()
    )

    assert plan.stale_nodes > 0
    assert plan.stale_relationships > 0
    assert any(sym.endswith("Core") or ":class:" in sym for sym in plan.affected_symbols)


def test_plan_falls_back_on_infrastructure_change(built_repo):
    repo, gen_id, base = built_repo
    candidate = gf.commit_files(repo, {"pyproject.toml": "[project]\nname='x'\n"}, "add pyproject")

    plan = IncrementalPlanner().plan_update(repo, base, candidate, repository_id="inc-repo")

    # pyproject.toml is new here, so it lands in added_files — still infrastructure.
    assert plan.decision == RebuildDecision.FULL_REBUILD.value
    assert "Infrastructure files changed" in (plan.fallback_reason or "")


def test_plan_falls_back_on_too_many_renames():
    plan = IncrementalPlan(base_revision="a", candidate_revision="b")
    plan.renamed_files = [(f"old{i}.py", f"new{i}.py") for i in range(30)]
    should, reason = IncrementalPlanner.should_fallback(plan)
    assert should
    assert "Too many renames" in reason


def test_plan_no_change_decision(built_repo):
    repo, gen_id, base = built_repo
    plan = IncrementalPlanner().plan_update(repo, base, base, repository_id="inc-repo")
    assert plan.decision == RebuildDecision.NO_CHANGE.value
    assert plan.total_changes == 0


def test_plan_fingerprint_ignores_ordering(built_repo):
    repo, gen_id, base = built_repo
    a = IncrementalPlan(base_revision="x", candidate_revision="y", repository_id="r")
    a.modified_files = ["b.py", "a.py"]
    b = IncrementalPlan(base_revision="x", candidate_revision="y", repository_id="r")
    b.modified_files = ["a.py", "b.py"]
    assert a.semantic_fingerprint() == b.semantic_fingerprint()


# ──────────────────────────────────────────────
# Phase C2 — generation derivation
# ──────────────────────────────────────────────


def test_incremental_derivation_replaces_only_affected_entities(built_repo):
    repo, gen_id, base = built_repo
    mgr = GraphGenerationManager(repo / ".brain")
    base_store = mgr.load_graph_store(gen_id)
    base_nodes = len(base_store.nodes)

    candidate = gf.commit_files(
        repo, {"pkg/util.py": "def helper(v):\n    return v * 3\n\n\ndef extra():\n    return 1\n"}, "extend util"
    )
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="inc-repo", base_store=base_store
    )
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    assert report.decision == RebuildDecision.INCREMENTAL.value
    assert report.validation_errors == []
    assert report.activated is True
    assert report.copied_nodes > 0
    assert report.added_nodes > 0
    assert report.tombstoned_nodes > 0

    new_store = mgr.load_graph_store(report.new_generation_id)
    assert new_store is not None
    # The new symbol exists, and every entity carries the new generation id.
    assert any("extra" in nid for nid in new_store.nodes)
    assert all(n.generation_id == report.new_generation_id for n in new_store.nodes.values())
    assert all(f":{gen_id}:" not in nid for nid in new_store.nodes)
    assert len(new_store.nodes) >= base_nodes


def test_derivation_has_no_cross_generation_edges(built_repo):
    repo, gen_id, base = built_repo
    candidate = gf.commit_files(repo, {"pkg/core.py": "import os\n\n\nclass Core:\n    pass\n"}, "core rewrite")
    mgr = GraphGenerationManager(repo / ".brain")
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="inc-repo", base_store=mgr.get_active_graph_store()
    )
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    store = mgr.load_graph_store(report.new_generation_id)
    for rel in store.relationships:
        assert f":{gen_id}:" not in rel.source_id
        assert f":{gen_id}:" not in rel.target_id


def test_deleted_file_is_tombstoned_not_copied(built_repo):
    repo, gen_id, base = built_repo
    mgr = GraphGenerationManager(repo / ".brain")
    base_store = mgr.load_graph_store(gen_id)
    assert any(n.normalized_path == "pkg/util.py" for n in base_store.nodes.values())

    gf.delete_files(repo, ["pkg/util.py"])
    candidate = gf.commit_all(repo, "remove util")
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="inc-repo", base_store=base_store
    )
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    new_store = mgr.load_graph_store(report.new_generation_id)
    assert not any(n.normalized_path == "pkg/util.py" for n in new_store.nodes.values())
    # No surviving relationship may point at a tombstoned entity.
    for rel in new_store.relationships:
        target = new_store.get_node(rel.target_id)
        if target is not None:
            assert target.normalized_path != "pkg/util.py"

    meta = next(
        g for g in mgr.list_generations() if g.generation_id == report.new_generation_id
    )
    assert meta.metrics["derivation"]["tombstoned_total"] > 0


def test_rename_does_not_duplicate_entities(built_repo):
    repo, gen_id, base = built_repo
    gf.rename_file(repo, "pkg/util.py", "pkg/helpers.py")
    candidate = gf.commit_all(repo, "rename")

    mgr = GraphGenerationManager(repo / ".brain")
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="inc-repo", base_store=mgr.get_active_graph_store()
    )
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    new_store = mgr.load_graph_store(report.new_generation_id)
    paths = {n.normalized_path for n in new_store.nodes.values()}
    assert "pkg/helpers.py" in paths
    assert "pkg/util.py" not in paths
    helper_nodes = [n for n in new_store.nodes.values() if n.properties.get("name") == "helper"]
    assert len(helper_nodes) == 1


def test_derivation_falls_back_to_full_rebuild_and_records_reason(built_repo):
    repo, gen_id, base = built_repo
    candidate = gf.commit_files(repo, {"requirements.txt": "loguru\n"}, "add requirements")

    plan = IncrementalPlanner().plan_update(repo, base, candidate, repository_id="inc-repo")
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    assert report.decision == RebuildDecision.FULL_REBUILD.value
    assert "Infrastructure files changed" in report.fallback_reason
    assert report.new_generation_id and report.new_generation_id != gen_id
    assert report.activated is True


def test_derivation_falls_back_when_worktree_is_not_at_candidate(built_repo):
    repo, gen_id, base = built_repo
    candidate = gf.commit_files(repo, {"pkg/util.py": "def helper(v):\n    return v\n"}, "mod")
    plan = IncrementalPlanner().plan_update(repo, base, candidate, repository_id="inc-repo")

    # Move the working tree back to the base revision: incremental extraction
    # would silently read the wrong content, so a full rebuild must take over.
    gf.checkout(repo, base)
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    assert report.decision == RebuildDecision.FULL_REBUILD.value
    assert "not at the candidate revision" in report.fallback_reason


def test_derivation_without_base_generation_falls_back(repo):
    plan = IncrementalPlan(
        base_revision=gf.head(repo), candidate_revision=gf.head(repo), repository_id="inc-repo"
    )
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, base_generation_id="")
    assert report.decision == RebuildDecision.FULL_REBUILD.value
    assert "No loadable base generation" in report.fallback_reason


def test_derivation_refuses_foreign_base_generation(built_repo):
    """Generation N of another repository is never copied into N+1."""
    repo, gen_id, base = built_repo
    candidate = gf.commit_files(repo, {"pkg/util.py": "def helper(v):\n    return v\n"}, "mod")
    plan = IncrementalPlanner().plan_update(repo, base, candidate, repository_id="other-repo")

    report = GenerationDeriver(repo, repository_id="other-repo").derive(plan, gen_id)

    assert report.decision == RebuildDecision.FULL_REBUILD.value
    assert "belongs to repository 'inc-repo'" in report.fallback_reason


def test_interrupted_derivation_leaves_active_generation_untouched(built_repo, monkeypatch):
    """A derivation that fails validation must not activate or replace N."""
    from brain.freshness import generation_deriver as deriver_module

    repo, gen_id, base = built_repo
    mgr = GraphGenerationManager(repo / ".brain")

    candidate = gf.commit_files(repo, {"pkg/util.py": "def helper(v):\n    return v\n"}, "mod")
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="inc-repo", base_store=mgr.get_active_graph_store()
    )

    def _boom(store, repository_id):
        raise deriver_module.GraphQualityValidationError("simulated quality gate failure")

    monkeypatch.setattr(deriver_module.GraphQualityValidator, "validate", staticmethod(_boom))
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    assert report.validation_errors
    assert report.activated is False
    assert mgr.get_active_generation_id() == gen_id
    failed = next(
        g for g in mgr.list_generations() if g.generation_id == report.new_generation_id
    )
    assert failed.status == "failed"


def test_activation_rollback_restores_previous_generation(built_repo):
    repo, gen_id, base = built_repo
    mgr = GraphGenerationManager(repo / ".brain")
    candidate = gf.commit_files(repo, {"pkg/util.py": "def helper(v):\n    return v + 1\n"}, "mod")
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="inc-repo", base_store=mgr.get_active_graph_store()
    )
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)
    assert mgr.get_active_generation_id() == report.new_generation_id

    rolled_back_to = mgr.rollback()
    assert rolled_back_to == gen_id
    assert mgr.get_active_generation_id() == gen_id


def test_no_change_derivation_is_stable(built_repo):
    repo, gen_id, base = built_repo
    mgr = GraphGenerationManager(repo / ".brain")
    plan = IncrementalPlanner().plan_update(
        repo, base, base, repository_id="inc-repo", base_store=mgr.get_active_graph_store()
    )
    report = GenerationDeriver(repo, repository_id="inc-repo").derive(plan, gen_id)

    base_store = mgr.load_graph_store(gen_id)
    new_store = mgr.load_graph_store(report.new_generation_id)
    assert len(new_store.nodes) == len(base_store.nodes)
    assert len(new_store.relationships) == len(base_store.relationships)


# ──────────────────────────────────────────────
# Phase C3 — freshness state
# ──────────────────────────────────────────────


def test_freshness_never_current_without_verified_revision(tmp_path):
    tracker = FreshnessTracker(tmp_path / "freshness")
    rec = tracker.record(
        artifact_id="graph:x",
        artifact_type=ArtifactType.REPOSITORY_GRAPH.value,
        repository_id="x",
        state=FreshnessState.CURRENT,
        source_revision="abc123",
        evidence="built",
        observed_revision="",  # nothing verified
    )
    assert rec.state == FreshnessState.UNKNOWN.value
    assert "could not be verified" in rec.reason


def test_freshness_downgrades_to_stale_on_revision_mismatch(tmp_path):
    tracker = FreshnessTracker(tmp_path / "freshness")
    rec = tracker.record(
        artifact_id="graph:x",
        artifact_type=ArtifactType.REPOSITORY_GRAPH.value,
        repository_id="x",
        state=FreshnessState.CURRENT,
        source_revision="abc123",
        evidence="built",
        observed_revision="def456",
    )
    assert rec.state == FreshnessState.STALE.value


def test_freshness_current_with_matching_evidence(repo, tmp_path):
    tracker = FreshnessTracker(tmp_path / "freshness")
    rev = gf.head(repo)
    rec = tracker.record_from_repository(
        artifact_id="graph:inc-repo",
        artifact_type=ArtifactType.REPOSITORY_GRAPH,
        repository_id="inc-repo",
        repo_path=repo,
        source_revision=rev,
        evidence="generation gen-1",
    )
    assert rec.state == FreshnessState.CURRENT.value
    assert rec.observed_revision == rev

    explained = tracker.explain("graph:inc-repo")
    assert explained["revision_match"] is True


def test_freshness_transitions_and_supersede(tmp_path):
    tracker = FreshnessTracker(tmp_path / "freshness")
    tracker.record(
        artifact_id="plan:1",
        artifact_type=ArtifactType.REMEDIATION_PLAN.value,
        repository_id="x",
        state=FreshnessState.BUILDING,
        source_revision="abc",
        evidence="planning",
    )
    assert tracker.invalidate("plan:1", "revision change", "abc -> def").state == (
        FreshnessState.STALE.value
    )
    assert tracker.mark_failed("plan:1", "extractor crashed").state == FreshnessState.FAILED.value
    superseded = tracker.supersede("plan:1", "plan:2")
    assert superseded.state == FreshnessState.SUPERSEDED.value
    assert superseded.superseded_by == "plan:2"
    assert tracker.get("plan:1").version >= 4


def test_freshness_unknown_artifact_returns_none(tmp_path):
    tracker = FreshnessTracker(tmp_path / "freshness")
    assert tracker.get("missing") is None
    assert tracker.invalidate("missing", "x") is None
    assert tracker.explain("missing") == {"artifact_id": "missing", "found": False}


# ──────────────────────────────────────────────
# Phase C4 — invalidation bus
# ──────────────────────────────────────────────


def test_invalidation_event_invalidates_dependents(tmp_path):
    tracker = FreshnessTracker(tmp_path / "freshness")
    for artifact in ("graph:x", "impact:x", "plan:x"):
        tracker.record(
            artifact_id=artifact,
            artifact_type=ArtifactType.IMPACT_ANALYSIS.value,
            repository_id="x",
            state=FreshnessState.BUILDING,
            source_revision="abc",
            evidence="seed",
        )

    bus = InvalidationBus()
    bus.register_handler(
        InvalidationEventType.REPOSITORY_REVISION_CHANGED.value,
        lambda ev: tracker.invalidate_repository(ev.repository_id, ev.caused_by, ev.artifact_id),
    )
    results = bus.emit(
        InvalidationEvent.create(
            InvalidationEventType.REPOSITORY_REVISION_CHANGED.value,
            artifact_id="repo:x",
            caused_by="git-hook",
            repository_id="x",
        )
    )

    assert results == [3]
    assert all(r.state == FreshnessState.STALE.value for r in tracker.list_by_repository("x"))
    assert len(bus.history()) == 1


def test_invalidation_bus_isolates_failing_handler():
    bus = InvalidationBus()

    def boom(_event):
        raise RuntimeError("subscriber exploded")

    bus.register_handler("policy_changed", boom)
    bus.register_handler("policy_changed", lambda ev: "ok")
    results = bus.emit(
        InvalidationEvent.create("policy_changed", artifact_id="policy", caused_by="test")
    )
    assert any(isinstance(r, dict) and "error" in r for r in results)
    assert "ok" in results


# ──────────────────────────────────────────────
# Phase C5 — incremental cache
# ──────────────────────────────────────────────


def test_cache_key_requires_semantic_inputs():
    with pytest.raises(ValueError):
        IncrementalCache.build_key("ast")
    with pytest.raises(ValueError):
        IncrementalCache.build_key("ast", path="pkg/core.py")

    key = IncrementalCache.build_key("ast", path="pkg/core.py", content_hash="deadbeef")
    assert key.startswith("ast:")


def test_cache_key_changes_with_every_semantic_input():
    a = IncrementalCache.build_key("ast", path="p.py", content_hash="a", parser="3.13")
    b = IncrementalCache.build_key("ast", path="p.py", content_hash="b", parser="3.13")
    c = IncrementalCache.build_key("ast", path="p.py", content_hash="a", parser="3.12")
    assert len({a, b, c}) == 3


def test_cache_roundtrip_stats_and_invalidation(tmp_path):
    cache = IncrementalCache(tmp_path / "cache", namespace="ast")
    key = IncrementalCache.build_key("ast", path="p.py", content_hash="abc")

    assert cache.get(key) is None
    cache.put(key, {"symbols": ["a", "b"]})
    assert cache.get(key) == {"symbols": ["a", "b"]}

    stats = cache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1 and stats["entries"] == 1
    assert 0.0 < stats["hit_rate"] < 1.0

    assert cache.invalidate(key) is True
    assert cache.get(key) is None


def test_cache_ttl_expiry(tmp_path):
    cache = IncrementalCache(tmp_path / "cache")
    key = IncrementalCache.build_key("policy", path="rules.yaml", content_hash="x")
    cache.put(key, "value", ttl_seconds=-1)  # already expired
    assert cache.get(key) is None


def test_cache_evicts_beyond_limit(tmp_path):
    cache = IncrementalCache(tmp_path / "cache", max_entries=3)
    for i in range(6):
        cache.put(IncrementalCache.build_key("ast", path=f"f{i}.py", content_hash=str(i)), i)
    assert cache.stats()["entries"] <= 3
    assert cache.stats()["evictions"] >= 3


def test_cache_recovers_from_corruption(tmp_path):
    cache = IncrementalCache(tmp_path / "cache")
    key = IncrementalCache.build_key("ast", path="p.py", content_hash="abc")
    cache.put(key, "value")
    corrupted = next((tmp_path / "cache").glob("*.cache.json"))
    corrupted.write_text("{not json", encoding="utf-8")

    assert cache.get(key) is None
    assert cache.stats()["corruptions"] == 1
    cache.put(key, "value2")
    assert cache.get(key) == "value2"


def test_cache_invalidate_kind(tmp_path):
    cache = IncrementalCache(tmp_path / "cache")
    cache.put(IncrementalCache.build_key("ast", path="a.py", content_hash="1"), 1)
    cache.put(IncrementalCache.build_key("ast", path="b.py", content_hash="2"), 2)
    cache.put(IncrementalCache.build_key("codeowners", path="CODEOWNERS", content_hash="3"), 3)

    assert cache.invalidate_kind("ast") == 2
    assert cache.stats()["entries"] == 1


# ──────────────────────────────────────────────
# Phase C6 — CLI and API
# ──────────────────────────────────────────────


def _register_repo(registry_dir: Path, repo: Path):
    from brain.workspace.capabilities import CapabilitiesManager
    from brain.workspace.models import RepositoryRecord, TrustLevel, _generate_repository_id
    from brain.workspace.path_sandbox import PathSandbox
    from brain.workspace.registry_store import RegistryStore

    store = RegistryStore(registry_dir)
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
        capabilities=CapabilitiesManager.get_defaults(TrustLevel.TRUSTED_INTERNAL),
    )
    return store.register(record, actor="test", source="test")


def _build_under_registry_id(repo: Path, repository_id: str) -> str:
    """Build generation 1 using the registry's stable repository identity."""
    builder = GraphBuilderV2(repo)
    builder.repo_id = repository_id
    meta, _report = builder.build_generation(gf.head(repo))
    mgr = GraphGenerationManager(repo / ".brain")
    assert mgr.activate_generation(meta.generation_id)
    return meta.generation_id


def test_cli_status_and_rebuild(repo, tmp_path, capsys):
    from brain.freshness import cli as freshness_cli

    registry_dir = tmp_path / "registry"
    record = _register_repo(registry_dir, repo)
    gen_id = _build_under_registry_id(repo, record.repository_id)

    freshness_cli.main(
        [
            "--brain-dir",
            str(tmp_path / "freshness"),
            "--registry-dir",
            str(registry_dir),
            "status",
            "--repository",
            record.repository_id,
        ]
    )
    status = json.loads(capsys.readouterr().out)
    assert status["active_generation_id"] == gen_id
    assert status["graph_freshness"] == FreshnessState.CURRENT.value

    gf.commit_files(repo, {"pkg/util.py": "def helper(v):\n    return v\n"}, "mod")
    freshness_cli.main(
        [
            "--brain-dir",
            str(tmp_path / "freshness"),
            "--registry-dir",
            str(registry_dir),
            "rebuild",
            "--repository",
            record.repository_id,
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["derivation"]["decision"] == RebuildDecision.INCREMENTAL.value
    assert result["derivation"]["activated"] is True

    freshness_cli.main(
        [
            "--brain-dir",
            str(tmp_path / "freshness"),
            "--registry-dir",
            str(registry_dir),
            "explain",
            "--artifact",
            f"graph:{record.repository_id}",
        ]
    )
    explained = json.loads(capsys.readouterr().out)
    assert explained["state"] == FreshnessState.CURRENT.value
    assert explained["revision_match"] is True


def test_cli_plan_command(repo, tmp_path, capsys):
    from brain.freshness import cli as freshness_cli

    base = gf.head(repo)
    registry_dir = tmp_path / "registry"
    record = _register_repo(registry_dir, repo)
    candidate = gf.commit_files(repo, {"pkg/util.py": "def helper(v):\n    return 0\n"}, "mod")

    freshness_cli.main(
        [
            "--registry-dir",
            str(registry_dir),
            "plan",
            "--repository",
            record.repository_id,
            "--base",
            base,
            "--candidate",
            candidate,
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["plan"]["modified_files"] == ["pkg/util.py"]
    assert out["preview"]["fallback_to_full"] is False


def test_cli_rejects_unregistered_repository(tmp_path, capsys):
    from brain.freshness import cli as freshness_cli

    with pytest.raises(SystemExit) as exc:
        freshness_cli.main(
            ["--registry-dir", str(tmp_path / "registry"), "status", "--repository", "repo-nope"]
        )
    assert exc.value.code == 1


def _mounted_paths() -> set:
    """Paths the application actually serves, per its own OpenAPI schema."""
    from apps.api.main import app

    return set(app.openapi()["paths"])


def test_freshness_router_is_mounted():
    paths = _mounted_paths()
    assert "/freshness/repositories/{repository_id}" in paths
    assert "/freshness/artifacts/{artifact_id}" in paths
    assert "/freshness/repositories/{repository_id}/rebuild" in paths


def test_workspace_and_portfolio_routers_are_mounted():
    """Workstream A/B endpoints were defined but never mounted before v0.5.0."""
    paths = _mounted_paths()
    assert "/workspace/repositories" in paths
    assert "/workspace/repositories/{repository_id}/capabilities" in paths
    assert "/portfolio" in paths
    assert "/portfolio/{portfolio_id}/build" in paths
