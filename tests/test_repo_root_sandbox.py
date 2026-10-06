"""Repo path sandbox: cwd and the system tempdir are scratch roots, not always-allowed."""

from unittest.mock import AsyncMock, patch

import pytest

from brain.config import paths
from brain.config.settings import settings
from brain.memory import consolidation


@pytest.fixture
def scratch_repo(tmp_path, monkeypatch):
    # Repo lives under a patched tempdir, outside REPO_ROOT / TARGET_REPO_PATH.
    repo = tmp_path / "scratch_repo"
    repo.mkdir()
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setattr(paths, "get_repo_root", lambda: configured)
    monkeypatch.setattr(settings, "TARGET_REPO_PATH", str(configured))
    monkeypatch.setattr(settings, "ALLOWED_REPO_ROOTS", None)
    monkeypatch.setattr(paths.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.chdir(configured)
    return repo


def test_scratch_roots_default_on_outside_production(scratch_repo, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_SCRATCH_REPO_ROOTS", None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    assert paths.validate_secure_repo_path(scratch_repo) == scratch_repo.resolve()


def test_scratch_roots_default_off_in_production(scratch_repo, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_SCRATCH_REPO_ROOTS", None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="outside allowed repository roots"):
        paths.validate_secure_repo_path(scratch_repo)


def test_scratch_roots_explicit_setting_wins(scratch_repo, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "ALLOW_SCRATCH_REPO_ROOTS", False)
    with pytest.raises(ValueError, match="outside allowed repository roots"):
        paths.validate_secure_repo_path(scratch_repo)

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ALLOW_SCRATCH_REPO_ROOTS", True)
    assert paths.validate_secure_repo_path(scratch_repo) == scratch_repo.resolve()


def test_cwd_not_a_root_when_scratch_disabled(tmp_path, monkeypatch):
    configured = tmp_path / "configured"
    configured.mkdir()
    workdir = tmp_path / "workdir"
    (workdir / "repo").mkdir(parents=True)
    monkeypatch.setattr(paths, "get_repo_root", lambda: configured)
    monkeypatch.setattr(settings, "TARGET_REPO_PATH", str(configured))
    monkeypatch.setattr(settings, "ALLOWED_REPO_ROOTS", None)
    monkeypatch.setattr(settings, "ALLOW_SCRATCH_REPO_ROOTS", False)
    monkeypatch.setattr(paths.tempfile, "gettempdir", lambda: str(configured))
    monkeypatch.chdir(workdir)
    with pytest.raises(ValueError, match="outside allowed repository roots"):
        paths.validate_secure_repo_path(workdir / "repo")


def test_allowed_repo_roots_is_a_real_setting(scratch_repo, monkeypatch):
    # Previously read via getattr only, so the env var was silently ignored.
    assert "ALLOWED_REPO_ROOTS" in type(settings).model_fields
    monkeypatch.setattr(settings, "ALLOW_SCRATCH_REPO_ROOTS", False)
    monkeypatch.setattr(settings, "ALLOWED_REPO_ROOTS", f" , {scratch_repo.parent} ")
    assert paths.validate_secure_repo_path(scratch_repo) == scratch_repo.resolve()


@pytest.mark.asyncio
async def test_promote_candidate_passes_scope_to_learning_store():
    candidate = consolidation.ConsolidationCandidate(statement="s", evidence=[{"event_id": 1}])
    gate = consolidation.GateResult(outcome=consolidation.GateOutcome.PROMOTE)
    add = AsyncMock(return_value=7)
    with patch.object(consolidation.LearningStore, "add_learning", add), patch.object(
        consolidation, "_derive_candidate_repo_scope", AsyncMock(return_value="/srv/repo-a")
    ) as derive:
        assert await consolidation.promote_candidate(candidate, gate) == 7
        assert add.await_args.kwargs["repo_scope"] == "/srv/repo-a"

        await consolidation.promote_candidate(candidate, gate, repo_scope="/srv/explicit/")
        assert add.await_args.kwargs["repo_scope"] == "/srv/explicit"
        derive.assert_awaited_once()


@pytest.mark.asyncio
async def test_promote_candidate_skips_non_promote_gate():
    gate = consolidation.GateResult(outcome=consolidation.GateOutcome.NEEDS_APPROVAL)
    add = AsyncMock()
    with patch.object(consolidation.LearningStore, "add_learning", add):
        assert await consolidation.promote_candidate(
            consolidation.ConsolidationCandidate(statement="s"), gate
        ) is None
    add.assert_not_awaited()
