"""Offline HTTP/SQL regressions for reasoned findings and project-scoped history."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from apps.api import auth, team_access, telemetry
from apps.api.routers import app_settings, core_status, web
from brain.config.settings import settings
from brain.database.models import BrainInsight, BrainQueryCapturePolicy, BrainRequestTelemetry, Decision, Embedding, File, FileChunk
from brain.insights.evidence import code_target
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import live_team_state
from brain.telemetry import query_capture

team_db = team_fixture
pytestmark = pytest.mark.team_state
STAMP = datetime.now(timezone.utc)
ADMIN = {"X-API-Key": "fixture-4"}
READER = {"X-API-Key": "fixture-1"}


@pytest_asyncio.fixture
async def content_api(team_db, monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(settings, "REQUEST_TELEMETRY_CAPTURE_QUERY", False)
    factory, _, _ = team_db
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(lambda sync: BrainInsight.metadata.create_all(
            sync, tables=[BrainInsight.__table__, BrainRequestTelemetry.__table__, BrainQueryCapturePolicy.__table__,
                          File.__table__, FileChunk.__table__, Embedding.__table__]))
    for module in (auth, team_access, core_status, web, telemetry, query_capture):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    async with factory() as session, session.begin():
        session.add_all([
            BrainQueryCapturePolicy(id=1, enabled=False),
            BrainInsight(id=1, insight_type="drift", severity="warning", title="Bound finding", summary="synthetic",
                         evidence=[{"repository_id": 2, "file": "src/code.py", "line": 10}], dedupe_key="synthetic-bound",
                         status="new", last_seen_at=STAMP),
            BrainInsight(id=2, insight_type="drift", severity="warning", title="Other project", summary="synthetic",
                         evidence=[{"repository_id": 1, "file": "src/code.py", "line": 10}], dedupe_key="synthetic-private",
                         status="new", last_seen_at=STAMP),
            BrainInsight(id=3, insight_type="drift", severity="warning", title="Historical module", summary="synthetic",
                         evidence=[{"module": "src"}], dedupe_key="synthetic-module", status="new", last_seen_at=STAMP),
            File(id=1, repository_id=2, path="src/code.py"),
            File(id=2, repository_id=1, path="src/code.py"),
        ])
        await session.flush()
        session.add_all([
            FileChunk(file_id=1, chunk_index=0, content="synthetic project B code", start_line=1, end_line=20),
            FileChunk(file_id=2, chunk_index=0, content="synthetic project A code", start_line=1, end_line=20),
        ])
    app = FastAPI()
    app.include_router(core_status.router)
    app.include_router(web.router)
    app.include_router(app_settings.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield client, factory


@pytest.mark.asyncio
async def test_dismiss_requires_authority_reason_version_and_verified_authorship(content_api):
    client, factory = content_api
    payload = {"reason": "Synthetic false positive", "expected_last_seen_at": STAMP.isoformat(), "actor": "forged-client"}
    assert (await client.post("/insights/1/dismiss", json=payload, headers=READER)).status_code == 403
    assert (await client.post("/insights/1/dismiss", json={**payload, "reason": " "}, headers=ADMIN)).status_code == 400
    assert (await client.post("/insights/1/dismiss", json={**payload, "expected_last_seen_at": (STAMP - timedelta(seconds=1)).isoformat()}, headers=ADMIN)).status_code == 409
    accepted = await client.post("/insights/1/dismiss", json=payload, headers=ADMIN)
    assert accepted.status_code == 200, accepted.text
    retry = await client.post("/insights/1/dismiss", json={**payload, "reason": "Do not overwrite"}, headers=ADMIN)
    assert retry.status_code == 200 and retry.json()["already_dismissed"]
    async with factory() as session:
        row = await session.get(BrainInsight, 1)
        assert row.status == "dismissed"
        assert row.dismissed_reason == "Synthetic false positive" and row.dismissed_by == "human-4"
        assert row.dismissed_at is not None
        assert row.dismissed_evidence == [{"repository_id": 2, "file": "src/code.py", "line": 10}]
        assert row.dismissed_last_seen_at is not None
    inbox = await client.get("/insights", headers=ADMIN)
    assert inbox.status_code == 200 and 1 not in [row["id"] for row in inbox.json()["insights"]]


@pytest.mark.asyncio
async def test_default_insights_feed_excludes_stale_and_actioned_findings(content_api):
    client, factory = content_api
    async with factory() as session, session.begin():
        session.add_all([
            BrainInsight(id=10, insight_type="task_health", severity="critical", title="High Task Timeout Rate",
                         summary="old stale scan", evidence=[], dedupe_key="synthetic-stale", status="stale", last_seen_at=STAMP),
            BrainInsight(id=11, insight_type="task_health", severity="critical", title="Remediated issue",
                         summary="actioned scan", evidence=[], dedupe_key="synthetic-actioned", status="actioned", last_seen_at=STAMP),
        ])
    feed = await client.get("/insights", headers=ADMIN)
    assert feed.status_code == 200
    ids = {row["id"] for row in feed.json()["insights"]}
    assert 10 not in ids and 11 not in ids
    stale_only = await client.get("/insights?status=stale", headers=ADMIN)
    assert stale_only.status_code == 200 and [row["id"] for row in stale_only.json()["insights"]] == [10]


@pytest.mark.asyncio
async def test_open_code_uses_recorded_repository_and_never_guesses_a_source(content_api):
    client, _ = content_api
    allowed = await client.get("/insights/1/code", headers=ADMIN)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["repository_id"] == 2 and allowed.json()["snapshot"] is True
    assert [chunk["content"] for chunk in allowed.json()["chunks"]] == ["synthetic project B code"]
    # An operator can triage global findings but needs a project role to read code.
    assert (await client.get("/insights/2/code", headers=ADMIN)).status_code == 403
    assert (await client.get("/insights/3/code", headers=ADMIN)).status_code == 404
    assert (await client.get("/insights/1/code", headers=READER)).status_code == 403


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "C:/private", "https://example.invalid/file", "src/../../private", "src/\x00bad"])
def test_file_evidence_refuses_unsafe_paths_without_reading_the_filesystem(path):
    assert code_target([{"repository_id": 1, "file": path}]) is None


@pytest.mark.asyncio
async def test_query_capture_is_opt_in_bounded_and_trace_stays_in_project_scope(content_api):
    client, factory = content_api
    common = dict(operation="search", repository_id=1, repository_path="/synthetic/A", principal_name="human-1", outcome="success", latency_ms=1)
    await telemetry.record_request(**common, request_id="no-capture", query_text="synthetic query must not be stored")
    changed = await client.patch("/api/settings", headers=ADMIN, json={"values": {"REQUEST_TELEMETRY_CAPTURE_QUERY": True}})
    assert changed.status_code == 200, changed.text
    await telemetry.record_request(**common, request_id="captured", query_text="q" * 900)
    async with factory() as session:
        rows = list((await session.execute(select(BrainRequestTelemetry).order_by(BrainRequestTelemetry.id))).scalars())
        assert rows[0].query_text is None and rows[1].query_text == "q" * 500
    own = await client.get("/api/web/requests/captured?repository_id=1", headers=READER)
    assert own.status_code == 200 and own.json()["observations"][0]["query_text"] == "q" * 500
    assert (await client.get("/api/web/requests/captured?repository_id=2", headers=READER)).status_code == 403
    assert (await client.get("/api/web/requests/captured?repository_id=2", headers=ADMIN)).status_code == 404


@pytest.mark.asyncio
async def test_current_decisions_exclude_replaced_and_other_project_rows_before_paging(content_api):
    client, factory = content_api
    async with factory() as session, session.begin():
        session.add_all([Decision(title=f"synthetic {status}", description="synthetic", status=status, repo_path="/synthetic/A")
                         for status in ("accepted", "proposed", "validated_unmerged", "superseded")])
        session.add(Decision(title="synthetic private", description="synthetic", status="accepted", repo_path="/synthetic/B"))
    result = await client.get("/api/web/decisions?status=current&page_size=2", headers=READER)
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 3 and len(result.json()["decisions"]) == 2
    second = await client.get("/api/web/decisions?status=current&page_size=2&page=2", headers=READER)
    statuses = {row["status"] for row in result.json()["decisions"] + second.json()["decisions"]}
    assert statuses == {"accepted", "proposed", "validated_unmerged"}
    replaced = await client.get("/api/web/decisions?status=superseded", headers=READER)
    assert replaced.status_code == 200 and replaced.json()["total"] == 1


@pytest.mark.asyncio
async def test_missing_capture_policy_keeps_metadata_without_recording_query(content_api):
    from sqlalchemy import text
    _, factory = content_api
    async with factory() as session, session.begin():
        await session.execute(text("DROP TABLE brain_query_capture_policy"))
    await telemetry.record_request(operation="search", repository_id=1, repository_path="/synthetic/A",
                                   principal_name="human-1", request_id="policy-unavailable", outcome="success",
                                   latency_ms=1, query_text="synthetic query must not be stored")
    async with factory() as session:
        row = (await session.execute(select(BrainRequestTelemetry).where(BrainRequestTelemetry.request_id == "policy-unavailable"))).scalar_one()
        assert row.query_text is None and row.outcome == "success"
