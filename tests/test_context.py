import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from brain.context.context_pack_builder import (
    ContextPackBuilder,
    _protected_retrieval_paths,
    _selected_retrieval_paths,
    _decision_scope_clause,
    _load_active_normative_memory,
    _rule_scope_clause,
)
from brain.analyzers.impact_analyzer import ImpactAnalyzer
from brain.analyzers.diff_analyzer import DiffAnalyzer


def test_context_builder_preserves_retrieval_abstention():
    abstained = SimpleNamespace(selected_paths=[], reranked=[SimpleNamespace(item_id="noise.py", included=False)], debug={"abstained": True})
    assert _selected_retrieval_paths(abstained, 5) == []


def test_context_builder_keeps_legacy_empty_selection_fallback():
    legacy = SimpleNamespace(selected_paths=[], reranked=[SimpleNamespace(item_id="fallback.py", included=False)], debug={})
    assert _selected_retrieval_paths(legacy, 5) == ["fallback.py"]


def test_context_pack_protects_final_selection_instead_of_intermediate_ranking():
    result = SimpleNamespace(
        selected_paths=["engine/widget.py", "engine/routes.py"],
        v5_top_paths=["engine/widget.py", "tests/test_widget.py"],
        reranked=[], debug={},
    )
    available = {"engine/widget.py", "engine/routes.py", "tests/test_widget.py"}
    assert _protected_retrieval_paths(result, 2, available) == result.selected_paths


def test_context_pack_retains_legacy_pair_expansion_when_no_final_selection():
    result = SimpleNamespace(selected_paths=[], v5_top_paths=["engine/widget.py"], reranked=[], debug={})
    assert _protected_retrieval_paths(result, 2, {"engine/widget.py", "tests/test_widget.py"}) == [
        "engine/widget.py", "tests/test_widget.py",
    ]


def test_context_pack_protection_respects_abstention():
    result = SimpleNamespace(selected_paths=[], v5_top_paths=["noise.py"], reranked=[], debug={"abstained": True})
    assert _protected_retrieval_paths(result, 2, {"noise.py"}) == []


def test_decision_scope_clause_includes_global_and_matching_repo():
    statement = str(_decision_scope_clause("/indexed/Eunoia/").compile(compile_kwargs={"literal_binds": True}))

    assert "decisions.repo_path IS NULL" in statement
    assert "lower(trim(decisions.repo_path)) = '/indexed/eunoia'" in statement


def test_rule_scope_clause_includes_global_and_matching_repo():
    statement = str(_rule_scope_clause("/indexed/Eunoia/").compile(compile_kwargs={"literal_binds": True}))

    assert "rules.repo_path IS NULL" in statement
    assert "lower(trim(rules.repo_path)) = '/indexed/eunoia'" in statement


@pytest.mark.asyncio
async def test_active_normative_memory_is_loaded_fresh_and_scoped():
    active_rule = SimpleNamespace(name="global rule")
    scoped_decision = SimpleNamespace(title="Nostia decision")
    rules_result = MagicMock()
    rules_result.scalars.return_value.all.return_value = [active_rule]
    decisions_result = MagicMock()
    decisions_result.scalars.return_value.all.return_value = [scoped_decision]
    session = AsyncMock()
    session.execute.side_effect = [rules_result, decisions_result]
    session_factory = MagicMock()
    session_factory.return_value.__aenter__.return_value = session

    with patch(
        "brain.context.context_pack_builder.async_session_factory",
        session_factory,
    ):
        rules, decisions = await _load_active_normative_memory("/indexed/Eunoia")

    assert rules == [active_rule]
    assert decisions == [scoped_decision]
    assert session.execute.await_count == 2
    rule_query = str(session.execute.await_args_list[0].args[0])
    decision_query = str(session.execute.await_args_list[1].args[0])
    assert "rules.repo_path IS NULL" in rule_query
    assert "decisions.repo_path IS NULL" in decision_query


@pytest.mark.asyncio
async def test_context_pack_builder_success(tmp_path):
    # Setup mock LLM and databases
    mock_llm = AsyncMock()
    mock_llm.generate.return_value = '{"task_type": "bugfix", "keywords": ["auth", "login"], "risks": ["payment regression"], "features": ["auth"], "plan": "1. Change login logic", "checklist": ["test login"]}'

    mock_graph_client = MagicMock()
    mock_graph_client.get_neighbors = AsyncMock(return_value=[])

    mock_session = AsyncMock()
    # session.add is synchronous on AsyncSession; a bare AsyncMock would return
    # an unawaited coroutine and emit RuntimeWarning.
    mock_session.add = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    # Mock DB Query Results
    mock_exec_res = MagicMock()
    mock_exec_res.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    async def mock_refresh(obj):
        obj.id = 101

    mock_session.refresh = mock_refresh

    mock_router = MagicMock()
    mock_router.llm.return_value = mock_llm

    # The hybrid retrieval pipeline embeds the query (network) and uses its own
    # DB session — neither is mocked here — so stub run() with an empty result.
    # This keeps the test offline and scopes it to the plan/save path.
    stub_retrieval = SimpleNamespace(
        vector_status="ok",
        candidates_by_channel={},
        selected_paths=[],
        reranked=[SimpleNamespace(item_id="noise.py", included=False, reranker_score=0.01, metadata={})],
        v5_top_paths=["noise.py"],
        v5_rerank_debug={},
        recall_debug={},
        debug={"abstained": True},
    )
    mock_pipeline = MagicMock()
    mock_pipeline.run = AsyncMock(return_value=stub_retrieval)

    # Repo lookup + CAG cache also touch the DB via their own session refs.
    mock_cache = MagicMock()
    mock_cache.initialize = AsyncMock()
    mock_cache.initialized = True
    mock_cache.files = [SimpleNamespace(path="noise.py", summary="irrelevant", id=7)]
    mock_cache.symbols = []

    source_repo = tmp_path / "indexed-repo"
    source_repo.mkdir()
    output_dir = tmp_path / "context-pack-store"

    with (
        patch("brain.context.context_pack_builder.get_model_router", return_value=mock_router),
        patch("brain.context.context_pack_builder.GraphClient", return_value=mock_graph_client),
        patch("brain.context.context_pack_builder.get_repository_by_path", AsyncMock(return_value=None)),
        patch("brain.retrieval.pipeline.HybridRetrievalPipeline", return_value=mock_pipeline),
        patch("brain.context.context_pack_builder.context_packs_dir", return_value=output_dir),
        patch("brain.context.context_pack_builder.async_session_factory", mock_session_factory),
    ):
        builder = ContextPackBuilder()
        builder._cache = mock_cache
        result = await builder.build_context_pack("Fix auth login issue", source_repo)

        assert result["id"] == 101
        assert result["task_type"] == "bugfix"
        assert "auth" in result["keywords"]
        assert Path(result["path"]).exists()
        assert Path(result["path"]).parent == output_dir
        assert result["retrieved_files"] == []
        assert not (source_repo / "context_packs").exists()


