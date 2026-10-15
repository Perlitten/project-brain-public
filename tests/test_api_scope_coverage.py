"""Exercise published API authorization dependencies without running their handlers.

The previous closure/qualname inspection saw no routes under deferred router
inclusion. OpenAPI is the public inventory; route declarations supply the real
request signature and dependencies to a safe, handler-free HTTP probe app.
"""
from __future__ import annotations

import inspect
import sys
from types import ModuleType
from unittest.mock import patch

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from apps.api.auth import require_api_key, require_scope
from brain.auth.principals import ResolvedPrincipal, mint_credential
from tests import test_auth_boundary_contracts as auth_fixtures

credential_store = auth_fixtures.credential_store

MUTATING = {"post", "put", "delete", "patch"}
# POST is also used for these documented queries/verification operations.
READ_ONLY_POSTS = {
    ("post", "/episodes/search"),
    ("post", "/skills/match"),
    ("post", "/api/setup/verify-agent"),
    ("post", "/api/web-login/verify"),
}
# Public health/version discovery does not authenticate. Other published
# operations default to protected, including a future route that forgets to
# declare any auth header or dependency.
PUBLIC_OPERATIONS = {("get", "/health"), ("get", "/ready"), ("get", "/api/version")}
# Separate transport policies have dedicated HTTP/storage tests. Keep the list
# explicit: a new endpoint cannot silently inherit a whole-tag exemption.
SEPARATE_OPERATIONS = {
    ("get", "/team/projects/{project_id}/tasks"): "live team membership/project policy; test_team_transport/foundations",
    ("post", "/team/projects/{project_id}/tasks"): "live team membership/project policy; test_team_transport/foundations",
    ("post", "/team/projects/{project_id}/tasks/{task_id}/runs"): "task-bound delegation; test_team_transport/foundations",
    ("post", "/v1/intake/capture"): "handler-level bridge auth; test_telegram_bridge_auth",
    ("post", "/v1/assistant/reply"): "handler-level bridge auth; test_telegram_bridge_auth",
    ("post", "/webhooks/git-merge"): "webhook secret policy; test_worker_scheduler",
}


def _published_protected_operations():
    declarations = {}
    for name, module in tuple(sys.modules.items()):
        if not name.startswith("apps.api.routers."):
            continue
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            for route in router.routes:
                if isinstance(route, APIRoute):
                    identifier = route.operation_id or route.unique_id
                    previous = declarations.get(identifier)
                    if previous is not None:
                        # include_router can clone an APIRoute. Keep the
                        # declaration containing inherited dependencies, and
                        # fail explicitly on ambiguous, non-nested policies.
                        assert previous.endpoint is route.endpoint, identifier
                        if all(dep in previous.dependencies for dep in route.dependencies):
                            continue
                        assert all(dep in route.dependencies for dep in previous.dependencies), identifier
                    declarations[identifier] = route
    operations = []
    for path, item in app.openapi()["paths"].items():
        for method, operation in item.items():
            if method not in MUTATING | {"get", "head", "options"}:
                continue
            if (method, path) in PUBLIC_OPERATIONS or (method, path) in SEPARATE_OPERATIONS:
                continue
            assert operation["operationId"] in declarations, (method, path, "unmapped public operation")
            operations.append((method, path, declarations[operation["operationId"]]))
    assert {("post", "/index"), ("post", "/search"), ("get", "/repositories")} <= {
        (method, path) for method, path, _ in operations
    }, "The inventory must contain the published core endpoints, not pass on an empty route list"
    return operations


def _safe_endpoint(original):
    async def reached_handler(**kwargs):
        return {"authorization_probe_reached_handler": True}

    # Preserve the public request/dependency contract, never the handler body.
    # Resolving annotations here avoids evaluating router-local types in this
    # test module's globals. This changes only the test function's signature.
    reached_handler.__signature__ = inspect.signature(original, eval_str=True)
    return reached_handler


def _probe_app(operations):
    probe = FastAPI()
    for method, path, route in operations:
        probe.add_api_route(path, _safe_endpoint(route.endpoint), methods=[method.upper()],
                            dependencies=route.dependencies, response_model=None)
    return probe


