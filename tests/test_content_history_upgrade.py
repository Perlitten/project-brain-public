"""Public-startup upgrade and cross-process privacy checks; SQLite only, no services."""
import os
from queue import Queue
from threading import Thread
import sqlite3
import subprocess
import sys
from pathlib import Path


def lite_env(tmp_path):
    return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "BRAIN_STORAGE_MODE": "lite", "BRAIN_LITE_DIR": str(tmp_path), "DATABASE_URL": "",
            "DEFAULT_LLM_PROVIDER": "disabled", "DEFAULT_EMBEDDING_PROVIDER": "local",
            "PROJECT_BRAIN_API_KEY": "", "BRAIN_TEAM_MODE": "false", "ENVIRONMENT": "local",
            "BRAIN_ENV_FILE": str(tmp_path / ".env"), "NVIDIA_API_KEY": "", "OPENAI_API_KEY": ""}


def run_child(source, env):
    result = subprocess.run([sys.executable, "-c", source], env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout



def read_line(process):
    lines = Queue()
    Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    return lines.get(timeout=30).strip()

def test_existing_lite_content_history_survives_repeated_public_startup(tmp_path):
    db = tmp_path / "brain.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript("""
            CREATE TABLE insights (
              id INTEGER PRIMARY KEY, insight_type TEXT, severity TEXT, title TEXT, summary TEXT,
              evidence JSON, recommended_action TEXT, confidence TEXT, dedupe_key TEXT UNIQUE,
              status TEXT, source TEXT, source_model TEXT, engine_version TEXT, occurrence_count INTEGER,
              created_at DATETIME, updated_at DATETIME, last_seen_at DATETIME);
            INSERT INTO insights VALUES (1,'drift','warning','Legacy finding','preserve me','[]',NULL,
              'medium','legacy','new','deterministic',NULL,'old',1,
              '2026-10-01 00:00:00','2026-10-01 00:00:00','2026-10-01 00:00:00');
            CREATE TABLE brain_request_telemetry (
              id INTEGER PRIMARY KEY, created_at DATETIME, operation TEXT, surface TEXT,
              repository_id INTEGER, repository_path TEXT, principal_name TEXT,
              request_id TEXT, outcome TEXT, latency_ms FLOAT);
            INSERT INTO brain_request_telemetry VALUES (1,'2026-10-01 00:00:00','search','mcp',NULL,
              NULL,'synthetic','legacy-request','success',1);
        """)
    source = '''
import asyncio
from sqlalchemy import select
from brain.database.session import init_db, async_session_factory, close_database_connections
from brain.database.models import BrainInsight, BrainRequestTelemetry
from apps.api.telemetry import record_request
async def main():
    await init_db()
    async with async_session_factory() as session:
        finding = await session.get(BrainInsight, 1)
        assert finding.title == "Legacy finding" and finding.status == "new"
        assert finding.dismissed_reason is None and finding.dismissed_evidence is None
        assert finding.dismissed_by is None and finding.dismissed_at is None and finding.dismissed_last_seen_at is None
        request = await session.get(BrainRequestTelemetry, 1)
        assert request.request_id == "legacy-request" and request.query_text is None
    await record_request(operation="search", repository_id=None, repository_path=None,
                         principal_name="synthetic", request_id="after-upgrade", outcome="success", latency_ms=1)
    async with async_session_factory() as session:
        assert (await session.execute(select(BrainRequestTelemetry).where(BrainRequestTelemetry.request_id == "after-upgrade"))).scalars().first() is not None
    await close_database_connections()
asyncio.run(main())
'''
    for _ in range(2):
        run_child(source, lite_env(tmp_path))


def test_disabling_capture_reaches_an_already_running_mcp_recorder(tmp_path):
    env = {**lite_env(tmp_path), "REQUEST_TELEMETRY_CAPTURE_QUERY": "true"}
    worker = '''
import asyncio,sys
from brain.database.session import init_db, close_database_connections
from brain.config.settings import settings
from apps.api.telemetry import record_request
async def main():
    await init_db()
    print("ready",flush=True)
    while (command := await asyncio.to_thread(sys.stdin.readline)).strip():
        assert settings.REQUEST_TELEMETRY_CAPTURE_QUERY is True, "process must retain stale startup state"
        await record_request(operation="search", surface="mcp", repository_id=None, repository_path=None,
                             principal_name="synthetic-mcp", request_id=command.strip(), query_text="synthetic query",
                             outcome="success", latency_ms=1)
        print("recorded",flush=True)
    await close_database_connections()
asyncio.run(main())
'''
    with (tmp_path / "worker.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-u", "-c", worker], env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            assert read_line(process) == "ready"
            for request_id in ["before-disable", "after-disable"]:
                process.stdin.write(request_id + "\n")
                process.stdin.flush()
                assert read_line(process) == "recorded"
                if request_id == "before-disable":
                    run_child('''
import asyncio,httpx
from fastapi import FastAPI
from apps.api.routers.app_settings import router
from brain.database.session import init_db, close_database_connections
async def main():
    await init_db()
    app=FastAPI(); app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
        response=await client.patch("/api/settings",json={"values":{"REQUEST_TELEMETRY_CAPTURE_QUERY":False}})
        assert response.status_code == 200, response.text
    await close_database_connections()
asyncio.run(main())
''', env)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    assert process.returncode == 0, (tmp_path / "worker.log").read_text()
    run_child("""
import asyncio
from brain.database.session import init_db, close_database_connections
from apps.api.telemetry import record_request
async def main():
    await init_db()
    await record_request(operation="search", surface="mcp", repository_id=None, repository_path=None,
                         principal_name="synthetic-mcp", request_id="after-restart", query_text="synthetic query",
                         outcome="success", latency_ms=1)
    await close_database_connections()
asyncio.run(main())
""", env)
    with sqlite3.connect(tmp_path / "brain.sqlite3") as connection:
        rows = dict(connection.execute("SELECT request_id,query_text FROM brain_request_telemetry"))
    assert rows["before-disable"] == "synthetic query"
    assert rows["after-disable"] is None
    assert rows["after-restart"] is None
