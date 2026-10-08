from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.api.request_observation import classify_result
from apps.api.routers.core_retrieval import build_task_context, search_code_endpoint
from apps.api.schemas import ContextRequest, SearchRequest


@pytest.mark.parametrize("result,operation,expected", [
    ({"status": "partial", "slices": []}, "context", "partial"),
    ({"status": "ok", "slices": [{"id": "a"}]}, "context", "success"),
    ({"status": "ok", "slices": []}, "context", "empty"),
    ({"critic_status": "LOW_CONFIDENCE", "path": "/saved.md"}, "context", "partial"),
    ({"results": [], "degraded": ["locator_error:TimeoutError"]}, "search", "error"),
    ({"results": []}, "search", "empty"),
])
def test_outcomes_distinguish_delivery_from_empty_partial_and_error(result, operation, expected):
    assert classify_result(result, operation) == expected


@pytest.mark.asyncio
async def test_non_persistent_context_records_partial_and_preserves_response():
    payload = {"status": "partial", "slices": []}
    request = SimpleNamespace(state=SimpleNamespace(principal=SimpleNamespace(name="owner"), request_id="trace"))
    with patch("apps.api.routers.core_retrieval.RuntimeContextBuilder") as builder, patch("apps.api.request_observation.observe_http_request", new_callable=AsyncMock) as observe:
        builder.return_value.build = AsyncMock(return_value=payload)
        result = await build_task_context(ContextRequest(task_description="query", repo_path="/app", persist=False), request)
    assert result == payload
    observe.assert_awaited_once()
    assert observe.call_args.kwargs["outcome"] == "partial"


@pytest.mark.asyncio
async def test_runtime_context_exception_records_error_even_when_response_is_partial():
    with patch("apps.api.routers.core_retrieval.RuntimeContextBuilder") as builder, patch("apps.api.request_observation.observe_http_request", new_callable=AsyncMock) as observe:
        builder.return_value.build = AsyncMock(side_effect=TimeoutError())
        result = await build_task_context(ContextRequest(task_description="query", repo_path="/app", persist=False), SimpleNamespace())
    assert result["status"] == "partial"
    assert observe.call_args.kwargs["outcome"] == "error"


@pytest.mark.asyncio
async def test_locator_early_return_is_measured():
    payload = {"results": [{"path": "config.py"}]}
    with patch("apps.api.routers.core_retrieval._agent_v2_mode", return_value="on"), patch("apps.api.routers.core_retrieval._compact_locator", new_callable=AsyncMock, return_value=payload), patch("apps.api.request_observation.observe_http_request", new_callable=AsyncMock) as observe:
        result = await search_code_endpoint(SearchRequest(query="config", repo_path="/app", response_mode="locator"), SimpleNamespace())
    assert result == payload
    observe.assert_awaited_once()
    assert observe.call_args.kwargs["outcome"] == "success"
