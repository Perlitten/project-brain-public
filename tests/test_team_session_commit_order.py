"""Team writes commit before the client sees the response.

FastAPI runs the exit code of a ``yield`` dependency after the response is sent
unless the dependency is declared with ``scope="function"``. For the team
session that exit code is the COMMIT, so a request created a run credential or
revoked a member, answered 200, and only then committed. A client acting on
the answer at once could hit the old state, and a failed commit was never
reported. Found by the team e2e check (scripts/team_e2e_check.py).
"""
import asyncio
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute

from apps.api.auth import require_principal
from apps.api.routers import team, team_invitations
from brain.auth.principals import ResolvedPrincipal

SESSION_DEPENDENCIES = {team.team_session, team_invitations.invitation_session}


def _walk(dependant):
    for dependency in dependant.dependencies:
        yield dependency
        yield from _walk(dependency)


def test_every_team_route_closes_its_session_before_responding():
    seen = 0
    for router in (team.router, team_invitations.router):
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for dependency in _walk(route.dependant):
                if dependency.call in SESSION_DEPENDENCIES:
                    seen += 1
                    assert dependency.scope == "function", f"{route.path} commits after the response is sent"
    assert seen >= 16


class _Transaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.events.append("commit")


class _Session:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def begin(self):
        return _Transaction(self.events)


def test_commit_happens_before_the_response_body_is_sent():
    events: list[str] = []
    app = FastAPI()
    app.include_router(team.router)
    app.dependency_overrides[require_principal] = lambda: ResolvedPrincipal(id=7, name="alice", kind="human")

    async def list_tasks(session, caller, project_id):
        return []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            events.append(f"status {message['status']}")
        if message["type"] == "http.response.body":
            events.append("response body sent")

    path = f"/team/projects/{uuid.uuid4()}/tasks"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": [],
             "client": ("test", 1), "server": ("test", 80)}
    with (
        patch.object(team, "require_team_storage", lambda: None),
        patch.object(team, "async_session_factory", lambda: _Session(events)),
        patch.object(team.store, "list_tasks", list_tasks),
    ):
        asyncio.run(app(scope, receive, send))

    assert events == ["commit", "status 200", "response body sent"]
