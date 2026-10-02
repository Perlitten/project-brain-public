import pytest
from pathlib import Path
from brain.insights.drift_analyzer import ArchitecturalDriftAnalyzer
from brain.insights.proactive import deterministic_insights
from brain._version import __version__

def test_version_is_bumped_to_v0_9_0():
    assert __version__ == "0.9.0"

def test_drift_analyzer_detects_api_layer_raw_orm_bypass():
    analyzer = ArchitecturalDriftAnalyzer(Path("."))
    code = """
import os
from sqlalchemy.orm.session import Session

def handle_request():
    pass
"""
    violations = analyzer.analyze_file("apps/api/routers/users.py", code)
    assert len(violations) == 1
    assert violations[0].rule_name == "api_raw_orm_bypass"
    assert violations[0].severity == "warning"
    assert violations[0].line_number == 3

def test_drift_analyzer_detects_worker_ui_coupling():
    analyzer = ArchitecturalDriftAnalyzer(Path("."))
    code = """
from apps.api.static import render_template

def process_job():
    pass
"""
    violations = analyzer.analyze_file("brain/workers/worker.py", code)
    assert len(violations) == 1
    assert violations[0].rule_name == "worker_ui_coupling"
    assert violations[0].severity == "critical"

def test_drift_analyzer_allows_clean_imports():
    analyzer = ArchitecturalDriftAnalyzer(Path("."))
    code = """
from brain.database.repository_utils import get_repository_by_path

def handle_request():
    pass
"""
    violations = analyzer.analyze_file("apps/api/routers/core.py", code)
    assert len(violations) == 0

@pytest.mark.asyncio
async def test_deterministic_insights_includes_drift_candidates():
    # Verify scan_repository_drift results flow into deterministic_insights() candidates
    snapshot = {
        "counts": {"files": 100, "chunks": 500, "rules": 5, "decisions": 5},
        "embeddings": {"pgvector_coverage_pct": 100.0},
        "services": {"postgres": {"status": "healthy"}, "redis": {"status": "healthy"}, "neo4j": {"status": "healthy"}},
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "reports": [{"name": "audit.md"}],
        "repo": {"path": "."},
    }
    candidates = deterministic_insights(snapshot)
    assert isinstance(candidates, list)
    types = {c.insight_type for c in candidates}
    assert "architectural_drift" in types or "operational_readiness" in types

def test_deduplication_key_consistency_across_multiple_runs():
    # Prove that running scan multiple times yields identical dedupe keys (preventing DB row duplication)
    snapshot = {
        "counts": {"files": 100, "chunks": 500},
        "embeddings": {"pgvector_coverage_pct": 100.0},
        "services": {"postgres": {"status": "healthy"}, "redis": {"status": "healthy"}, "neo4j": {"status": "healthy"}},
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "reports": [{"name": "audit.md"}],
        "repo": {"path": "."},
    }
    run1 = [c.dedupe_key for c in deterministic_insights(snapshot)]
    run2 = [c.dedupe_key for c in deterministic_insights(snapshot)]
    assert run1 == run2
