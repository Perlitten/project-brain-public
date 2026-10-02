"""REST API Router for Subsystem Coupling Intelligence."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_scope
from brain.config.paths import resolve_repo_path, validate_secure_repo_path
from brain.graph.generation_manager import GraphGenerationManager
from brain.insights.coupling_metrics import CouplingMetricsCalculator
from brain.insights.coupling_rules import CouplingRulesEngine
from brain.insights.cycle_detector import CycleDetector
from brain.insights.hotspots import HotspotAnalyzer
from brain.insights.subsystem_config import SubsystemConfigManager

router = APIRouter(prefix="/insights/coupling", tags=["coupling-intelligence"], dependencies=[Depends(require_api_key), Depends(require_scope("coupling:read"))])


def _safe_resolve(repo_path_arg: Optional[str]) -> Path:
    if repo_path_arg:
        return validate_secure_repo_path(repo_path_arg)
    return resolve_repo_path()


def _get_active_store_and_subsystems(repo_path: Path):
    gen_mgr = GraphGenerationManager(repo_path / ".brain")
    store = gen_mgr.get_active_graph_store()
    if not store:
        raise HTTPException(status_code=400, detail="No active Graphify v2 generation found for repository")

    sub_mgr = SubsystemConfigManager(repo_path)
    subsystems = sub_mgr.load_subsystems()
    return store, sub_mgr, subsystems


@router.get("/summary", dependencies=[Depends(require_scope("coupling:read"))])
async def get_coupling_summary(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    store, sub_mgr, subsystems = _get_active_store_and_subsystems(target_repo)

    node_metrics, sub_metrics = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    node_sub_map = {nid: m.subsystem for nid, m in node_metrics.items()}
    cycles = CycleDetector.detect_cycles(store, node_sub_map)
    hotspots = HotspotAnalyzer.analyze_hotspots(store, node_metrics, cycles)

    avg_instability = (
        sum(m.instability for m in sub_metrics.values()) / len(sub_metrics) if sub_metrics else 0.0
    )

    return {
        "status": "success",
        "repository": target_repo.as_posix(),
        "generation_id": store.generation_id,
        "subsystem_count": len(subsystems),
        "total_cycles": len(cycles),
        "total_hotspots": len(hotspots),
        "average_instability": round(avg_instability, 3),
        "subsystems": {k: v.to_dict() for k, v in sub_metrics.items()},
    }


@router.get("/subsystems", dependencies=[Depends(require_scope("coupling:read"))])
async def list_subsystems(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    store, sub_mgr, subsystems = _get_active_store_and_subsystems(target_repo)
    _, sub_metrics = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    return {
        "status": "success",
        "subsystems": {k: v.to_dict() for k, v in sub_metrics.items()},
    }


@router.get("/subsystems/{name}", dependencies=[Depends(require_scope("coupling:read"))])
async def get_subsystem_detail(name: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    store, sub_mgr, subsystems = _get_active_store_and_subsystems(target_repo)
    if name not in subsystems:
        raise HTTPException(status_code=404, detail=f"Subsystem '{name}' not found")

    node_metrics, sub_metrics = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    sub_nodes = [m.to_dict() for m in node_metrics.values() if m.subsystem == name]

    return {
        "status": "success",
        "subsystem": sub_metrics[name].to_dict(),
        "spec": subsystems[name].to_dict(),
        "member_nodes": sub_nodes,
    }


@router.get("/cycles", dependencies=[Depends(require_scope("coupling:read"))])
async def get_dependency_cycles(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    store, sub_mgr, subsystems = _get_active_store_and_subsystems(target_repo)
    node_metrics, _ = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    node_sub_map = {nid: m.subsystem for nid, m in node_metrics.items()}
    cycles = CycleDetector.detect_cycles(store, node_sub_map)
    return {"status": "success", "total_cycles": len(cycles), "cycles": [c.to_dict() for c in cycles]}


@router.get("/hotspots", dependencies=[Depends(require_scope("coupling:read"))])
async def get_architecture_hotspots(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    store, sub_mgr, subsystems = _get_active_store_and_subsystems(target_repo)
    node_metrics, _ = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    node_sub_map = {nid: m.subsystem for nid, m in node_metrics.items()}
    cycles = CycleDetector.detect_cycles(store, node_sub_map)
    hotspots = HotspotAnalyzer.analyze_hotspots(store, node_metrics, cycles)
    return {"status": "success", "total_hotspots": len(hotspots), "hotspots": [h.to_dict() for h in hotspots]}


@router.post("/analyze", dependencies=[Depends(require_scope("coupling:write"))])
async def run_coupling_analysis(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    store, sub_mgr, subsystems = _get_active_store_and_subsystems(target_repo)
    node_metrics, sub_metrics = CouplingMetricsCalculator.calculate(store, subsystems, sub_mgr)
    findings = CouplingRulesEngine.evaluate(store, subsystems, sub_mgr)
    return {
        "status": "success",
        "findings_count": len(findings),
        "findings": [f.to_dict() for f in findings],
    }
