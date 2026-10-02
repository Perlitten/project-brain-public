"""Project Brain v0.4.0 End-to-End Analysis and Verification Script."""

import json
import sys
from pathlib import Path

# Add project root to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from brain.graph.builder_v2 import GraphBuilderV2
from brain.insights.coupling_metrics import CouplingMetricsCalculator
from brain.insights.cycle_detector import CycleDetector
from brain.insights.hotspots import HotspotAnalyzer
from brain.insights.impact_engine import ImpactAnalysisEngine
from brain.insights.remediation_manager import RemediationPlanManager
from brain.insights.remediation_package import RemediationPackageWriter
from brain.insights.remediation_strategies import RemediationStrategyRegistry
from brain.insights.subsystem_config import SubsystemConfigManager


def main():
    print("=== Project Brain v0.4.0 Real Codebase Analysis ===")
    out_dir = repo_root / "reports" / "v0.4.0-deep-coupling"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Graphify v2 Build
    print("[1/5] Building Graphify v2 Knowledge Graph...")
    builder = GraphBuilderV2(repo_root)
    meta, report = builder.build_generation(git_revision="v0.4.0-rc1")
    store = builder.mgr.get_active_graph_store()
    print(f"      Nodes: {meta.metrics['total_nodes']}, Relationships: {meta.metrics['total_relationships']}")
    (out_dir / "graph_summary.json").write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")

    # 2. Subsystem & Coupling Analysis
    print("[2/5] Analyzing Subsystems and Coupling Metrics...")
    sub_mgr = SubsystemConfigManager(repo_root)
    subsystems = sub_mgr.load_subsystems()
    node_metrics, c_metrics = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    c_dicts = {k: v.to_dict() for k, v in c_metrics.items()}
    (out_dir / "subsystem_coupling.json").write_text(json.dumps(c_dicts, indent=2), encoding="utf-8")

    # 3. Cycle & Hotspot Detection
    print("[3/5] Detecting Dependency Cycles and Architecture Hotspots...")
    node_sub_map = {nid: m.subsystem for nid, m in node_metrics.items()}
    cycles = CycleDetector.detect_cycles(store, node_sub_map)
    hotspots = HotspotAnalyzer.analyze_hotspots(store, node_metrics, cycles)
    c_list = [c.to_dict() for c in cycles]
    h_dicts = [h.to_dict() for h in hotspots]
    (out_dir / "cycles_and_hotspots.json").write_text(
        json.dumps({"cycles": c_list, "hotspots": h_dicts}, indent=2), encoding="utf-8"
    )

    # 4. Deep Change-Impact Traversal
    print("[4/5] Running Deep Change-Impact Analysis...")
    imp_engine = ImpactAnalysisEngine(repo_root)
    impact_res = imp_engine.analyze_impact(base_rev="HEAD~1", cand_rev="HEAD")
    (out_dir / "impact_analysis.json").write_text(json.dumps(impact_res.to_dict(), indent=2), encoding="utf-8")

    # 5. Assisted Remediation Plan Generation
    print("[5/5] Generating Sample Remediation Plan Package...")
    rem_mgr = RemediationPlanManager(repo_root / ".brain")
    finding = {"rule_id": "COUPLING-001", "file_path": "apps/api/routers/coupling.py", "description": "Subsystem boundary check"}
    plan = RemediationStrategyRegistry.create_plan_for_finding("project-brain", "HEAD", finding, repo_root)
    rem_mgr.save_plan(plan)
    pkg_dir = out_dir / "remediation_plan_sample"
    RemediationPackageWriter.create_package(pkg_dir, plan)

    print(f"=== v0.4.0 Real Analysis Complete. Outputs written to {out_dir.as_posix()} ===")


if __name__ == "__main__":
    main()
