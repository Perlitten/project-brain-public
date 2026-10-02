"""Scope coverage: every API-key route resolves a principal AND enforces a

domain scope — mutating routes require a ``{domain}:write`` grant. Prevents a
new or refactored endpoint from silently bypassing the scoped-credential
model."""

from __future__ import annotations

from unittest.mock import patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.routing import APIRoute  # noqa: E402

from apps.api.auth import require_api_key  # noqa: E402

MUTATING = {"POST", "PUT", "DELETE", "PATCH"}


def _dep_calls(route: APIRoute):
    return [dep.call for dep in route.dependant.dependencies]


def _scope_names(call) -> tuple[str, ...]:
    """require_scope(...) returns an inner ``_checker`` closure over scopes."""
    if getattr(call, "__qualname__", "") != "require_scope.<locals>._checker":
        return ()
    for cell in call.__closure__ or ():
        if isinstance(cell.cell_contents, tuple):
            return tuple(str(s) for s in cell.cell_contents)
    return ()


def _api_key_routes() -> list[APIRoute]:
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute) and require_api_key in _dep_calls(route):
            routes.append(route)
    return routes


def test_every_api_key_route_enforces_a_scope():
    """An authenticated route without any scope dep accepts ANY valid key —

    indistinguishable from the anonymous shared key this work replaces."""
    missing = [
        (sorted(route.methods), route.path)
        for route in _api_key_routes()
        if not any(_scope_names(call) for call in _dep_calls(route))
    ]
    assert not missing, f"routes without scope enforcement: {missing}"


def test_mutating_routes_require_write_scope():
    missing = []
    for route in _api_key_routes():
        if not (route.methods & MUTATING):
            continue
        scopes = {s for call in _dep_calls(route) for s in _scope_names(call)}
        if not any(s.endswith(":write") or s == "*" for s in scopes):
            missing.append((sorted(route.methods), route.path, sorted(scopes)))
    assert not missing, f"mutating routes without :write scope: {missing}"
