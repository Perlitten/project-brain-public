from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.schemas import AskRequest
from apps.api.routers import core_retrieval
from brain.config.settings import settings


@pytest.mark.asyncio
async def test_ask_no_candidates_is_explicitly_partial(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    monkeypatch.setattr(core_retrieval, "hybrid_search_code", AsyncMock(return_value={"files": [], "symbols": [], "chunks": []}))
    result = await core_retrieval.ask_project(AskRequest(query="where is the missing feature"), SimpleNamespace(headers={}))
    assert result["status"] == "partial"
    assert result["degraded"] == ["no_retrieval_candidates"]
    assert "fresh" not in result["answer"].lower()


@pytest.mark.asyncio
async def test_ask_uses_compact_budget_without_changing_default_synthesis(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    monkeypatch.setattr(settings, "LLM_TASK_ASK_MODEL", "fast-ask-model")
    monkeypatch.setattr(settings, "MEMORY_SKILLS_IN_ASK", False)
    monkeypatch.setattr(core_retrieval, "hybrid_search_code", AsyncMock(return_value={
        "files": [{"path": "apps/api/main.py", "summary": "entrypoint"}], "symbols": [], "chunks": [],
        "repository_scope": {"repository_path": "/app", "freshness": {"status": "current"}},
    }))
    monkeypatch.setattr(core_retrieval.RuleStore, "list_active_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(core_retrieval.DecisionStore, "list_decisions", AsyncMock(return_value=[]))
    import brain.context.context_pack_builder as builder_module
    monkeypatch.setattr(builder_module, "_load_active_learnings", AsyncMock(return_value=[]))
    generate = AsyncMock(return_value={"text": "FINAL ANSWER:\nUse apps/api/main.py.", "finish_reason": "stop", "usage": {"completion_tokens": 6}})
    provider = SimpleNamespace(generate_with_metadata=generate)
    selected = []
    router = SimpleNamespace(llm_for_model=lambda model: (selected.append(model) or provider), llm=lambda task: provider)
    monkeypatch.setattr(core_retrieval, "get_model_router", lambda: router)

    result = await core_retrieval.ask_project(AskRequest(query="where is the entrypoint"), SimpleNamespace(headers={}))
    assert result["answer"] == "Use apps/api/main.py."
    assert generate.call_args.kwargs["max_tokens"] == 900
    assert result["usage"]["completion_tokens"] == 6
    assert selected == ["fast-ask-model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata,marker", [
    ({"text": "<think>unfinished reasoning", "finish_reason": "stop"}, "open_thinking_block"),
    ({"text": "FINAL ANSWER:\ncut off", "finish_reason": "length"}, "generation_token_limit"),
])
async def test_ask_does_not_mark_incomplete_generation_as_ok(monkeypatch, metadata, marker):
    monkeypatch.setattr(settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    monkeypatch.setattr(settings, "MEMORY_SKILLS_IN_ASK", False)
    monkeypatch.setattr(core_retrieval, "hybrid_search_code", AsyncMock(return_value={
        "files": [{"path": "a.py", "summary": "evidence"}], "symbols": [], "chunks": [],
        "repository_scope": {"repository_path": "/app", "freshness": {"status": "current"}},
    }))
    monkeypatch.setattr(core_retrieval.RuleStore, "list_active_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(core_retrieval.DecisionStore, "list_decisions", AsyncMock(return_value=[]))
    import brain.context.context_pack_builder as builder_module
    monkeypatch.setattr(builder_module, "_load_active_learnings", AsyncMock(return_value=[]))
    provider = SimpleNamespace(generate_with_metadata=AsyncMock(return_value=metadata))
    monkeypatch.setattr(core_retrieval, "get_model_router", lambda: SimpleNamespace(llm=lambda task: provider))
    result = await core_retrieval.ask_project(AskRequest(query="where is evidence"), SimpleNamespace(headers={}))
    assert result["status"] == "partial"
    assert marker in result["degraded"]


@pytest.mark.asyncio
async def test_ask_marks_response_output_cap_as_partial(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    monkeypatch.setattr(settings, "MEMORY_SKILLS_IN_ASK", False)
    monkeypatch.setattr(settings, "AGENT_ASK_OUTPUT_MAX_BYTES", 20)
    monkeypatch.setattr(core_retrieval, "hybrid_search_code", AsyncMock(return_value={
        "files": [{"path": "a.py", "summary": "evidence"}], "symbols": [], "chunks": [],
        "repository_scope": {"repository_path": "/app", "freshness": {"status": "current"}},
    }))
    monkeypatch.setattr(core_retrieval.RuleStore, "list_active_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(core_retrieval.DecisionStore, "list_decisions", AsyncMock(return_value=[]))
    import brain.context.context_pack_builder as builder_module
    monkeypatch.setattr(builder_module, "_load_active_learnings", AsyncMock(return_value=[]))
    # Deliberately omit metadata API: legacy providers remain supported.
    provider = SimpleNamespace(generate=AsyncMock(return_value="FINAL ANSWER:\n" + ("x" * 100)))
    monkeypatch.setattr(core_retrieval, "get_model_router", lambda: SimpleNamespace(llm=lambda task: provider))
    result = await core_retrieval.ask_project(AskRequest(query="where is evidence"), SimpleNamespace(headers={}))
    assert result["status"] == "partial"
    assert "answer_output_cap" in result["degraded"]
