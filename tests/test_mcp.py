import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from apps.mcp_server.server import mcp


@pytest.mark.asyncio
async def test_mcp_server_tools_registered():
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]

    expected_tools = [
        "ask_project",
        "prepare_task_context",
        "prepare_deep_context",
        "get_background_job",
        "impact_analysis",
        "record_decision",
        "record_rule",
        "search_code",
        "find_related_files",
        "review_diff",
    ]

    for tool_name in expected_tools:
        assert tool_name in tool_names, f"Tool '{tool_name}' was not registered on the MCP server."


@pytest.mark.asyncio
async def test_mcp_record_decision_tool():
    # Mock DecisionStore
    import json

    mock_add_decision = AsyncMock(return_value=77)
    with patch("apps.mcp_server.server.DecisionStore.add_decision", mock_add_decision):
        result = await mcp.call_tool(
            "record_decision",
            {
                "title": "Use FastMCP",
                "repo_path": "/indexed/example",
                "description": "Standardize on FastMCP for server implementation",
                "status": "active",
            },
        )
        assert len(result.content) == 1
        data = json.loads(result.content[0].text)
        assert data["status"] == "success"
        assert data["decision_id"] == 77
        mock_add_decision.assert_called_once()
        assert mock_add_decision.await_args.kwargs["repo_path"] == "/indexed/example"


@pytest.mark.asyncio
async def test_mcp_record_rule_tool():
    import json

    mock_add_rule = AsyncMock(return_value="brand-logo-source")
    with patch("apps.mcp_server.server.RuleStore.add_rule", mock_add_rule):
        result = await mcp.call_tool(
            "record_rule",
            {
                "name": "Brand logo source",
                "repo_path": "/indexed/example",
                "description": "Use shared brand config.",
                "status": "active",
            },
        )
        assert len(result.content) == 1
        data = json.loads(result.content[0].text)
        assert data["status"] == "success"
        assert data["rule_id"] == "brand-logo-source"
        assert mock_add_rule.await_args.kwargs["repo_path"] == "/indexed/example"


@pytest.mark.asyncio
async def test_local_mcp_job_lookup_probes_each_v2_pool():
    from apps.mcp_server import server

    class Queue:
        def __init__(self, _redis, prefix):
            self.prefix = prefix

        async def get_job(self, _job_id):
            return {"id": "job-1", "status": "completed"} if self.prefix.endswith(":deep") else None

    with (
        patch("apps.mcp_server.server.JobQueue", Queue),
        patch("apps.mcp_server.server.settings.BRAIN_WORKER_POOLS_V2_ENABLED", True),
    ):
        result = await server.get_background_job("job-1")

    assert result["pool"] == "deep"


# --------------------------------------------------------------------------- freshness envelope
#
# Every tool that serves indexed knowledge carries the same ``freshness``
# block, so an agent decides from ``state`` alone whether to trust the answer.

ENVELOPE_CORE = {"state", "reason", "indexed_commit", "indexed_at", "age_s", "reindex_job_id"}
ENVELOPE_STATES = {"fresh", "stale_served", "stale_blocked", "unknown"}


def _envelope(result):
    import json

    data = json.loads(result.content[0].text)
    if result.structured_content is not None:
        assert result.structured_content["freshness"] == data["freshness"]
    return data["freshness"]


def _well_formed(envelope):
    assert ENVELOPE_CORE <= set(envelope)
    assert envelope["state"] in ENVELOPE_STATES
    assert isinstance(envelope["reason"], str) and envelope["reason"]
    return envelope


def _fresh(**overrides):
    base = {
        "state": "fresh", "reason": "index_matches_source", "indexed_commit": "a" * 40,
        "indexed_at": "2026-10-17T00:00:00+00:00", "age_s": 60, "reindex_job_id": None,
    }
    return {**base, **overrides}


def _telemetry_patches():
    return (
        patch("apps.api.telemetry.repository_id_for_path", AsyncMock(return_value=None)),
        patch("apps.api.telemetry.record_request", AsyncMock()),
    )


