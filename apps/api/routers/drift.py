"""Product-facing API router for Architectural Drift Intelligence v2 (Phase 6).

Endpoints:
POST /insights/architectural-drift/scan
GET  /insights/architectural-drift/summary
GET  /insights/architectural-drift/findings
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_scope
from brain.config.paths import resolve_repo_path
from brain.config.settings import settings
from brain.insights.codeowners import CodeownersParser, FindingOwnershipResolver
from brain.insights.drift_analyzer import scan_repository_drift
from brain.insights.drift_baseline import DriftBaseline, DriftBaselineManager
from brain.insights.drift_enforcement import ArchitectureChangeGuardEngine
from brain.insights.drift_policy import ArchitecturePolicy
from brain.insights.drift_rules import get_default_rule_registry
from brain.insights.drift_trends import DriftTrendTracker
from brain.insights.drift_waivers import DriftWaiverManager
from brain.insights.pr_risk import ArchitectureRiskCalculator
from brain.insights.pr_summary import PRMarkdownRenderer, PRSummaryModel
from brain.insights.review_queue import ReviewQueueManager

router = APIRouter(prefix="/insights/architectural-drift", tags=["architectural-drift"], dependencies=[Depends(require_api_key), Depends(require_scope("drift:read"))])


def _safe_resolve(repo_path: Optional[str]) -> Path:
    try:
        return resolve_repo_path(repo_path or settings.TARGET_REPO_PATH)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/scan", dependencies=[Depends(require_scope("drift:write"))])
async def run_drift_scan(
    repo_path: Optional[str] = Query(None, description="Path to repository to scan"),
    read_only: bool = Query(False, description="If True, computes deltas without modifying the on-disk baseline"),
):
    start_time = time.time()
    target_repo = _safe_resolve(repo_path)

    findings = scan_repository_drift(target_repo)
    baseline_mgr = DriftBaselineManager(target_repo / ".brain" / "drift_baseline.json")
    prev_baseline = baseline_mgr.load_baseline()
    evaluated = baseline_mgr.compute_deltas(findings, prev_baseline)

    rule_registry_ver = prev_baseline.rule_registry_version if prev_baseline else "1.0.0"

    # Save new baseline atomically unless read_only mode is requested
    if not read_only:
        new_baseline = DriftBaseline(
            repository_id=target_repo.name,
            source_revision="HEAD",
            created_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            rule_registry_version="1.0.0",
            findings=[item.finding.to_dict() for item in evaluated if item.delta_state != "resolved"],
        )
        baseline_mgr.save_baseline_atomic(new_baseline)
        rule_registry_ver = new_baseline.rule_registry_version

        # Record time-series trend point
        trend_tracker = DriftTrendTracker(target_repo / ".brain" / "drift_trends.jsonl")
        trend_tracker.record_scan_event(target_repo.name, evaluated)

    duration = round(time.time() - start_time, 4)
    active_rules = get_default_rule_registry().list_active_rules()

    severity_counts = {"info": 0, "warning": 0, "critical": 0}
    delta_counts = {"new": 0, "persistent": 0, "moved": 0, "resolved": 0}

    for item in evaluated:
        if item.finding.severity in severity_counts:
            severity_counts[item.finding.severity] += 1
        if item.delta_state in delta_counts:
            delta_counts[item.delta_state] += 1

    return {
        "status": "success",
        "repository": target_repo.as_posix(),
        "revision": "HEAD",
        "rules_evaluated": len(active_rules),
        "total_findings": len([e for e in evaluated if e.delta_state != "resolved"]),
        "severity_counts": severity_counts,
        "delta_counts": delta_counts,
        "duration_seconds": duration,
        "baseline_version": rule_registry_ver,
    }


@router.get("/summary", dependencies=[Depends(require_scope("drift:read"))])
async def get_drift_summary(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    baseline_mgr = DriftBaselineManager(target_repo / ".brain" / "drift_baseline.json")
    baseline = baseline_mgr.load_baseline()

    if not baseline:
        return {
            "status": "no_baseline",
            "message": "No baseline scan has been recorded yet.",
            "repository": target_repo.as_posix(),
        }

    return {
        "status": "success",
        "repository": baseline.repository_id,
        "source_revision": baseline.source_revision,
        "last_scan_utc": baseline.created_at_utc,
        "rule_registry_version": baseline.rule_registry_version,
        "total_active_violations": len(baseline.findings),
    }


@router.get("/findings", dependencies=[Depends(require_scope("drift:read"))])
async def list_drift_findings(
    repo_path: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    rule_id: Optional[str] = Query(None),
    file_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    baseline_mgr = DriftBaselineManager(target_repo / ".brain" / "drift_baseline.json")
    baseline = baseline_mgr.load_baseline()

    if not baseline:
        return {"findings": [], "total": 0}

    res = baseline.findings
    if severity:
        res = [f for f in res if f.get("severity") == severity]
    if rule_id:
        res = [f for f in res if f.get("rule_id") == rule_id]
    if file_path:
        res = [f for f in res if file_path in f.get("file_path", "")]

    return {"findings": res, "total": len(res)}


@router.get("/trends", dependencies=[Depends(require_scope("drift:read"))])
async def get_drift_trends(
    repo_path: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    target_repo = _safe_resolve(repo_path)
    trend_tracker = DriftTrendTracker(target_repo / ".brain" / "drift_trends.jsonl")
    points = trend_tracker.get_history(limit=limit)
    return {"status": "success", "repository": target_repo.as_posix(), "total_points": len(points), "history": points}


@router.post("/check", dependencies=[Depends(require_scope("drift:write"))])
async def check_architectural_drift(
    repo_path: Optional[str] = Query(None),
    base_rev: str = Query("HEAD~1", description="Base Git revision"),
    candidate_rev: str = Query("HEAD", description="Candidate Git revision"),
):
    target_repo = _safe_resolve(repo_path)
    engine = ArchitectureChangeGuardEngine(target_repo)
    result = engine.evaluate_change_guard(base_rev=base_rev, candidate_rev=candidate_rev)
    return result.to_dict()


@router.get("/policy", dependencies=[Depends(require_scope("drift:read"))])
async def get_architecture_policy(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    pol_file = target_repo / ".brain" / "architecture-policy.yaml"
    policy = ArchitecturePolicy.load_from_yaml(pol_file)
    return {"status": "success", "repository": target_repo.as_posix(), "policy": policy}


@router.get("/waivers", dependencies=[Depends(require_scope("drift:read"))])
async def get_drift_waivers(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    waiver_file = target_repo / ".brain" / "drift-waivers.yaml"
    mgr = DriftWaiverManager(waiver_file)
    waivers = [w.__dict__ for w in mgr.load_waivers()]
    return {"status": "success", "repository": target_repo.as_posix(), "total_waivers": len(waivers), "waivers": waivers}


@router.post("/pr-review", dependencies=[Depends(require_scope("drift:write"))])
async def run_pr_architecture_review(
    repo_path: Optional[str] = Query(None),
    base_rev: str = Query("HEAD~1"),
    candidate_rev: str = Query("HEAD"),
):
    target_repo = _safe_resolve(repo_path)
    engine = ArchitectureChangeGuardEngine(target_repo)
    res = engine.evaluate_change_guard(base_rev=base_rev, candidate_rev=candidate_rev)

    codeowners = CodeownersParser.discover_and_parse(target_repo)
    resolver = FindingOwnershipResolver(codeowners)

    owners_set = set()
    unowned_count = 0
    for ef in res.evaluated_findings:
        ownership = resolver.resolve_path_ownership(ef.finding.file_path)
        if ownership.is_unowned:
            unowned_count += 1
        owners_set.update(ownership.owners)

    risk = ArchitectureRiskCalculator.calculate_risk(
        new_criticals_count=len([e for e in res.evaluated_findings if e.delta_state == "new" and e.finding.severity == "critical"]),
        new_warnings_count=len([e for e in res.evaluated_findings if e.delta_state == "new" and e.finding.severity == "warning"]),
        expired_waivers_count=len([e for e in res.evaluated_findings if e.waiver_status == "waiver_expired"]),
        unowned_findings_count=unowned_count,
        files_changed_count=res.files_changed_count,
        policy_failed=(res.decision == "fail"),
    )

    summary_model = PRSummaryModel(
        repository=target_repo.name,
        base_revision=res.base_revision,
        candidate_revision=res.candidate_revision,
        decision=res.decision,
        exit_code=res.exit_code,
        policy_version="1.0.0",
        files_changed=res.files_changed_count,
        files_scanned=res.files_scanned_count,
        new_findings_count=res.counts_by_state.get("new", 0),
        persistent_findings_count=res.counts_by_state.get("persistent", 0),
        moved_findings_count=res.counts_by_state.get("moved", 0),
        resolved_findings_count=res.counts_by_state.get("resolved", 0),
        waived_count=res.waived_count,
        expired_waivers_count=len([e for e in res.evaluated_findings if e.waiver_status == "waiver_expired"]),
        risk_score=risk.numeric_score,
        risk_category=risk.risk_category,
        risk_reasons=risk.reasons,
        responsible_owners=sorted(list(owners_set)),
        unowned_findings_count=unowned_count,
    )

    comment_md = PRMarkdownRenderer.render_comment(summary_model)

    return {
        "status": "success",
        "evaluation": res.to_dict(),
        "risk_assessment": risk.to_dict(),
        "responsible_owners": sorted(list(owners_set)),
        "unowned_findings_count": unowned_count,
        "markdown_comment": comment_md,
    }


@router.get("/reviews", dependencies=[Depends(require_scope("drift:read"))])
async def list_architecture_reviews(
    repo_path: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    mgr = ReviewQueueManager(target_repo / ".brain" / "review_queue.jsonl")
    items = [i.to_dict() for i in mgr.list_reviews(state=state)]
    return {"status": "success", "repository": target_repo.as_posix(), "total_reviews": len(items), "reviews": items}


@router.get("/reviews/{review_id}", dependencies=[Depends(require_scope("drift:read"))])
async def get_architecture_review(
    review_id: str,
    repo_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    mgr = ReviewQueueManager(target_repo / ".brain" / "review_queue.jsonl")
    item = mgr.get_review(review_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Review ID '{review_id}' not found")
    return {"status": "success", "review": item.to_dict()}


@router.post("/reviews/{review_id}/acknowledge", dependencies=[Depends(require_scope("drift:write"))])
async def acknowledge_architecture_review(
    review_id: str,
    repo_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    mgr = ReviewQueueManager(target_repo / ".brain" / "review_queue.jsonl")
    item = mgr.update_state(review_id, "acknowledged")
    if not item:
        raise HTTPException(status_code=404, detail=f"Review ID '{review_id}' not found")
    return {"status": "success", "review": item.to_dict()}


@router.post("/reviews/{review_id}/approve", dependencies=[Depends(require_scope("drift:write"))])
async def approve_architecture_review(
    review_id: str,
    repo_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    mgr = ReviewQueueManager(target_repo / ".brain" / "review_queue.jsonl")
    item = mgr.update_state(review_id, "approved")
    if not item:
        raise HTTPException(status_code=404, detail=f"Review ID '{review_id}' not found")
    return {"status": "success", "review": item.to_dict()}


@router.post("/reviews/{review_id}/reject", dependencies=[Depends(require_scope("drift:write"))])
async def reject_architecture_review(
    review_id: str,
    repo_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    mgr = ReviewQueueManager(target_repo / ".brain" / "review_queue.jsonl")
    item = mgr.update_state(review_id, "rejected")
    if not item:
        raise HTTPException(status_code=404, detail=f"Review ID '{review_id}' not found")
    return {"status": "success", "review": item.to_dict()}
