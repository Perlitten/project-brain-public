import json
import subprocess
from unittest.mock import AsyncMock

import pytest

from brain.config.settings import Settings, settings
from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.memory.source_overlay import collect_source_delta, read_live_source
from brain.retrieval.service import RetrievalCandidate, RetrievalResult


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init")
    (tmp_path / "feature.py").write_text("def feature():\n    return 'indexed-old'\n")
    return tmp_path, commit(tmp_path)


def context_builder(root, indexed, freshness, monkeypatch):
    result = RetrievalResult(
        query="Explain feature", intent="runtime_context",
        repository={"repository_path": str(root), "repository_id": 42, "indexed_revision": indexed,
                    "freshness": {"status": freshness, "source_head_commit": indexed}},
        candidates=[RetrievalCandidate(path="feature.py", symbols=["feature"], ranges=[(1, 2)])],
    )
    retrieval = AsyncMock()
    retrieval.retrieve.return_value = result
    builder = RuntimeContextBuilder(retrieval)
    builder._load_slices = AsyncMock(return_value=[{
        "path": "feature.py", "range": [1, 2], "content": "def feature():\n    return 'indexed-old'\n",
    }])
    monkeypatch.setattr(settings, "BRAIN_STALE_CONTEXT_POLICY", "warn_and_serve")
    monkeypatch.setattr("brain.context.context_cache.get_cached_context", lambda *_: None)
    monkeypatch.setattr("brain.context.context_cache.put_cached_context", lambda *_args, **_kwargs: None)
    queued = AsyncMock(return_value="job-1")
    monkeypatch.setattr("brain.context.runtime_context_builder._queue_auto_reindex", queued)
    monkeypatch.setattr("brain.context.runtime_context_builder.select_relevant_normative_memory", AsyncMock(return_value={
        "rules": [{"id": "R-1", "description": "Retain source evidence"}],
        "decisions": [{"id": "D-1", "title": "Keep module boundaries"}],
    }))
    monkeypatch.setattr("brain.context.runtime_context_builder.select_learnings_for_context", AsyncMock(return_value=[{
        "id": "L-1", "statement": "Verify current source",
    }]))
    return builder, queued


def test_warn_and_serve_is_the_declared_default():
    assert Settings.model_fields["BRAIN_STALE_CONTEXT_POLICY"].default == "warn_and_serve"


@pytest.mark.asyncio
async def test_ahead_commit_serves_current_shifted_symbol_and_live_memory(repository, monkeypatch):
    root, indexed = repository
    (root / "feature.py").write_text("# inserted\n# shifted\ndef feature():\n    return 'current-live'\n")
    head = commit(root)
    builder, queued = context_builder(root, indexed, "behind", monkeypatch)

    payload = await builder.build("Explain feature", root)

    assert payload["status"] == "ok"
    assert payload["repo"]["freshness"] == "stale_served"
    assert payload["freshness"]["state"] == "stale_served"
    assert payload["freshness"]["reason"] == "source_ahead_of_index"
    assert payload["freshness"]["reindex_job_id"] == "job-1"
    assert payload["freshness"]["indexed_commit"] == indexed
    assert payload["repo"]["indexed_revision"] == indexed
    assert payload["repo"]["source_revision"] == head
    assert payload["freshness_warning"]["changed_files"] == ["feature.py"]
    assert payload["slices"][0]["range"] == [3, 4]
    assert "current-live" in payload["slices"][0]["content"]
    assert "indexed-old" not in json.dumps(payload["slices"])
    assert payload["slices"][0]["source"] == "live_disk"
    assert set(payload["memory"]) == {"decisions", "rules", "learnings"}
    queued.assert_awaited_once_with(str(root))


