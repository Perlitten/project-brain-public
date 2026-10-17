"""A repository path the sandbox refuses is a bad request (400), never a server fault (500)."""
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from apps.api.repo_paths import repo_path_errors_as_400
from apps.api.routers import core_lifecycle, core_retrieval, coupling, graph_v2, impact, remediation
from apps.api.schemas import ImpactRequest, IndexRequest, RelatedRequest
from brain.config import paths


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """One allowed root holding an existing repository; a sibling directory outside every root."""
    allowed = tmp_path / "allowed"
    inside = allowed / "repo"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(paths, "allowed_repo_roots", lambda: [allowed])
    return inside, outside


def test_only_sandbox_refusals_become_400():
    with pytest.raises(HTTPException) as refused:
        with repo_path_errors_as_400():
            raise ValueError("Access denied: outside allowed repository roots")
    assert refused.value.status_code == 400
    assert "outside allowed repository roots" in refused.value.detail
    with pytest.raises(KeyError):
        with repo_path_errors_as_400():
            raise KeyError("unrelated")


@pytest.mark.asyncio
async def test_late_interaction_status_refuses_a_path_outside_the_roots_with_400(roots, monkeypatch):
    _, outside = roots
    lookup = AsyncMock()
    monkeypatch.setattr(core_lifecycle, "require_repository_by_path", lookup)
    with pytest.raises(HTTPException) as refused:
        await core_lifecycle.late_interaction_status(repo_path=str(outside))
    assert refused.value.status_code == 400
    assert "outside allowed repository roots" in refused.value.detail
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_interaction_status_refuses_a_missing_folder_with_400(roots):
    inside, _ = roots
    with pytest.raises(HTTPException) as refused:
        await core_lifecycle.late_interaction_status(repo_path=str(inside.parent / "gone"))
    assert refused.value.status_code == 400
    assert "does not exist" in refused.value.detail


@pytest.mark.asyncio
async def test_late_interaction_status_answers_404_for_an_allowed_but_unindexed_repository(roots, monkeypatch):
    inside, _ = roots
    monkeypatch.setattr(core_lifecycle, "require_repository_by_path",
                        AsyncMock(side_effect=LookupError(f"Repository is not indexed: {inside}")))
    with pytest.raises(HTTPException) as missing:
        await core_lifecycle.late_interaction_status(repo_path=str(inside))
    assert missing.value.status_code == 404
    assert "not indexed" in missing.value.detail


@pytest.mark.asyncio
async def test_late_interaction_status_reports_an_indexed_repository(roots, monkeypatch):
    inside, _ = roots
    repository = object()
    monkeypatch.setattr(core_lifecycle, "require_repository_by_path", AsyncMock(return_value=repository))
    collect = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(core_lifecycle, "collect_late_interaction_operator_status", collect)
    assert await core_lifecycle.late_interaction_status(repo_path=str(inside)) == {"status": "ok"}
    collect.assert_awaited_once_with(repository)


@pytest.mark.parametrize("router", [coupling, graph_v2, impact, remediation])
def test_insight_routers_refuse_a_path_outside_the_roots_with_400(router, roots):
    inside, outside = roots
    assert router._safe_resolve(str(inside)) == inside.resolve()
    with pytest.raises(HTTPException) as refused:
        router._safe_resolve(str(outside))
    assert refused.value.status_code == 400


@pytest.mark.asyncio
async def test_core_retrieval_routes_refuse_a_path_outside_the_roots_with_400(roots, monkeypatch):
    _, outside = roots
    monkeypatch.setattr(core_retrieval, "repository_authority", AsyncMock())
    monkeypatch.setattr(core_retrieval, "_agent_v2_mode", lambda: "off")
    calls = (
        core_retrieval.index_repository(IndexRequest(repo_path=str(outside)), principal=None),
        core_retrieval.analyze_change_impact(ImpactRequest(change_request="x", repo_path=str(outside)),
                                             principal=None),
        core_retrieval.related_files_endpoint(RelatedRequest(file_path="a.py", repo_path=str(outside)),
                                              principal=None),
        core_retrieval._impact_v2(ImpactRequest(change_request="x", repo_path=str(outside))),
    )
    for call in calls:
        with pytest.raises(HTTPException) as refused:
            await call
        assert refused.value.status_code == 400
