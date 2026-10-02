"""Comprehensive Test Suite for Project Brain v0.7.1 Revised Champion-Challenger Platform."""

from brain.improvement.models import (
    TrajectoryRecord, TrajectoryTask, TrajectoryRouting, TrajectoryBody,
    TrajectoryOutcome, TrajectoryCost, TrajectoryPrivacy, PrivacyClassification,
    PromotionOutcome, EvaluationPoolType
)
from brain.improvement.storage.blob_store import ContentAddressedBlobStore
from brain.improvement.storage.db_store import RelationalControlPlaneStore
from brain.improvement.evaluation.pools import EvaluationPoolManager
from brain.improvement.evaluation.statistics import compute_mcnemar_exact_p_value, compute_paired_bootstrap_ci
from brain.improvement.replay.matched_rerun import MatchedRerunEngine
from brain.improvement.promotion import LexicographicPromotionEngine
from brain.improvement.safety_mutations import SafetyMutationAuditor
from brain.improvement.registry.registry import BundleRegistry


# 1. Trajectory Privacy Defaults
def test_privacy_guard_defaults():
    priv = TrajectoryPrivacy()
    assert priv.training_eligible is False
    assert priv.classification == PrivacyClassification.INTERNAL_CONFIDENTIAL


# 2. Content Addressed Blob Store & Relational Control Plane
def test_blob_and_control_plane_store(tmp_path):
    blob_store = ContentAddressedBlobStore(base_dir=tmp_path / "blobs")
    content = "Hello World Large Prompt Payload"
    blob_id = blob_store.put_blob(content)
    assert blob_id.startswith("sha256:")
    assert blob_store.get_blob(blob_id) == content

    control_store = RelationalControlPlaneStore(db_dir=tmp_path / "db", blob_store=blob_store)
    record = TrajectoryRecord(
        trajectory_id="trj-rel-1",
        task_id="tsk-1",
        session_id="sess-1",
        agent_bundle_id="bundle_champ",
        champion_bundle_id="bundle_champ",
        task=TrajectoryTask(category="cross_repo", repository_ids=["repo-1"], source_revisions={"repo-1": "sha1"}),
        routing=TrajectoryRouting(selected_route="PHASED_BRAIN_SELECTIVE", confidence=0.9),
        trajectory=TrajectoryBody(events=[{"seq": 1, "type": "route.decision"}]),
        outcome=TrajectoryOutcome(binary_success=True, diagnostic_score=92.0),
        cost=TrajectoryCost(),
    )
    traj_id = control_store.save_trajectory_header(record)
    assert traj_id == "trj-rel-1"

    loaded = control_store.get_trajectory_header("trj-rel-1")
    assert loaded is not None
    assert loaded.trajectory_id == "trj-rel-1"


# 3. 4-Tier Evaluation Pools & Contamination Guard
def test_evaluation_pool_isolation_and_contamination_guard():
    pool_mgr = EvaluationPoolManager()
    dev_tasks = pool_mgr.get_pool_tasks(EvaluationPoolType.DEVELOPMENT_CASES)
    assert len(dev_tasks) > 0

    sealed_tasks = pool_mgr.get_pool_tasks(EvaluationPoolType.SEALED_PROMOTION_HOLDOUT)
    assert sealed_tasks[0].explicit_scope == ["SEALED_SCOPE_PROTECTED"]

    assert pool_mgr.is_candidate_contaminated("import sealed_promotion_holdout", EvaluationPoolType.SEALED_PROMOTION_HOLDOUT)
    assert not pool_mgr.is_candidate_contaminated("import normal_code", EvaluationPoolType.DEVELOPMENT_CASES)


# 4. Paired Statistical Analysis (McNemar & Bootstrap CIs)
def test_paired_statistical_analysis():
    p_val = compute_mcnemar_exact_p_value(win_champ_loss_challenger=0, loss_champ_win_challenger=3)
    assert 0.0 <= p_val <= 1.0

    champ_scores = [80.0, 85.0, 90.0, 75.0]
    challenger_scores = [85.0, 90.0, 95.0, 80.0]
    ci = compute_paired_bootstrap_ci(champ_scores, challenger_scores)
    assert len(ci) == 2
    assert ci[0] <= ci[1]


# 5. Matched Rerun Sandbox Engine
def test_matched_rerun_engine():
    engine = MatchedRerunEngine()
    task = TrajectoryTask(category="cross_repo", repository_ids=["repo-1"], source_revisions={"repo-1": "sha1"})
    record = engine.run_matched_rerun("bundle_challenger", "bundle_champion", task)
    assert record.execution_mode == "matched_rerun_frozen_sandbox"
    assert record.outcome.binary_success is True


# 6. Lexicographic Promotion Gate Engine & Explicit Outcomes
def test_lexicographic_promotion_outcomes(tmp_path):
    reg = BundleRegistry(registry_dir=tmp_path)
    engine = LexicographicPromotionEngine(reg)

    # Test contamination -> REJECT_CONTAMINATION
    comp = {"task_count": 10, "architecture_violations": 0}
    outcome, rejections, report = engine.evaluate_candidate_promotion(comp, candidate_diff="import sealed_promotion_holdout", human_approved=True)
    assert outcome == PromotionOutcome.REJECT_CONTAMINATION
    assert report.outcome == PromotionOutcome.REJECT_CONTAMINATION

    # Test clean promotion -> PROMOTE
    comp_clean = {
        "champion_bundle": "bundle_champ",
        "challenger_bundle": "bundle_challenger",
        "task_count": 10,
        "architecture_violations": 0,
        "test_integrity_violations": 0,
        "empty_patch_rate_pct": 0.0,
        "success_rate_difference_ci": [0.01, 0.10],
    }
    outcome, rejections, report = engine.evaluate_candidate_promotion(comp_clean, human_approved=True)
    assert outcome == PromotionOutcome.PROMOTE
    assert report.outcome == PromotionOutcome.PROMOTE


# 7. Safety Mutation Auditor (100% Kill Rate)
def test_safety_mutation_kill_rate():
    auditor = SafetyMutationAuditor()
    all_killed, killed, total, failures = auditor.audit_safety_mutations()
    assert all_killed is True
    assert killed == total
    assert len(failures) == 0
