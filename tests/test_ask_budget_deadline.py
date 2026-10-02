import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.api.routers.core import _ask_v2
from apps.api.schemas import AskRequest
from brain.config.settings import settings


def _size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@pytest.mark.asyncio
async def test_ask_returns_bounded_evidence_only_answer_for_stale_context():
    runtime_builder = SimpleNamespace(
        build=AsyncMock(return_value={"status": "stale_blocked", "missing": ["current_source_required"]})
    )
    with patch("apps.api.routers.core.RuntimeContextBuilder", return_value=runtime_builder):
        payload = await _ask_v2(AskRequest(query="change the endpoint", repo_path="/app"))

    assert payload["status"] == "partial"
    assert "current_source_required" in payload["degraded"]
    assert _size(payload) <= settings.AGENT_ASK_OUTPUT_MAX_BYTES


@pytest.mark.asyncio
async def test_ask_does_not_start_synthesis_after_its_total_deadline():
    async def late_runtime(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return {"status": "ok", "candidates": []}

    runtime_builder = SimpleNamespace(build=late_runtime)
    with (
        patch("apps.api.routers.core.RuntimeContextBuilder", return_value=runtime_builder),
        patch("apps.api.routers.core.settings.AGENT_ASK_DEADLINE_S", 0.001),
        patch("apps.api.routers.core.get_model_router") as router,
    ):
        payload = await _ask_v2(AskRequest(query="where is the endpoint", repo_path="/app"))

    assert payload["status"] == "partial"
    assert payload["degraded"] == ["ask_deadline_exceeded"]
    router.assert_not_called()
    assert _size(payload) <= settings.AGENT_ASK_OUTPUT_MAX_BYTES
