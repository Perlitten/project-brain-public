"""Read paths that list or fetch by id follow project membership once the team has
members: the repository listing, diff-review results and context packs
(ADR docs/adr/team-collaboration.md)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI

from apps.api import auth, team_access
from apps.api.routers import core_retrieval, web
from brain.config.settings import settings
from brain.database.models import ContextPack, IndexingRun
from tests.test_team_transport import (ADMIN, CONTRIBUTOR, LEGACY, OUTSIDER, READER, add_outsider_and_unattached_repo,
                                       client_for, live_team_state, membership_driven, revoke_everyone)
from tests.test_team_foundations import team_db as team_fixture

team_db = team_fixture
pytestmark = pytest.mark.team_state

READ_SCOPE_TABLES = [ContextPack.__table__, IndexingRun.__table__]
JOBS = {
    "job-a": {"type": "diff_review", "status": "completed", "result": {"repo": "A"},
              "params": {"repository_id": 1, "repo_path": "/synthetic/A", "base": "main", "head": "HEAD"}},
    "job-b": {"type": "diff_review", "status": "completed", "result": {"repo": "B"},
              "params": {"repository_id": 2, "repo_path": "/synthetic/B", "base": "main", "head": "HEAD"}},
    "job-c": {"type": "diff_review", "status": "completed", "result": {"repo": "C"},
              "params": {"repository_id": 3, "repo_path": "/synthetic/C", "base": "main", "head": "HEAD"}},
}


@pytest_asyncio.fixture
async def read_scope_db(team_db, tmp_path):
    """Team fixture plus context packs: 1 in A, 2 in B, 3 unattributed; repository 3 (C) is unattached."""
    factory, a, b = team_db
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: ContextPack.metadata.create_all(sync, tables=READ_SCOPE_TABLES))
    await add_outsider_and_unattached_repo(factory)
    artifact = tmp_path / "pack.md"
    artifact.write_text("# synthetic pack\n", encoding="utf-8")
    now = datetime.now(timezone.utc)
    async with factory() as session, session.begin():
        session.add_all([ContextPack(id=1, task_description="pack A", path=str(artifact), repository_id=1, created_at=now),
                         ContextPack(id=2, task_description="pack B", path=str(artifact), repository_id=2, created_at=now),
                         ContextPack(id=3, task_description="pack none", path=str(artifact), created_at=now)])
    yield factory, a, b, artifact
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: ContextPack.metadata.drop_all(sync, tables=READ_SCOPE_TABLES))


class FakeQueue:
    async def get_job(self, job_id):
        return JOBS.get(job_id)


def read_app(factory, artifact, monkeypatch):
    for module in (auth, team_access, core_retrieval, web):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr(core_retrieval, "assess_repository_freshness", AsyncMock(return_value={}))
    monkeypatch.setattr("brain.workers.queue.queue_for_job", lambda *_, **__: FakeQueue())
    monkeypatch.setattr(web, "resolve_context_pack_file", lambda _path: artifact)
    app = FastAPI()
    app.include_router(core_retrieval.router)
    app.include_router(web.router)
    return app


async def repository_ids(client, headers):
    response = await client.get("/repositories", headers=headers)
    assert response.status_code == 200, response.text
    return [repo["id"] for repo in response.json()["repositories"]]


async def pack_ids(client, headers, **params):
    response = await client.get("/api/web/context-packs", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return sorted(pack["id"] for pack in response.json()["packs"])


@pytest.mark.asyncio
async def test_solo_read_paths_unchanged(read_scope_db, monkeypatch):
    factory, _, _, artifact = read_scope_db
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = read_app(factory, artifact, monkeypatch)
    async with client_for(app) as client:
        for headers in (LEGACY, READER, OUTSIDER):
            assert await repository_ids(client, headers) == [1, 2, 3]
            for job_id in JOBS:
                review = await client.get(f"/diff-review/{job_id}", headers=headers)
                assert review.status_code == 200 and review.json()["status"] == "completed"
            assert (await client.get("/diff-review/missing", headers=headers)).status_code == 404
            assert await pack_ids(client, headers) == [1, 2, 3]
            for pack_id in (1, 2, 3):
                assert (await client.get(f"/api/web/context-packs/{pack_id}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_team_repository_listing_follows_project_membership(read_scope_db, monkeypatch):
    factory, _, _, artifact = read_scope_db
    membership_driven(monkeypatch)
    app = read_app(factory, artifact, monkeypatch)
    async with client_for(app) as client:
        assert await repository_ids(client, READER) == [1]
        assert await repository_ids(client, CONTRIBUTOR) == [1]
        assert await repository_ids(client, OUTSIDER) == []
        # Team administrators see every repository, including unattached ones.
        assert await repository_ids(client, ADMIN) == [1, 2, 3]


@pytest.mark.asyncio
async def test_team_diff_review_result_needs_read_on_the_job_repository(read_scope_db, monkeypatch):
    factory, _, _, artifact = read_scope_db
    membership_driven(monkeypatch)
    app = read_app(factory, artifact, monkeypatch)
    async with client_for(app) as client:
        allowed = await client.get("/diff-review/job-a", headers=READER)
        assert allowed.status_code == 200 and allowed.json()["result"] == {"repo": "A"}
        missing = await client.get("/diff-review/missing", headers=READER)
        # Another project's job and an unattached repository's job look exactly like a missing one.
        for job_id in ("job-b", "job-c"):
            for headers in (READER, CONTRIBUTOR, OUTSIDER):
                hidden = await client.get(f"/diff-review/{job_id}", headers=headers)
                assert hidden.status_code == 404 and hidden.json() == missing.json()
        # A team administrator reads unattached repositories and their own projects (here B), but holds no
        # implicit role in project A: by-id reads follow authorize_repository exactly.
        for job_id, expected in (("job-a", 404), ("job-b", 200), ("job-c", 200)):
            assert (await client.get(f"/diff-review/{job_id}", headers=ADMIN)).status_code == expected


@pytest.mark.asyncio
async def test_team_context_packs_follow_the_pack_repository(read_scope_db, monkeypatch):
    factory, _, _, artifact = read_scope_db
    membership_driven(monkeypatch)
    app = read_app(factory, artifact, monkeypatch)
    async with client_for(app) as client:
        assert await pack_ids(client, READER, repository_id=1) == [1]
        assert (await client.get("/api/web/context-packs", params={"repository_id": 2}, headers=READER)).status_code == 403
        assert (await client.get("/api/web/context-packs", headers=READER)).status_code == 403
        assert await pack_ids(client, ADMIN) == [1, 2, 3]
        own = await client.get("/api/web/context-packs/1", params={"repository_id": 1}, headers=READER)
        assert own.status_code == 200 and own.json()["pack"]["task"] == "pack A"
        # Naming a readable repository in the query no longer unlocks another repository's pack.
        missing = await client.get("/api/web/context-packs/999", params={"repository_id": 1}, headers=READER)
        assert missing.status_code == 404
        for pack_id in (2, 3):
            hidden = await client.get(f"/api/web/context-packs/{pack_id}", params={"repository_id": 1}, headers=READER)
            assert hidden.status_code == 404 and hidden.json() == missing.json()
        assert (await client.get("/api/web/context-packs/1", headers=OUTSIDER)).status_code == 403
        for pack_id, expected in ((1, 404), (2, 200), (3, 200)):
            assert (await client.get(f"/api/web/context-packs/{pack_id}", headers=ADMIN)).status_code == expected


@pytest.mark.asyncio
async def test_team_read_paths_refuse_the_shared_key(read_scope_db, monkeypatch):
    factory, _, _, artifact = read_scope_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = read_app(factory, artifact, monkeypatch)
    async with client_for(app) as client:
        for path, params in (("/repositories", {}), ("/diff-review/job-a", {}),
                             ("/api/web/context-packs", {"repository_id": 1}),
                             ("/api/web/context-packs/1", {"repository_id": 1})):
            denied = await client.get(path, params=params, headers=LEGACY)
            assert denied.status_code == 403, path
            assert "named credential required" in denied.json()["detail"].lower()
