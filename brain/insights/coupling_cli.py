"""CLI Interface for Subsystem Coupling Intelligence."""

import argparse
import json
import sys
from pathlib import Path

from brain.graph.generation_manager import GraphGenerationManager
from brain.insights.coupling_metrics import CouplingMetricsCalculator
from brain.insights.cycle_detector import CycleDetector
from brain.insights.hotspots import HotspotAnalyzer
from brain.insights.subsystem_config import SubsystemConfigManager


def main():
    parser = argparse.ArgumentParser(description="Project Brain Coupling Intelligence CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # summary
    summary_p = subparsers.add_parser("summary", help="Show coupling summary")
    summary_p.add_argument("--repo", default=".", help="Repository root path")

    # cycles
    cycles_p = subparsers.add_parser("cycles", help="List dependency cycles")
    cycles_p.add_argument("--repo", default=".", help="Repository root path")

    # hotspots
    hotspots_p = subparsers.add_parser("hotspots", help="List architecture hotspots")
    hotspots_p.add_argument("--repo", default=".", help="Repository root path")

    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    gen_mgr = GraphGenerationManager(repo_path / ".brain")
    store = gen_mgr.get_active_graph_store()

    if not store:
        print("No active Graphify v2 generation found.", file=sys.stderr)
        sys.exit(1)

    sub_mgr = SubsystemConfigManager(repo_path)
    subsystems = sub_mgr.load_subsystems()

    node_metrics, sub_metrics = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    node_sub_map = {nid: m.subsystem for nid, m in node_metrics.items()}
    cycles = CycleDetector.detect_cycles(store, node_sub_map)
    hotspots = HotspotAnalyzer.analyze_hotspots(store, node_metrics, cycles)

    if args.command == "summary":
        print(json.dumps({
            "subsystems": {k: v.to_dict() for k, v in sub_metrics.items()},
            "total_cycles": len(cycles),
            "total_hotspots": len(hotspots),
        }, indent=2))
    elif args.command == "cycles":
        print(json.dumps([c.to_dict() for c in cycles], indent=2))
    elif args.command == "hotspots":
        print(json.dumps([h.to_dict() for h in hotspots], indent=2))


if __name__ == "__main__":
    main()
