from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from brain.embeddings.constants import VectorSearchStatus
from brain.search.code_search import search_code

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def api_key_env():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        mock_settings.ENVIRONMENT = "test"
        yield


@pytest.mark.asyncio
async def test_search_code_unknown_repo_path_does_not_fall_back_to_global():
    lookup = AsyncMock(return_value=None)
    with (
        patch("brain.search.code_search.get_repository_by_path", lookup),
        patch("brain.search.code_search.async_session_factory") as session_factory,
    ):
        result = await search_code("telegram digest", repo_path="/indexed/missing-repo")

    lookup.assert_awaited_once_with("/indexed/missing-repo")
    session_factory.assert_not_called()
    assert result["files"] == []
    assert result["symbols"] == []
    assert result["chunks"] == []
    assert result["vector_status"] == VectorSearchStatus.DEGRADED
    assert result["repository_scope"] == {
        "requested_path": "/indexed/missing-repo",
        "found": False,
    }


@pytest.mark.asyncio
async def test_search_code_returns_repository_index_provenance():
    repository = type(
        "RepositoryRecord",
        (),
        {
            "id": 9,
            "path": "/indexed/Eunoia",
            "name": "Eunoia",
            "last_indexed_commit": "41b3a10b2675afea0ce7316485b9a72bf86d9725",
            "indexing_status": "completed",
        },
    )()
    lookup = AsyncMock(return_value=repository)
    session_factory = MagicMock()
    session_factory.return_value.__aenter__.return_value = AsyncMock()
    with (
        patch("brain.search.code_search.get_repository_by_path", lookup),
        patch("brain.search.code_search.extract_keywords", return_value=[]),
        patch(
            "brain.search.code_search.assess_repository_freshness",
            AsyncMock(return_value={"status": "current"}),
        ),
        patch(
            "brain.search.code_search.async_session_factory",
            session_factory,
        ),
        patch(
            "brain.search.code_search.vector_search_chunks",
            AsyncMock(
                return_value=SimpleNamespace(
                    status=VectorSearchStatus.OK,
                    matches=[],
                )
            ),
        ),
    ):
        result = await search_code("journal capture", repo_path="/indexed/Eunoia")

    assert result["repository_scope"] == {
        "requested_path": "/indexed/Eunoia",
        "found": True,
        "repository_id": 9,
        "repository_path": "/indexed/Eunoia",
        "repository_name": "Eunoia",
        "last_indexed_commit": "41b3a10b2675afea0ce7316485b9a72bf86d9725",
        "indexing_status": "completed",
        "freshness": {"status": "current"},
    }


def test_search_endpoint_forwards_repo_path_to_hybrid_search(api_key_env):
    search_result = {
        "files": [],
        "symbols": [],
        "chunks": [],
        "vector_status": VectorSearchStatus.OK,
        "repository_scope": {
            "requested_path": "/indexed/forex-bot",
            "found": True,
            "repository_id": 7,
            "repository_path": "/indexed/forex-bot",
            "repository_name": "forex-bot",
            "last_indexed_commit": "d1ee6b44620a0000000000000000000000000000",
            "indexing_status": "completed",
        },
    }
    search_mock = AsyncMock(return_value=search_result)

    with patch("apps.api.routers.core_retrieval.hybrid_search_code", search_mock):
        response = client.post(
            "/search",
            json={"query": "telegram digest", "limit": 7, "repo_path": "/indexed/forex-bot"},
            headers={"X-API-Key": "test-secret-key"},
        )

    assert response.status_code == 200
    assert response.json()["repository_scope"]["repository_id"] == 7
    search_mock.assert_awaited_once_with("telegram digest", limit=7, repo_path="/indexed/forex-bot")


@pytest.mark.asyncio
async def test_remote_mcp_search_uses_repo_path_payload():
    from apps.mcp_server import remote_server

    post_mock = AsyncMock(return_value={"files": []})
    with patch.object(remote_server, "_post", post_mock):
        result = await remote_server.search_code("risk gate", repo_path="/indexed/forex-bot")

    assert result == {"files": []}
    post_mock.assert_awaited_once_with(
        "/search",
        {"query": "risk gate", "limit": 5, "repo_path": "/indexed/forex-bot"},
    )


@pytest.mark.asyncio
async def test_remote_mcp_search_defaults_to_configured_repo():
    from apps.mcp_server import remote_server

    post_mock = AsyncMock(return_value={"files": []})
    with (
        patch.object(remote_server, "DEFAULT_REPO", "/indexed/default-repo"),
        patch.object(remote_server, "_post", post_mock),
    ):
        await remote_server.search_code("risk gate")

    post_mock.assert_awaited_once_with(
        "/search",
        {"query": "risk gate", "limit": 5, "repo_path": "/indexed/default-repo"},
    )


@pytest.mark.asyncio
async def test_remote_diff_review_defaults_to_configured_repo():
    from apps.mcp_server import remote_server

    post_mock = AsyncMock(return_value={"status": "completed"})
    with (
        patch.object(remote_server, "DEFAULT_REPO", "/indexed/default-repo"),
        patch.object(remote_server, "_post", post_mock),
    ):
        result = await remote_server.review_diff(head="current")

    assert result == {"status": "completed"}
    post_mock.assert_awaited_once_with(
        "/diff-review",
        {
            "base": None,
            "head": "current",
            "repo_path": "/indexed/default-repo",
        },
    )


@pytest.mark.asyncio
async def test_remote_diff_review_forwards_explicit_repo_scope():
    from apps.mcp_server import remote_server

    post_mock = AsyncMock(return_value={"status": "completed"})
    with patch.object(remote_server, "_post", post_mock):
        await remote_server.review_diff(
            base="origin/main",
            head="current",
            repo_path="/indexed/Eunoia",
        )

    post_mock.assert_awaited_once_with(
        "/diff-review",
        {
            "base": "origin/main",
            "head": "current",
            "repo_path": "/indexed/Eunoia",
        },
    )


@pytest.mark.asyncio
async def test_remote_record_decision_forwards_repo_scope():
    from apps.mcp_server import remote_server

    post_mock = AsyncMock(return_value={"status": "success"})
    with patch.object(remote_server, "_post", post_mock):
        await remote_server.record_decision(
            "Scoped decision",
            repo_path="/indexed/Eunoia",
        )

    payload = post_mock.await_args.args[1]
    assert payload["title"] == "Scoped decision"
    assert payload["repo_path"] == "/indexed/Eunoia"


@pytest.mark.asyncio
async def test_remote_record_rule_forwards_repo_scope():
    from apps.mcp_server import remote_server

    post_mock = AsyncMock(return_value={"status": "success"})
    with patch.object(remote_server, "_post", post_mock):
        await remote_server.record_rule(
            "Scoped rule",
            repo_path="/indexed/Eunoia",
        )

    post_mock.assert_awaited_once()
    assert post_mock.await_args.args[0] == "/rules"
    payload = post_mock.await_args.args[1]
    assert payload["name"] == "Scoped rule"
    assert payload["repo_path"] == "/indexed/Eunoia"