@pytest.mark.asyncio
async def test_published_key_routes_reject_credentials_without_any_scope(credential_store, monkeypatch):
    # Model FastAPI versions that copy aggregate routes, even when the current
    # version defers inclusion. These remain real public APIRoute declarations.
    from apps.api.routers import core_retrieval
    copied = APIRouter()
    for route in core_retrieval.router.routes:
        if isinstance(route, APIRoute):
            copied.add_api_route(route.path, route.endpoint, methods=route.methods,
                                 operation_id=route.operation_id or route.unique_id,
                                 dependencies=route.dependencies, response_model=None)
    module = ModuleType("apps.api.routers.scope_gate_copied_control")
    module.router = copied
    monkeypatch.setitem(sys.modules, module.__name__, module)
    operations = _published_protected_operations()
    async with credential_store() as session, session.begin():
        key, _ = await mint_credential(session, name="unscoped-route-probe", scopes=[])
    failures = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(_probe_app(operations), raise_app_exceptions=False),
                                 base_url="http://scope-probe") as client:
        for method, path, _ in operations:
            response = await client.request(method, path, headers={"X-API-Key": key}, json={})
            if response.status_code != 403:
                failures.append((method, path, response.status_code))
    assert not failures, f"routes allowing an unscoped caller past authorization: {failures}"


@pytest.mark.asyncio
async def test_mutating_key_routes_reject_read_only_credentials(credential_store, monkeypatch):
    published = _published_protected_operations()
    operations = [(method, path, route) for method, path, route in published
                  if method in MUTATING and (method, path) not in READ_ONLY_POSTS]
    assert operations
    assert READ_ONLY_POSTS <= {(method, path) for method, path, _ in published}
    # Discover read grants through the principal's public scope protocol.
    # This phase only builds input: authorization assertions below use the
    # original, unpatched method and an actual persisted credential.
    read_grants = set()
    observed_scopes = set()
    def discover_reads(principal, scope):
        observed_scopes.add(scope)
        if scope.endswith(":read"):
            read_grants.add(scope)
            return True
        return False

    async with credential_store() as session, session.begin():
        discovery_key, _ = await mint_credential(session, name="read-scope-discovery", scopes=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(_probe_app(published), raise_app_exceptions=False),
                                 base_url="http://scope-probe") as client:
        with monkeypatch.context() as discovery:
            discovery.setattr(ResolvedPrincipal, "has_scope", discover_reads)
            for method, path, _ in operations:
                await client.request(method, path, headers={"X-API-Key": discovery_key}, json={})
            for method, path in sorted(READ_ONLY_POSTS):
                await client.request(method, path, headers={"X-API-Key": discovery_key}, json={})
    assert all(scope.rpartition(":")[2] in {"read", "write"} for scope in observed_scopes), observed_scopes
    async with credential_store() as session, session.begin():
        key, _ = await mint_credential(session, name="read-only-route-probe", scopes=sorted(read_grants))
    failures = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(_probe_app(published), raise_app_exceptions=False),
                                 base_url="http://scope-probe") as client:
        for method, path, _ in operations:
            response = await client.request(method, path, headers={"X-API-Key": key}, json={})
            if response.status_code != 403:
                failures.append((method, path, response.status_code))
        for method, path in sorted(READ_ONLY_POSTS):
            response = await client.request(method, path, headers={"X-API-Key": key}, json={})
            assert response.status_code in {200, 422}, (method, path, response.status_code)
    assert not failures, f"write routes allowing a read-only caller past authorization: {failures}"


@pytest.mark.asyncio
async def test_write_gate_detects_missing_write_scope_in_a_new_domain(credential_store, monkeypatch):
    # No GET endpoint and no manually registered scope domain: a router-level
    # read check must not mask the missing write check on this POST operation.
    router = APIRouter(dependencies=[Depends(require_api_key), Depends(require_scope("new-domain:read"))])

    @router.post("/new-domain/mutate")
    async def mutate():
        pytest.fail("the gate must never execute application handlers")

    route = router.routes[0]
    monkeypatch.setattr(sys.modules[__name__], "_published_protected_operations",
                        lambda: [("post", route.path, route)])
    monkeypatch.setattr(sys.modules[__name__], "READ_ONLY_POSTS", set())
    with pytest.raises(AssertionError, match="write routes allowing a read-only caller"):
        await test_mutating_key_routes_reject_read_only_credentials(credential_store, monkeypatch)
