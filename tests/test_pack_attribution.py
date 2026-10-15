"""A pack has to carry who asked for it.

Adoption per agent is only answerable if the transport that knows the caller
tells the builder. Nothing downstream can recover that later, so these tests
pin the plumbing at each surface — and pin the unattributed spelling, because
two spellings of "unknown caller" would split one agent into two rows.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.api.request_observation import requesting_principal


def test_an_unidentified_caller_has_one_spelling():
    assert requesting_principal(None) == "api:unattributed"
    assert requesting_principal(SimpleNamespace(state=SimpleNamespace())) == "api:unattributed"
    assert requesting_principal(SimpleNamespace(state=SimpleNamespace(principal=None))) == "api:unattributed"


def test_a_named_principal_is_used_verbatim():
    request = SimpleNamespace(state=SimpleNamespace(principal=SimpleNamespace(name="api:ci-bot")))

    assert requesting_principal(request) == "api:ci-bot"


@pytest.mark.asyncio
async def test_http_context_pack_records_the_calling_principal(api_principal):
    from apps.api.routers.core import build_task_context
    from apps.api.schemas import ContextRequest

    builder = AsyncMock(return_value={"status": "ok", "path": "/packs/p.md"})
    request = SimpleNamespace(state=SimpleNamespace(principal=SimpleNamespace(name="api:ci-bot"), request_id="r1"))
    with (
        patch("apps.api.routers.core_retrieval.ContextPackBuilder") as pack_builder,
        patch("apps.api.routers.core_retrieval.resolve_repo_path", return_value="/app"),
        patch("apps.api.routers.core.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off"),
        patch("apps.api.request_observation.observe_http_request", new=AsyncMock()),
    ):
        pack_builder.return_value.build_context_pack = builder
        await build_task_context(
            ContextRequest(task_description="locate the API route", repo_path="/app", persist=True), request,
            principal=api_principal,
        )

    assert builder.await_args.kwargs["consumer"] == "api:ci-bot"


@pytest.mark.asyncio
async def test_mcp_context_pack_is_attributed_to_the_handshaking_client(monkeypatch):
    from apps.mcp_server import server

    builder = AsyncMock(return_value={"status": "ok"})

    class _Builder:
        build_context_pack = builder

    monkeypatch.setattr(server, "ContextPackBuilder", lambda: _Builder())
    monkeypatch.setattr(server.settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    monkeypatch.setattr(server, "_CONNECTED_CLIENT", "Claude Code")
    # The telemetry decorator wraps the tool; keep its write out of the way.
    monkeypatch.setattr("apps.api.telemetry.record_request", AsyncMock())
    monkeypatch.setattr("apps.api.telemetry.repository_id_for_path", AsyncMock(return_value=None))

    await server.prepare_task_context("find the readiness step", repo_path="/app")

    assert builder.await_args.kwargs["consumer"] == "mcp:claude-code"


def test_mcp_principal_falls_back_to_unattributed(monkeypatch):
    from apps.mcp_server import server

    monkeypatch.setattr(server, "_CONNECTED_CLIENT", None)
    assert server._mcp_principal() == "mcp:unattributed"
    # A client name that normalizes to nothing is still an unknown caller.
    monkeypatch.setattr(server, "_CONNECTED_CLIENT", "***")
    assert server._mcp_principal() == "mcp:unattributed"