@pytest.mark.asyncio
async def test_prepare_task_context_surfaces_the_builders_envelope_and_reindex_job(monkeypatch):
    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "on")
    payload = {
        "status": "ok", "repo": {"path": "/indexed/app", "freshness": "stale_served"}, "missing": [],
        "slices": [], "freshness": _fresh(state="stale_served", reason="source_ahead_of_index",
                                          reindex_job_id="job-9"),
    }
    telemetry_repo, telemetry_record = _telemetry_patches()
    with (
        patch("apps.mcp_server.server.RuntimeContextBuilder.build", AsyncMock(return_value=payload)),
        telemetry_repo, telemetry_record,
    ):
        result = await mcp.call_tool("prepare_task_context", {"task_description": "explain", "repo_path": "/indexed/app"})
    envelope = _well_formed(_envelope(result))
    assert envelope["state"] == "stale_served"
    assert envelope["reindex_job_id"] == "job-9"


@pytest.mark.asyncio
async def test_prepare_task_context_v1_attaches_an_envelope_for_the_path(monkeypatch):
    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    telemetry_repo, telemetry_record = _telemetry_patches()
    with (
        patch("apps.mcp_server.server.ContextPackBuilder.build_context_pack", AsyncMock(return_value={"files": []})),
        patch("apps.mcp_server.server.envelope_for_path", AsyncMock(return_value=_fresh())),
        telemetry_repo, telemetry_record,
    ):
        result = await mcp.call_tool("prepare_task_context", {"task_description": "explain", "repo_path": "/indexed/app"})
    assert _well_formed(_envelope(result))["state"] == "fresh"


@pytest.mark.asyncio
async def test_ask_project_returns_stale_blocked_as_a_structured_code(monkeypatch):
    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "on")
    blocked = {
        "status": "partial", "degraded": ["current_source_required_for_code_change"],
        "answer": "Current indexed context is not fresh enough to answer safely.",
        "freshness": _fresh(state="stale_blocked", reason="policy_strict_block", reindex_job_id="job-3"),
    }
    with patch("apps.api.routers.core._ask_v2", AsyncMock(return_value=blocked)):
        result = await mcp.call_tool("ask_project", {"query": "how does parsing work", "repo_path": "/indexed/app"})
    assert not result.is_error
    assert result.content[0].text.startswith("Current indexed context is not fresh")
    envelope = _well_formed(result.structured_content["freshness"])
    assert envelope["state"] == "stale_blocked"
    assert envelope["reason"] == "policy_strict_block"
    assert envelope["reindex_job_id"] == "job-3"
    assert result.structured_content["answer"] == blocked["answer"]


@pytest.mark.asyncio
async def test_ask_project_v1_keeps_the_prose_answer_and_adds_the_envelope(monkeypatch):
    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    found = {
        "files": [], "symbols": [], "chunks": [{"path": "a.py", "content": "x", "similarity": 0.9}],
        "repository_scope": {"found": True, "repository_path": "/indexed/app", "last_indexed_commit": "b" * 40,
                             "freshness": {"status": "current"}},
    }
    llm = MagicMock()
    llm.generate = AsyncMock(return_value="grounded answer")
    router = MagicMock()
    router.llm.return_value = llm
    with (
        patch("apps.mcp_server.server.hybrid_search_code", AsyncMock(return_value=found)),
        patch("apps.mcp_server.server.get_model_router", lambda: router),
        patch("apps.mcp_server.server.RuleStore.list_active_rules", AsyncMock(return_value=[])),
        patch("apps.mcp_server.server.DecisionStore.list_decisions", AsyncMock(return_value=[])),
    ):
        result = await mcp.call_tool("ask_project", {"query": "parser", "repo_path": "/indexed/app"})
    assert result.content[0].text == "grounded answer"
    envelope = _well_formed(result.structured_content["freshness"])
    assert envelope["state"] == "fresh"
    assert envelope["indexed_commit"] == "b" * 40


@pytest.mark.asyncio
async def test_search_code_v1_derives_the_envelope_from_the_repository_scope(monkeypatch):
    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    found = {"chunks": [], "repository_scope": {
        "found": True, "last_indexed_commit": "c" * 40,
        "freshness": {"status": "behind", "commits_behind": 3, "age_seconds": 120},
    }}
    telemetry_repo, telemetry_record = _telemetry_patches()
    with (
        patch("apps.mcp_server.server.hybrid_search_code", AsyncMock(return_value=found)),
        telemetry_repo, telemetry_record,
    ):
        result = await mcp.call_tool("search_code", {"query": "config"})
    envelope = _well_formed(_envelope(result))
    assert envelope["state"] == "stale_served"
    assert envelope["reason"] == "source_ahead_of_index"
    assert envelope["commits_behind"] == 3 and envelope["age_s"] == 120


