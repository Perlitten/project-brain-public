from pathlib import Path
from brain.insights.drift_analyzer import ArchitecturalDriftAnalyzer
from brain.insights.proactive import deterministic_insights
from tests.fixtures.drift_fixtures import setup_moved_fixture, setup_violating_fixture

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

def test_drift_insights_include_evidence_and_keep_distinct_keys_when_lines_move(tmp_path):
    setup_violating_fixture(tmp_path)
    snapshot = {
        "counts": {"files": 100, "chunks": 500, "rules": 5, "decisions": 5},
        "embeddings": {"pgvector_coverage_pct": 100.0},
        "services": {"postgres": {"status": "healthy"}, "redis": {"status": "healthy"}, "neo4j": {"status": "healthy"}},
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "reports": [{"name": "audit.md"}],
        "repo": {"path": str(tmp_path)},
    }

    def drift():
        candidates = [item for item in deterministic_insights(snapshot) if item.insight_type == "architectural_drift"]
        assert len(candidates) == 2
        return {dict((entry["label"], entry["value"]) for entry in item.evidence)["file_path"]: item
                for item in candidates}

    first = drift()
    assert set(first) == {"apps/api/routers/users.py", "brain/workers/worker.py"}
    assert first["apps/api/routers/users.py"].severity == "warning"
    assert first["brain/workers/worker.py"].severity == "critical"
    evidence = {path: {entry["label"]: entry["value"] for entry in item.evidence} for path, item in first.items()}
    assert evidence["apps/api/routers/users.py"]["imported_module"] == "sqlalchemy.orm.session"
    assert evidence["apps/api/routers/users.py"]["line_number"] == "2"
    assert evidence["brain/workers/worker.py"]["imported_module"] == "apps.api.static"
    assert evidence["brain/workers/worker.py"]["line_number"] == "1"
    assert all(item["delta_state"] == "new" for item in evidence.values())
    keys = {path: item.dedupe_key for path, item in first.items()}
    assert all(keys.values()) and len(set(keys.values())) == 2
    assert {path: item.dedupe_key for path, item in drift().items()} == keys
    setup_moved_fixture(tmp_path)
    moved = drift()
    assert {path: item.dedupe_key for path, item in moved.items()} == keys
    moved_evidence = {entry["label"]: entry["value"] for entry in moved["apps/api/routers/users.py"].evidence}
    assert moved_evidence["line_number"] == "17"
