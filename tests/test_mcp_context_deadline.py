"""MCP context budgets include assembly, without disguising denial or failure."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from mcp import Client
from pydantic import ValidationError

from apps.api.routers import core_retrieval
from apps.api.schemas import ContextRequest
from apps.mcp_server import remote_server


@pytest.mark.parametrize('deadline', [0, -1, 31, float('nan'), float('inf'), -float('inf')])
def test_context_budget_rejects_invalid_deadlines(deadline):
    with pytest.raises(ValidationError):
        ContextRequest(task_description='locate code', deadline_s=deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize(('requested', 'configured'), [(0.02, 30), (30, 0.02)])
async def test_total_context_budget_cancels_post_retrieval_work(monkeypatch, requested, configured):
    stopped = asyncio.Event()

    async def build(*args, **kwargs):
        # Retrieval finished; source/memory assembly is still waiting.
        await asyncio.sleep(0)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(core_retrieval, 'repository_authority', AsyncMock())
    monkeypatch.setattr(core_retrieval, 'RuntimeContextBuilder', lambda: SimpleNamespace(build=build))
    monkeypatch.setattr(core_retrieval.settings, 'AGENT_CONTEXT_DEADLINE_S', configured)
    result = await asyncio.wait_for(core_retrieval.build_task_context(
        ContextRequest(task_description='locate code', repo_path='/repo', deadline_s=requested),
        principal=SimpleNamespace(),
    ), timeout=1)
    assert stopped.is_set()
    assert result['status'] == 'partial'
    assert result['missing'] == ['context_deadline_exceeded']
    assert result['freshness']['reason'] == 'context_deadline_exceeded'


@pytest.mark.asyncio
async def test_context_budget_does_not_turn_revocation_into_partial_success(monkeypatch):
    monkeypatch.setattr(core_retrieval, 'repository_authority', AsyncMock(side_effect=HTTPException(403, 'revoked')))
    builder = AsyncMock()
    monkeypatch.setattr(core_retrieval, 'RuntimeContextBuilder', builder)
    with pytest.raises(HTTPException) as error:
        await core_retrieval.build_task_context(
            ContextRequest(task_description='locate code', deadline_s=0.01), principal=SimpleNamespace(),
        )
    assert error.value.status_code == 403
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_context_survives_old_nine_second_transport_limit(monkeypatch):
    app = FastAPI()

    @app.post('/context')
    async def context(body: ContextRequest):
        assert body.deadline_s == 30 and not body.persist
        # Represents cumulative auth/assembly/observation delay from the load
        # reproduction. Real ASGITransport + asyncio timeout, no mocked clock.
        await asyncio.sleep(9.1)
        return {'status': 'ok', 'slices': [{'path': 'app.py'}]}

    monkeypatch.setattr(remote_server, '_asgi_app', app)
    monkeypatch.setattr(remote_server, '_caller_credential', lambda: 'fixture')
    async with Client(remote_server.mcp, mode='legacy') as client:
        result = await client.call_tool('prepare_task_context', {'task_description': 'locate code'})
    assert not result.is_error, str(result)
    assert 'app.py' in str(result)


@pytest.mark.asyncio
async def test_in_process_transport_timeout_still_fails_and_cancels(monkeypatch):
    app = FastAPI()
    stopped = asyncio.Event()

    @app.post('/context')
    async def context():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(remote_server, '_asgi_app', app)
    monkeypatch.setattr(remote_server, '_caller_credential', lambda: 'fixture')
    result = await remote_server._request('POST', '/context', {}, timeout=0.02)
    assert result['_bridge_error'] and 'TimeoutError' in result['error']
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_ask_fallback_requests_the_same_bounded_context(monkeypatch):
    requests = []

    async def post(path, payload, timeout=120):
        requests.append((path, payload, timeout))
        return {'status': 'failed', 'error': 'fixture unavailable', '_bridge_error': True}

    monkeypatch.setattr(remote_server, '_post', post)
    async with Client(remote_server.mcp, mode='legacy') as client:
        result = await client.call_tool('ask_project', {'query': 'locate code'})
    assert result.is_error
    assert requests[1][0] == '/context'
    assert requests[1][1]['deadline_s'] == 30
    assert requests[1][2] == 35