@pytest.mark.asyncio
async def test_dirty_tree_overrides_cached_current_freshness(repository, monkeypatch):
    root, indexed = repository
    (root / "feature.py").write_text("def feature():\n    return 'dirty-live'\n")
    builder, queued = context_builder(root, indexed, "current", monkeypatch)
    monkeypatch.setattr("brain.context.context_cache.get_cached_context", lambda *_: {
        "evidence_cache_version": 1, "slices": [{"path": "feature.py", "range": [1, 2], "content": "old cached"}],
    })
    payload = await builder.build("Explain feature", root, max_tokens=400)

    assert payload["status"] == "ok" and not payload.get("_cache_hit")
    assert payload["freshness_warning"]["working_tree_dirty"]
    assert "dirty-live" in payload["slices"][0]["content"]
    budgeted = {key: value for key, value in payload.items() if key != "freshness"}
    assert len(json.dumps(budgeted, ensure_ascii=False, separators=(",", ":")).encode()) <= 1600
    assert payload["freshness"]["state"] == "stale_served"
    assert payload["freshness"]["reason"] == "working_tree_dirty"
    assert payload["freshness"]["reindex_job_id"] is None
    assert "auto_reindex_deferred_until_clean" in payload["missing"]
    queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_mode_retains_blocking_and_reindex(repository, monkeypatch):
    root, indexed = repository
    builder, queued = context_builder(root, indexed, "behind", monkeypatch)
    monkeypatch.setattr(settings, "BRAIN_STALE_CONTEXT_POLICY", "strict_block")
    payload = await builder.build("Explain feature", root)

    assert payload["status"] == "stale_blocked"
    assert payload["freshness"]["state"] == "stale_blocked"
    assert payload["freshness"]["reason"] == "policy_strict_block"
    assert payload["freshness"]["reindex_job_id"] == "job-1"
    assert not payload.get("slices") and not payload.get("candidates")
    builder._load_slices.assert_not_awaited()
    queued.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleted_candidate_retains_memory_without_returning_deleted_code(repository, monkeypatch):
    root, indexed = repository
    (root / "feature.py").unlink()
    commit(root)
    builder, _ = context_builder(root, indexed, "behind", monkeypatch)
    payload = await builder.build("Explain feature", root)

    assert payload["status"] == "partial" and payload["freshness"]["state"] == "stale_served"
    assert not payload.get("slices")
    assert payload["memory"]["decisions"]


@pytest.mark.asyncio
async def test_missing_source_remains_blocked_in_warn_mode(tmp_path, monkeypatch):
    root = tmp_path / "missing"
    builder, queued = context_builder(root, "a" * 40, "behind", monkeypatch)
    payload = await builder.build("Explain feature", root)

    assert payload["status"] == "stale_blocked"
    assert "live_source_unavailable" in payload["missing"]
    assert payload["freshness"]["state"] == "stale_blocked"
    assert payload["freshness"]["reason"] == "live_source_unavailable"
    assert not payload.get("slices")
    queued.assert_not_awaited()