@pytest.mark.asyncio
async def test_search_code_reports_an_unindexed_repository_as_unknown(monkeypatch):
    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    found = {"chunks": [], "repository_scope": {"found": False}}
    telemetry_repo, telemetry_record = _telemetry_patches()
    with (
        patch("apps.mcp_server.server.hybrid_search_code", AsyncMock(return_value=found)),
        telemetry_repo, telemetry_record,
    ):
        result = await mcp.call_tool("search_code", {"query": "config"})
    envelope = _well_formed(_envelope(result))
    assert envelope["state"] == "unknown" and envelope["reason"] == "repository_not_indexed"


@pytest.mark.asyncio
async def test_impact_review_related_and_deep_context_carry_the_envelope(monkeypatch, tmp_path):
    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    monkeypatch.setattr("apps.mcp_server.server.settings.TARGET_REPO_PATH", str(tmp_path))
    for_path = AsyncMock(return_value=_fresh(reason="by_path"))
    for_repo = AsyncMock(return_value=_fresh(reason="by_repository"))

    class Queue:
        async def enqueue(self, *_args, **_kwargs):
            return "job-7"

    with (
        patch("apps.mcp_server.server.envelope_for_path", for_path),
        patch("apps.mcp_server.server.envelope_for_repository", for_repo),
        patch("apps.mcp_server.server.ImpactAnalyzer.analyze_impact", AsyncMock(return_value={"affected": []})),
        patch("apps.mcp_server.server.DiffAnalyzer.review_diff", AsyncMock(return_value={"report": "clean"})),
        patch("apps.mcp_server.server.require_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=1))),
        patch("apps.mcp_server.server.GraphClient.get_neighbors", AsyncMock(return_value=[])),
        patch("apps.mcp_server.server.queue_for_job", lambda *_a, **_k: Queue()),
    ):
        impact = await mcp.call_tool("impact_analysis", {"change_request": "rename", "repo_path": str(tmp_path)})
        review = await mcp.call_tool("review_diff", {"repo_path": str(tmp_path)})
        related = await mcp.call_tool("find_related_files", {"file_path": "a.py", "repo_path": str(tmp_path)})
        deep = await mcp.call_tool("prepare_deep_context", {"task_description": "deep", "repo_path": str(tmp_path)})

    for result in (impact, review, deep):
        assert _well_formed(_envelope(result))["reason"] == "by_path"
    assert _well_formed(_envelope(related))["reason"] == "by_repository"
    assert for_path.await_count == 3 and for_repo.await_count == 1


@pytest.mark.asyncio
async def test_tool_failures_still_carry_an_unknown_envelope(monkeypatch):
    import json

    monkeypatch.setattr("apps.mcp_server.server.settings.BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    with patch("apps.mcp_server.server.DiffAnalyzer.review_diff", AsyncMock(side_effect=RuntimeError("boom"))):
        result = await mcp.call_tool("review_diff", {"repo_path": "/indexed/app"})
    data = json.loads(result.content[0].text)
    assert data["status"] == "failed"
    envelope = _well_formed(data["freshness"])
    assert envelope["state"] == "unknown" and envelope["reason"] == "tool_failed"


def test_envelope_model_is_the_api_contract():
    from apps.api.schemas import FreshnessEnvelope
    from brain.memory.freshness_envelope import freshness_envelope, unknown_envelope

    assert set(FreshnessEnvelope.CORE_FIELDS) == ENVELOPE_CORE
    assert set(unknown_envelope()) == ENVELOPE_CORE
    mapped = freshness_envelope({
        "status": "stale", "note": "Unable to validate freshness: no git", "indexed_commit": "d" * 40,
        "last_indexed_at": "2026-10-01T00:00:00+00:00", "age_seconds": "7", "origin_head": "e" * 40,
    })
    assert mapped["state"] == "stale_served" and mapped["reason"] == "source_head_unverified"
    assert mapped["age_s"] == 7 and mapped["index_status"] == "stale"
    assert mapped["origin_head"] == "e" * 40  # a field the freshness module adds passes through
    assert FreshnessEnvelope.model_validate(mapped).state == "stale_served"
