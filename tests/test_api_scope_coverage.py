"""Exercise published API authorization dependencies without running their handlers.

The previous closure/qualname inspection saw no routes under deferred router
inclusion. OpenAPI is the public inventory; route declarations supply the real
request signature and dependencies to a safe, handler-free HTTP probe app.
"""
from __future__ import annotations

import inspect
import sys
from unittest.mock import patch

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from brain.auth.principals import mint_credential
from tests import test_auth_boundary_contracts as auth_fixtures

credential_store = auth_fixtures.credential_store

MUTATING = {"post", "put", "delete", "patch"}
READ_DOMAINS = (
    "audit", "autonomy", "control", "core", "coupling", "drift", "execution", "experiments",
    "freshness", "graph", "harness", "impact", "improvement", "jobs", "lab", "ledger", "operations",
    "portfolio", "principals", "remediation", "routing", "setup", "shadow", "workspace",
)
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
                    assert identifier not in declarations or declarations[identifier] is route, identifier
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
async def test_published_key_routes_reject_credentials_without_any_scope(credential_store):
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
async def test_mutating_key_routes_reject_read_only_credentials(credential_store):
    published = _published_protected_operations()
    operations = [(method, path, route) for method, path, route in published
                  if method in MUTATING and (method, path) not in READ_ONLY_POSTS]
    assert operations
    assert READ_ONLY_POSTS <= {(method, path) for method, path, _ in published}
    async with credential_store() as session, session.begin():
        key, _ = await mint_credential(session, name="read-only-route-probe",
                                       scopes=[f"{domain}:read" for domain in READ_DOMAINS])
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
