"""L2/L4 endpoint contracts: schemas, limit caps, approve/reject flow. No Postgres required."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

from brain.memory.consolidation import EpisodeStateError

client = TestClient(app, raise_server_exceptions=False)
HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def api_key_env():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        yield mock_settings


class _Session:
    def __init__(self):
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


def test_get_episodes_caps_limit():
    session = _Session()
    with patch("apps.api.routers.core.async_session_factory", return_value=session):
        assert client.get("/episodes?limit=200", headers=HEADERS).status_code == 200
        assert client.get("/episodes?limit=201", headers=HEADERS).status_code == 422
        assert client.get("/episodes?limit=0", headers=HEADERS).status_code == 422
    assert len(session.statements) == 1


def test_get_episodes_rejects_unknown_status():
    assert client.get("/episodes?status=bogus", headers=HEADERS).status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/episodes/search", {"query": ""}),
        ("/episodes/search", {"query": "x", "limit": 51}),
        ("/skills", {"description": "missing name"}),
        ("/skills", {"name": "n", "description": "d", "confidence": 1.5}),
        ("/skills", {"name": "n", "description": "d", "triggers": "not-a-list"}),
        ("/skills/match", {"query": "x", "limit": 21}),
        ("/skills/match", {}),
    ],
)
def test_memory_endpoints_validate_bodies(path, body):
    assert client.post(path, json=body, headers=HEADERS).status_code == 422


def test_approve_pending_episode_promotes():
    approve = AsyncMock(return_value={"episode_id": 3, "status": "promoted", "learning_id": 9})
    with patch("brain.memory.consolidation.approve_episode", approve):
        response = client.post("/episodes/3/approve", json={"reason": "verified"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"episode_id": 3, "status": "promoted", "learning_id": 9}
    approve.assert_awaited_once_with(3, "verified")


def test_reject_pending_episode_without_body():
    reject = AsyncMock(return_value={"episode_id": 4, "status": "rejected", "learning_id": None})
    with patch("brain.memory.consolidation.reject_episode", reject):
        response = client.post("/episodes/4/reject", headers=HEADERS)
    assert response.status_code == 200
    reject.assert_awaited_once_with(4, None)


@pytest.mark.parametrize(
    ("error", "status"),
    [(LookupError("Episode 5 not found"), 404), (EpisodeStateError("Episode 5 is promoted, not pending"), 409)],
)
def test_episode_decision_maps_errors(error, status):
    with patch("brain.memory.consolidation.approve_episode", AsyncMock(side_effect=error)):
        assert client.post("/episodes/5/approve", headers=HEADERS).status_code == status


def test_episode_decision_requires_write_scope_key():
    assert client.post("/episodes/5/approve").status_code == 401


def test_get_skills_caps_limit():
    with patch("brain.memory.skill_store.list_skills", new=AsyncMock(return_value=[])):
        assert client.get("/skills?limit=200", headers=HEADERS).status_code == 200
        assert client.get("/skills?limit=201", headers=HEADERS).status_code == 422
        assert client.get("/skills?limit=0", headers=HEADERS).status_code == 422


def test_skills_match_forwards_repo_path():
    match = AsyncMock(return_value=[])
    with patch("brain.memory.skill_store.match_skills", new=match):
        response = client.post(
            "/skills/match",
            json={"query": "deploy", "repo_path": "/repos/x", "limit": 7},
            headers=HEADERS,
        )
    assert response.status_code == 200
    match.assert_awaited_once_with("deploy", repo_scope="/repos/x", limit=7)


def test_skills_match_rejects_oversized_repo_path():
    response = client.post(
        "/skills/match",
        json={"query": "deploy", "repo_path": "x" * 1025},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_skills_conflict_maps_to_409():
    from brain.memory.skill_store import SkillConflictError

    create = AsyncMock(side_effect=SkillConflictError("Skill 'dup' already exists"))
    with patch("brain.memory.skill_store.create_skill", new=create):
        response = client.post(
            "/skills",
            json={"name": "dup", "description": "d"},
            headers=HEADERS,
        )
    assert response.status_code == 409
