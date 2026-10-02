"""Test Suite for Milestone v0.8.0 Automated Hypothesis Factory."""

import pytest
from brain.improvement.hypothesis_generator import SingleLeverHypothesisGenerator
from brain.improvement.report_generator import PromotionReportGenerator
from brain.improvement.weekly_factory import WeeklyImprovementFactory
from brain.improvement.models import CandidateHypothesis, PairedEvaluationReport, PromotionOutcome, EvaluationPoolType


def test_single_lever_hypothesis_generator():
    gen = SingleLeverHypothesisGenerator()
    hyp = gen.generate_hypothesis("cluster-premature-edit", [], target_lever="routing_thresholds")
    assert isinstance(hyp, CandidateHypothesis)
    assert hyp.lever_changes[0]["lever"] == "routing_thresholds"

    with pytest.raises(ValueError, match="is not in allowed safe levers"):
        gen.generate_hypothesis("cluster-1", [], target_lever="unauthorized_forbidden_lever")


def test_promotion_report_generator(tmp_path):
    report_gen = PromotionReportGenerator(output_dir=tmp_path)
    hyp = CandidateHypothesis(
        candidate_id="cnd-test-1",
        parent_bundle_id="bundle_champ",
        failure_cluster_ids=["cluster-1"],
        hypothesis="Test single lever hypothesis",
        lever_changes=[{"lever": "routing_thresholds", "parameter": "threshold", "new_value": "0.85"}],
        expected_benefit={"gain": 5.0},
        risk_predictions=[],
        evaluation_plan_id="eval-plan-1",
        author={"system": "test"},
    )
    eval_report = PairedEvaluationReport(
        champion_bundle_id="bundle_champ",
        challenger_bundle_id="cnd-test-1",
        evaluation_pool_type=EvaluationPoolType.SEALED_PROMOTION_HOLDOUT,
        task_count=10,
        paired_success_concordance={"win_win": 8, "loss_loss": 0, "champ_win_challenger_loss": 0, "challenger_win_champ_loss": 2},
        mcnemar_p_value=0.157,
        success_rate_difference_ci=[0.02, 0.12],
        median_score_delta_ci=[2.0, 8.0],
        non_inferiority_margin=-0.05,
        non_inferiority_passed=True,
        outcome=PromotionOutcome.PROMOTE,
        gate_rejections=[],
    )

    files = report_gen.generate_report(hyp, eval_report)
    assert files["json"].exists()
    assert files["markdown"].exists()
    assert "PROMOTE" in files["markdown"].read_text(encoding="utf-8")


def test_weekly_improvement_factory_cycle(tmp_path):
    factory = WeeklyImprovementFactory()
    res = factory.run_weekly_cycle("cluster-premature-edit")
    assert res["status"] == "weekly_cycle_completed"
    assert res["outcome"] == "PROMOTE"
    assert "json" in res["reports"]
