import pytest


@pytest.mark.asyncio
async def test_search_code_records_success_with_direct_mcp_identity(monkeypatch):
    from apps.mcp_server import server

    records = []
    async def fake_search(query, *, limit, repo_path):
        return {"repository_scope": {"repository_path": "/srv/project"}, "chunks": [{"path": "a.py"}]}
    async def fake_id(path):
        assert path == "/srv/project"
        return 7
    async def fake_record(**payload):
        records.append(payload)

    monkeypatch.setattr(server, "hybrid_search_code", fake_search)
    monkeypatch.setattr(server.settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    monkeypatch.setattr("apps.api.telemetry.repository_id_for_path", fake_id)
    monkeypatch.setattr("apps.api.telemetry.record_request", fake_record)

    result = await server.search_code("find config", repo_path="/requested")
    assert result["repository_scope"]["repository_path"] == "/srv/project"
    assert len(records) == 1
    assert records[0]["operation"] == "search"
    assert records[0]["principal_name"] == "mcp:unattributed"
    assert records[0]["repository_id"] == 7
    assert records[0]["repository_path"] == "/srv/project"
    assert records[0]["outcome"] == "success"
    assert records[0]["request_id"]
    assert records[0]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_prepare_context_records_returned_error_and_survives_telemetry_failure(monkeypatch):
    from apps.mcp_server import server

    records = []
    class FakeBuilder:
        async def build_context_pack(self, task, target):
            return {"status": "failed", "error": "context unavailable"}

    async def fake_record(**payload):
        records.append(payload)
        raise RuntimeError("telemetry store down")

    monkeypatch.setattr(server, "ContextPackBuilder", FakeBuilder)
    monkeypatch.setattr(server.settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    async def fake_id(path):
        return None
    monkeypatch.setattr("apps.api.telemetry.repository_id_for_path", fake_id)
    monkeypatch.setattr("apps.api.telemetry.record_request", fake_record)

    result = await server.prepare_task_context("locate setup", repo_path="/srv/project")
    assert result["status"] == "failed"
    assert len(records) == 1
    assert records[0]["operation"] == "context"
    assert records[0]["outcome"] == "error"
    assert records[0]["repository_path"] == "/srv/project"


@pytest.mark.asyncio
async def test_search_code_records_raised_exception_and_preserves_signature(monkeypatch):
    from inspect import signature
    from apps.mcp_server import server

    records = []
    async def boom(query, *, limit, repo_path):
        raise TimeoutError("retrieval timeout")
    async def fake_record(**payload):
        records.append(payload)

    monkeypatch.setattr(server, "hybrid_search_code", boom)
    monkeypatch.setattr(server.settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    async def fake_id(path):
        return None
    monkeypatch.setattr("apps.api.telemetry.repository_id_for_path", fake_id)
    monkeypatch.setattr("apps.api.telemetry.record_request", fake_record)

    result = await server.search_code("slow query")
    assert result["status"] == "failed"
    assert records[0]["outcome"] == "error"
    assert records[0]["repository_path"] is None
    assert str(signature(server.search_code)) == "(query: str, repo_path: Optional[str] = None) -> dict"
