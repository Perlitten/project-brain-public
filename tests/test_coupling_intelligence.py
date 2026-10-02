"""Unit & integration test suite for Subsystem Coupling Intelligence (Workstream B)."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.graph.builder_v2 import GraphBuilderV2
from brain.graph.generation_manager import GraphGenerationManager
from brain.insights.coupling_metrics import CouplingMetricsCalculator
from brain.insights.coupling_rules import CouplingRulesEngine
from brain.insights.cycle_detector import CycleDetector
from brain.insights.subsystem_config import SubsystemConfigManager


def test_subsystem_matching(tmp_path):
    mgr = SubsystemConfigManager(tmp_path)
    subsystems = mgr.load_subsystems()

    assert mgr.find_subsystem_for_file("apps/api/main.py", subsystems) == "api"
    assert mgr.find_subsystem_for_file("brain/retrieval/pipeline.py", subsystems) == "retrieval"
    assert mgr.find_subsystem_for_file("brain/workers/worker.py", subsystems) == "workers"


def test_coupling_metrics_and_cycle_detection(tmp_path):
    api_dir = tmp_path / "apps" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "routes.py").write_text("import brain.workers.task\n", encoding="utf-8")

    worker_dir = tmp_path / "brain" / "workers"
    worker_dir.mkdir(parents=True)
    (worker_dir / "task.py").write_text("import apps.api.routes\n", encoding="utf-8")

    builder = GraphBuilderV2(tmp_path)
    meta, _ = builder.build_generation("HEAD")

    gen_mgr = GraphGenerationManager(tmp_path / ".brain")
    gen_mgr.activate_generation(meta.generation_id)
    store = gen_mgr.get_active_graph_store()

    sub_mgr = SubsystemConfigManager(tmp_path)
    subsystems = sub_mgr.load_subsystems()

    node_metrics, sub_metrics = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    assert len(sub_metrics) >= 2

    node_sub_map = {nid: m.subsystem for nid, m in node_metrics.items()}
    cycles = CycleDetector.detect_cycles(store, node_sub_map)
    assert len(cycles) >= 1  # Circular import between routes.py and task.py

    findings = CouplingRulesEngine.evaluate(store, subsystems, sub_mgr)
    assert any(f.rule_id == "COUPLING-001" for f in findings)


def test_coupling_api_endpoints(tmp_path):
    (tmp_path / "apps" / "api").mkdir(parents=True)
    (tmp_path / "apps" / "api" / "routes.py").write_text("x = 1\n", encoding="utf-8")

    builder = GraphBuilderV2(tmp_path)
    meta, _ = builder.build_generation("HEAD")
    gen_mgr = GraphGenerationManager(tmp_path / ".brain")
    gen_mgr.activate_generation(meta.generation_id)

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.coupling.resolve_repo_path", return_value=tmp_path):
            # GET /summary
            r1 = client.get(f"/insights/coupling/summary?repo_path={tmp_path}")
            assert r1.status_code == 200
            assert r1.json()["status"] == "success"

            # GET /subsystems
            r2 = client.get(f"/insights/coupling/subsystems?repo_path={tmp_path}")
            assert r2.status_code == 200

            # GET /cycles
            r3 = client.get(f"/insights/coupling/cycles?repo_path={tmp_path}")
            assert r3.status_code == 200

            # GET /hotspots
            r4 = client.get(f"/insights/coupling/hotspots?repo_path={tmp_path}")
            assert r4.status_code == 200
    finally:
        app.dependency_overrides.clear()