def test_source_overlay_rejects_escaping_paths_symlinks_binary_and_bounds_reads(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "private.py"
    outside.write_text("private_value = 'outside'\n")
    (root / "escape.py").symlink_to(outside)
    (root / ".env").write_text("API_KEY=private\n")
    (root / "alias.py").symlink_to(root / ".env")
    (root / "binary.py").write_bytes(b"zero\0binary")
    (root / "large.py").write_text("abcdefghij" * 100)

    assert read_live_source(root, "../private.py", 100) is None
    assert read_live_source(root, "escape.py", 100) is None
    assert read_live_source(root, "alias.py", 100) is None
    assert read_live_source(root, "binary.py", 100) is None
    bounded = read_live_source(root, "large.py", 100)
    assert bounded is not None and bounded.truncated
    assert len("".join(bounded.lines).encode()) == 100


def test_current_ranges_relocates_javascript_and_rejects_missing_symbols():
    from brain.memory.source_overlay import LiveSource, current_ranges

    text = "\n".join(["// shifted"] * 12 + ["export function feature() {", "  return 'live';", "}"])
    source = LiveSource(tuple(f"{line}\n" for line in text.splitlines()), "digest", False)
    ranges = current_ranges(source, ["feature"])

    assert ranges and ranges[0][0] <= 13 <= ranges[0][1]
    assert current_ranges(source, ["deleted_symbol"]) == []


def test_diff_is_nul_safe_and_bounded(repository):
    root, indexed = repository
    for name in ("new file.py", "new\nline.py", "unicode_ș.py"):
        (root / name).write_text("value = 1\n")
    delta = collect_source_delta(root, indexed, limit=2, timeout=1)
    assert len(delta.changed_files) == 2 and delta.dirty and not delta.complete
    assert "new\nline.py" in delta.changed_files
    assert len(json.dumps(delta.warning(20)).encode()) < 400


# A tar-deployed root (prod /app) has no .git. Origin follow indexes it from a
# Brain-owned mirror, so the indexed revision is a bare 40-hex commit while the
# working-tree diff can never be computed. Without the mirror verdict that
# combination was "unverified" on every request and strict_block never lifted.


@pytest.fixture
def snapshot_root(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "feature.py").write_text("def feature():\n    return 'indexed-old'\n")
    # Keep git from discovering a repository above the temporary directory.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return root


def mirror_served(builder, *, indexed, mirror_head, status):
    builder.retrieval.retrieve.return_value.repository["freshness"] = {
        "status": status, "indexed_commit": indexed, "source_head_commit": mirror_head,
        "source_revision_kind": "mirror", "index_source": "mirror", "age_seconds": 60,
    }


def test_snapshot_root_without_git_cannot_produce_a_complete_diff(snapshot_root):
    delta = collect_source_delta(snapshot_root, "a" * 40, limit=10, timeout=1)
    assert delta.source_present and not delta.complete
    assert delta.reason == "git_unavailable"


@pytest.mark.asyncio
async def test_mirror_verified_snapshot_is_not_strict_blocked(snapshot_root, monkeypatch):
    indexed = "a" * 40
    builder, queued = context_builder(snapshot_root, indexed, "current", monkeypatch)
    mirror_served(builder, indexed=indexed, mirror_head=indexed, status="current")
    monkeypatch.setattr(settings, "BRAIN_STALE_CONTEXT_POLICY", "strict_block")

    payload = await builder.build("Explain feature", snapshot_root)

    assert payload["status"] == "ok"
    assert "current_source_required_for_code_change" not in payload["missing"]
    assert payload["repo"]["freshness"] == "current"
    assert payload["freshness"]["state"] == "fresh"
    assert "freshness_warning" not in payload
    assert payload["slices"][0]["content"].endswith("'indexed-old'\n")
    queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_mirror_behind_the_index_still_blocks_under_strict_policy(snapshot_root, monkeypatch):
    indexed = "a" * 40
    builder, queued = context_builder(snapshot_root, indexed, "behind", monkeypatch)
    mirror_served(builder, indexed=indexed, mirror_head="b" * 40, status="behind")
    monkeypatch.setattr(settings, "BRAIN_STALE_CONTEXT_POLICY", "strict_block")

    payload = await builder.build("Explain feature", snapshot_root)

    assert payload["status"] == "stale_blocked"
    assert "current_source_required_for_code_change" in payload["missing"]
    queued.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkout_served_snapshot_with_a_commit_index_still_needs_a_diff(snapshot_root, monkeypatch):
    # Not mirror-served: nothing proves the tree matches the commit, so strict mode keeps blocking.
    indexed = "a" * 40
    builder, _ = context_builder(snapshot_root, indexed, "current", monkeypatch)
    monkeypatch.setattr(settings, "BRAIN_STALE_CONTEXT_POLICY", "strict_block")

    payload = await builder.build("Explain feature", snapshot_root)

    assert payload["status"] == "stale_blocked"
