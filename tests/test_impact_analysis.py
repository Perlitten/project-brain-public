"""Unit & integration test suite for Deep Change-Impact Analysis (Workstream C)."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.insights.impact_engine import ImpactAnalysisEngine
from brain.insights.impact_models import ImpactedEntity
from brain.insights.test_impact import TestImpactSelector


def test_test_impact_selector(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_api(): pass\n", encoding="utf-8")

    entities = [
        ImpactedEntity(
            entity_id="file:apps/api/main.py",
            normalized_path="apps/api/main.py",
            entity_type="File",
            subsystem="api",
            evidence_category="direct",
            confidence="confirmed",
        )
    ]

    suggested = TestImpactSelector.select_tests(tmp_path, entities)
    assert "tests/test_api.py" in suggested


def test_impact_analysis_engine(tmp_path):
    engine = ImpactAnalysisEngine(tmp_path)

    with patch("brain.insights.git_diff.GitDiffEngine.get_changed_files", return_value=["apps/api/main.py"]):
        res = engine.analyze_impact(base_rev="HEAD~1", cand_rev="HEAD")

        assert res.directly_changed_files == ["apps/api/main.py"]
        assert len(res.impacted_entities) >= 1
        assert "api" in res.impacted_subsystems


def test_impact_api_endpoints(tmp_path):
    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.impact.resolve_repo_path", return_value=tmp_path):
            with patch("brain.insights.git_diff.GitDiffEngine.get_changed_files", return_value=["brain/core/context_pack.py"]):
                r1 = client.post(f"/insights/impact/analyze?repo_path={tmp_path}")
                assert r1.status_code == 200
                data = r1.json()
                assert data["status"] == "success"
                run_id = data["result"]["run_id"]

                # GET /{run_id}
                r2 = client.get(f"/insights/impact/{run_id}")
                assert r2.status_code == 200

                # GET /{run_id}/tests
                r3 = client.get(f"/insights/impact/{run_id}/tests")
                assert r3.status_code == 200
                assert "suggested_tests" in r3.json()
    finally:
        app.dependency_overrides.clear()


def test_impact_node_scoring_prefers_discriminative_keywords():
    """IDF-weighted scoring: nodes matched by rare keywords outrank nodes
    matched only by generic high-frequency keywords."""
    from brain.analyzers.impact_analyzer import _score_node

    # 'file' matches hundreds of nodes, 'migrations' only a handful.
    kw_idf = {"file": 2.6, "migrations": 6.4, "database": 4.4}
    generic = {
        "name": "File", "type": "Symbol", "distance": 0,
        "matched_keywords": {"file"}, "path_count": 1,
    }
    specific = {
        "name": "brain/database/migrations.py", "type": "File", "distance": 0,
        "matched_keywords": {"database", "migrations"}, "path_count": 2,
    }
    assert _score_node(specific, kw_idf) > _score_node(generic, kw_idf)


def test_impact_register_node_accumulates_evidence():
    """Repeated registration unions keywords, bumps path count, keeps min distance."""
    from brain.analyzers.impact_analyzer import _register_node

    visited: dict = {}
    _register_node(visited, "n1", "models.py", "File", 2, "file", {})
    _register_node(visited, "n1", "models.py", "File", 0, "database", {})
    info = visited["n1"]
    assert info["distance"] == 0
    assert info["matched_keywords"] == {"file", "database"}
    assert info["path_count"] == 2


def test_impact_noise_node_filter_unchanged():
    """The SQL/prose debris filter still rejects junk and keeps file paths."""
    from brain.analyzers.impact_analyzer import _is_noise_node

    assert _is_noise_node("SELECT")
    assert _is_noise_node("where")
    assert _is_noise_node("")
    assert not _is_noise_node("brain/database/models.py")
    assert not _is_noise_node("get_embedding_provider")


def test_deterministic_keywords_extracts_technical_terms():
    """Fast-mode keyword extraction keeps technical terms, drops stopwords."""
    from brain.analyzers.impact_analyzer import _deterministic_keywords

    kws = _deterministic_keywords(
        "Rename the function resolve_embedding_dimension to get_embedding_dims across the codebase"
    )
    assert "resolve_embedding_dimension" in kws
    assert "get_embedding_dims" in kws
    # Stopwords and short tokens are dropped.
    assert "the" not in kws
    assert "to" not in kws
    assert "across" not in kws
    assert "codebase" not in kws
    # No duplicates (case-insensitive).
    assert len(kws) == len({k.lower() for k in kws})


def test_deterministic_keywords_empty_request():
    """Degenerate input yields no keywords instead of crashing."""
    from brain.analyzers.impact_analyzer import _deterministic_keywords

    assert _deterministic_keywords("a b c") == []
    assert _deterministic_keywords("") == []


def test_split_keyword_compounds():
    """Compound keywords are split into searchable parts, original kept."""
    from brain.analyzers.impact_analyzer import _split_keyword

    parts = _split_keyword("indexing_source")
    assert "indexing_source" in parts
    assert "indexing" in parts
    assert "source" in parts

    parts = _split_keyword("getEmbeddingDims")
    assert "get" in parts
    assert "Embedding" in parts or "embedding" in [p.lower() for p in parts]

    # Short fragments are dropped, but the original compound is kept
    # if it meets the length threshold.
    assert _split_keyword("a_bc") == ["a_bc"]
    assert _split_keyword("ab") == []


def test_llm_keywords_union_with_deterministic():
    """LLM keywords are merged with deterministic ones for robustness."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from brain.analyzers.impact_analyzer import ImpactAnalyzer

    async def go():
        analyzer = ImpactAnalyzer("/tmp")
        mock_llm = AsyncMock()
        # LLM omits 'database' and returns a compound that matches nothing.
        mock_llm.generate.return_value = '["File", "indexing_source", "nullable", "column"]'
        with patch.object(type(analyzer.router), "llm", return_value=mock_llm):
            kws = await analyzer._llm_keywords(
                "Add a new nullable column to the File database model for tracking the indexing source"
            )
        low = {k.lower() for k in kws}
        # LLM terms preserved...
        assert "file" in low
        # ...compounds split...
        assert "indexing" in low
        assert "source" in low
        # ...and deterministic backup fills the gap the LLM left.
        assert "database" in low
        assert "model" in low
        return True

    assert asyncio.run(go())


def test_strip_thinking_final_answer_marker():
    """FINAL ANSWER marker extracts only the answer part."""
    from apps.api.routers.core import _strip_thinking_blocks

    text = """1. **Analyze**: The user asks about retries.
Let me think about this...

FINAL ANSWER: The queue uses exponential backoff."""
    assert _strip_thinking_blocks(text) == "The queue uses exponential backoff."

    # Without marker, falls back to other stripping.
    assert _strip_thinking_blocks("1. **Step**: thinking\n\nReal answer.") == "Real answer."