def test_context_packs_dir_uses_dedicated_setting(tmp_path):
    from brain.config.paths import context_packs_dir

    configured = tmp_path / "artifacts" / "context-packs"
    with patch("brain.config.paths.settings.CONTEXT_PACK_OUTPUT_DIR", str(configured)):
        assert context_packs_dir() == configured.resolve()


@pytest.mark.asyncio
async def test_impact_analyzer_success():
    # Setup mock LLM
    mock_llm = AsyncMock()
    mock_llm.generate.side_effect = [
        '["auth", "users"]',  # Keywords
        '{"adjusted_risk_level": "High", "rationale": "Directly impacts security", "verification_checks": ["Check session expiration"]}',  # Analysis
    ]

    mock_graph_client = MagicMock()
    mock_graph_client.driver.session.return_value.__aenter__.return_value = AsyncMock()

    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    mock_exec_res = MagicMock()
    mock_exec_res.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    mock_router = MagicMock()
    mock_router.llm.return_value = mock_llm

    with (
        patch("brain.analyzers.impact_analyzer.get_model_router", return_value=mock_router),
        patch("brain.analyzers.impact_analyzer.GraphClient", return_value=mock_graph_client),
        patch(
            "brain.analyzers.impact_analyzer.require_repository_by_path",
            new=AsyncMock(return_value=SimpleNamespace(id=7, path="/app")),
        ),
        patch("brain.analyzers.impact_analyzer.async_session_factory", mock_session_factory),
    ):
        analyzer = ImpactAnalyzer()
        result = await analyzer.analyze_impact("Add OAuth login method")

        assert result["risk_level"] == "High"
        assert "Check session expiration" in result["verification_checks"]
        assert Path(result["report_path"]).exists()


@pytest.mark.asyncio
async def test_impact_analyzer_rejects_unindexed_repository():
    mock_llm = AsyncMock()
    mock_llm.generate.return_value = '["auth"]'
    mock_router = MagicMock()
    mock_router.llm.return_value = mock_llm

    with (
        patch("brain.analyzers.impact_analyzer.get_model_router", return_value=mock_router),
        patch(
            "brain.analyzers.impact_analyzer.require_repository_by_path",
            new=AsyncMock(side_effect=LookupError("Repository is not indexed: /missing")),
        ),
    ):
        analyzer = ImpactAnalyzer("/missing")
        with pytest.raises(LookupError, match="Repository is not indexed"):
            await analyzer.analyze_impact("Change auth")


@pytest.mark.asyncio
async def test_diff_analyzer_success(tmp_path):
    mock_llm = AsyncMock()
    mock_llm.generate.return_value = '{"rule_violations": [{"rule_id": "auth-rule", "details": "no test"}], "suspicious_changes": ["debug print left"], "feedback": "Needs work"}'

    # Mock subprocess.run for git diff
    mock_run = MagicMock()
    mock_run.side_effect = [
        MagicMock(stdout=""),  # git rev-parse (check if inside work tree)
        MagicMock(stdout="brain/auth.py\n"),  # git diff --name-only
        MagicMock(stdout="+ print('debug')\n"),  # git diff
        MagicMock(stdout="abc123commit\n"),  # git rev-parse HEAD
    ]

    mock_session = AsyncMock()
    # session.add is synchronous on AsyncSession; a bare AsyncMock would return
    # an unawaited coroutine and emit RuntimeWarning.
    mock_session.add = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    mock_exec_res = MagicMock()
    mock_exec_res.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_exec_res

    async def mock_refresh(obj):
        obj.id = 202

    mock_session.refresh = mock_refresh

    mock_router = MagicMock()
    mock_router.llm.return_value = mock_llm

    with (
        patch("brain.analyzers.diff_analyzer.get_model_router", return_value=mock_router),
        patch("subprocess.run", mock_run),
        patch("brain.analyzers.diff_analyzer.async_session_factory", mock_session_factory),
        patch(
            "brain.analyzers.diff_analyzer.RuleStore.list_active_rules",
            AsyncMock(return_value=[]),
        ),
    ):
        analyzer = DiffAnalyzer(tmp_path)
        result = await analyzer.review_diff(base="main", head="current")

        assert result["status"] == "needs_review"
        assert len(result["rule_violations"]) == 1
        assert "brain/auth.py" in result["missing_tests"]
        assert Path(result["report_path"]).exists()
