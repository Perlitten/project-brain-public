import pytest

from apps.api import helpers
from brain.database import session as database_session


@pytest.mark.asyncio
async def test_mcp_runtime_status_uses_registration_and_datastore_liveness(monkeypatch):
    monkeypatch.setattr(helpers, "get_mcp_tools_list", lambda: [{"name": "search_code"}])

    async def healthy_datastores():
        return {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        }

    monkeypatch.setattr(database_session, "check_health", healthy_datastores)
    status = await helpers.get_mcp_runtime_status()

    assert status["status"] == "online"
    assert status["available"] is True
    assert status["diagnostics"]["probe_target"] == "registration_and_datastores"
    assert status["diagnostics"]["probe_status"] == "passed"
    assert "registration" in status["diagnostics"]["stage_timings_ms"]
    assert "datastores" in status["diagnostics"]["stage_timings_ms"]
