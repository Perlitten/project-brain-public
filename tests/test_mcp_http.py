"""Native Streamable HTTP ``/mcp``: authentication, session binding and
in-process tool calls that carry the MCP caller's own credential."""

import asyncio
import contextlib
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from apps.api import mcp_http
from apps.mcp_server import remote_server
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings

KEYS = {
    "pbk_alice": ResolvedPrincipal(id=1, name="alice", kind="agent", scopes=frozenset({"core:write"})),
    "pbk_bob": ResolvedPrincipal(id=2, name="bob", kind="agent", scopes=frozenset({"core:read"})),
}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
STREAM_HEADERS = {"Accept": "application/json, text/event-stream"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _app(public_url: str | None):
    """A stand-in API: one route the rules resource calls, echoing the key it got."""
    app = FastAPI()

    @app.get("/rules")
    async def rules(request: Request):
        return [{"name": "seen-key", "status": "active", "key": request.headers.get("x-api-key")}]

    endpoint = mcp_http.install(app, public_url=public_url)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with endpoint.serving():
            yield

    app.router.lifespan_context = lifespan
    return app


@contextlib.contextmanager
def _serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def keyed(monkeypatch):
    """A Brain with keys configured (not dev-open) and two known principals."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "legacy-test-key")

    async def resolve(raw):
        return KEYS.get(raw)

    monkeypatch.setattr(mcp_http, "resolve_api_key", resolve)
    monkeypatch.setattr(remote_server, "_asgi_app", None)


@pytest.fixture
def base_url(keyed):
    port = _free_port()
    with _serve(_app(public_url=None), port) as url:
        yield url


@pytest.fixture
def oauth_base_url(keyed):
    port = _free_port()
    with _serve(_app(public_url=f"http://localhost:{port}"), port) as url:
        yield url, f"http://localhost:{port}"


def test_public_base_url_prefers_explicit_url():
    assert mcp_http.public_base_url("https://brain.example.com/", "other") == "https://brain.example.com"
    assert mcp_http.public_base_url("", "brain.example.com") == "https://brain.example.com"
    assert mcp_http.public_base_url(None, " ") is None


def test_rejects_missing_and_unknown_keys(base_url):
    with httpx.Client(base_url=base_url) as client:
        assert client.post("/mcp", json=INIT, headers=STREAM_HEADERS).status_code == 401
        bad = client.post("/mcp", json=INIT, headers={**STREAM_HEADERS, "X-API-Key": "pbk_nobody"})
        assert bad.status_code == 401


def test_unauthorized_response_points_at_oauth_metadata(oauth_base_url):
    base_url, public = oauth_base_url
    with httpx.Client(base_url=base_url) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert "none" in metadata["token_endpoint_auth_methods_supported"]
        assert "none" in metadata["revocation_endpoint_auth_methods_supported"]
        denied = client.post("/mcp", json=INIT, headers=STREAM_HEADERS)
        assert denied.status_code == 401
        assert f'resource_metadata="{public}/.well-known/oauth-protected-resource/mcp"' in denied.headers[
            "www-authenticate"
        ]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == f"{public}/mcp"
        assert metadata["authorization_servers"] == [f"{public}/"]
        server_metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert server_metadata["registration_endpoint"] == f"{public}/register"
        assert "S256" in server_metadata["code_challenge_methods_supported"]


def test_session_is_bound_to_the_principal_that_opened_it(base_url):
    with httpx.Client(base_url=base_url) as client:
        opened = client.post("/mcp", json=INIT, headers={**STREAM_HEADERS, "Authorization": "Bearer pbk_alice"})
        assert opened.status_code == 200
        session_id = opened.headers["mcp-session-id"]
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        hijack = client.post(
            "/mcp",
            json=ping,
            headers={**STREAM_HEADERS, "X-API-Key": "pbk_bob", "mcp-session-id": session_id},
        )
        assert hijack.status_code == 404


async def _read_rules(url: str, headers: dict) -> list:
    import json

    async with httpx.AsyncClient(headers=headers, timeout=30) as http_client:
        async with streamable_http_client(f"{url}/mcp", http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert any(tool.name == "search_code" for tool in tools.tools)
                result = await session.read_resource("brain://rules")
                return json.loads(result.contents[0].text)


def test_tools_run_in_process_with_the_callers_own_key(base_url):
    rows = asyncio.run(_read_rules(base_url, {"Authorization": "Bearer pbk_alice"}))
    assert rows == [{"name": "seen-key", "status": "active", "key": "pbk_alice"}]
    rows = asyncio.run(_read_rules(base_url, {"X-API-Key": "pbk_bob"}))
    assert rows[0]["key"] == "pbk_bob"


def test_in_process_call_without_an_http_caller_fails_closed(monkeypatch):
    monkeypatch.setattr(remote_server, "_asgi_app", FastAPI())
    result = asyncio.run(remote_server._get("/rules"))
    assert result["status"] == "failed"
    assert "no authenticated MCP caller" in result["error"]
