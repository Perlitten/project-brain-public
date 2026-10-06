from unittest.mock import AsyncMock, patch

import pytest

from apps.api.routers.core import build_task_context
from apps.api.schemas import ContextRequest


@pytest.mark.asyncio
async def test_context_persist_false_never_calls_durable_legacy_builder_when_v2_is_off():
    runtime = AsyncMock(return_value={"status": "ok", "slices": []})
    legacy = AsyncMock()
    with (
        patch("apps.api.routers.core_retrieval.RuntimeContextBuilder") as runtime_builder,
        patch("apps.api.routers.core_retrieval.ContextPackBuilder") as legacy_builder,
        patch("apps.api.routers.core.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off"),
    ):
        runtime_builder.return_value.build = runtime
        legacy_builder.return_value.build_context_pack = legacy
        payload = await build_task_context(
            ContextRequest(task_description="locate the API route", repo_path="/app", persist=False)
        )

    assert payload == {"status": "ok", "slices": []}
    runtime.assert_awaited_once()
    legacy.assert_not_awaited()
