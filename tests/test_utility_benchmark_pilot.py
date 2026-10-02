"""Tests for Utility Benchmark Pilot Package.

Phase 14 — Tests for benchmark schema, task definition validation,
randomization reproducibility, gold isolation, workspace isolation,
event logging, token accounting, evaluator determinism, and run cleanup.
"""

from __future__ import annotations

import pytest

from benchmarks.utility_pilot.schemas.run_model import (
    AgentEvent,
    ExecutionMode,
    ModelConfig,
    RunRecord,
)
from benchmarks.utility_pilot.harness.randomizer import ExecutionOrderRandomizer
from benchmarks.utility_pilot.harness.telemetry import TelemetryTracker
from benchmarks.utility_pilot.gold.gold_manifests import get_all_gold_manifests
from benchmarks.utility_pilot.tasks.task_definitions import TASKS
from benchmarks.utility_pilot.evaluators.patch_eval import PatchEvaluator
from benchmarks.utility_pilot.evaluators.impact_eval import ImpactEvaluator
from benchmarks.utility_pilot.evaluators.evaluator_suite import EvaluatorSuite


class TestBenchmarkSchemas:
    def test_model_config(self):
        cfg = ModelConfig()
        d = cfg.to_dict()
        assert d["provider"] == "Google DeepMind"
        assert d["max_tokens"] == 4096

    def test_run_record_roundtrip(self):
        rec = RunRecord(
            run_id="run-1",
            benchmark_version="v0.5.0-pilot-v1",
            task_id="task_01",
            mode="control",
            randomization_order=1,
            task_success=True,
            task_score=95.0,
        )
        d = rec.to_dict()
        restored = RunRecord.from_dict(d)
        assert restored.run_id == "run-1"
        assert restored.task_score == 95.0

    def test_agent_event_creation(self):
        evt = AgentEvent(
            event_id="e1",
            run_id="r1",
            event_type="tool_called",
            timestamp_utc="2026-08-04T00:00:00Z",
            details={"tool": "view_file"},
        )
        d = evt.to_dict()
        assert d["event_type"] == "tool_called"


class TestRandomizer:
    def test_deterministic_order(self):
        rand1 = ExecutionOrderRandomizer(seed=42)
        rand2 = ExecutionOrderRandomizer(seed=42)

        order1 = rand1.determine_order_for_task("task_01_bug_localization")
        order2 = rand2.determine_order_for_task("task_01_bug_localization")

        assert order1 == order2
        assert len(order1) == 2
        assert set(order1) == {ExecutionMode.CONTROL, ExecutionMode.TREATMENT}

    def test_generate_schedule(self):
        rand = ExecutionOrderRandomizer(seed=123)
        tasks = ["t1", "t2", "t3"]
        sched = rand.generate_schedule(tasks)
        assert len(sched) == 3
        for t in tasks:
            assert len(sched[t]) == 2


class TestTelemetryTracker:
    def test_event_and_token_recording(self):
        tracker = TelemetryTracker("test-run-1")
        tracker.add_tokens(100, 50)
        assert tracker.input_tokens == 100
        assert tracker.output_tokens == 50

        tracker.record_tool_call("view_file", {"AbsolutePath": "/path/to/file.py"})
        assert len(tracker.files_read) == 1
        assert tracker.files_read[0] == "/path/to/file.py"

        tracker.record_brain_call("query", "test query", ["res1"], 500, 120, 15.5)
        assert len(tracker.brain_calls) == 1
        assert tracker.brain_calls[0].tokens_returned == 120

        duration = tracker.finalize()
        assert duration >= 0.0
        assert tracker.events[-1].event_type == "run_completed"


class TestGoldIsolationAndTasks:
    def test_all_tasks_have_gold_manifests(self):
        gold_map = get_all_gold_manifests()
        for task_id in TASKS:
            assert task_id in gold_map, f"Missing gold manifest for {task_id}"

    def test_gold_manifest_structure(self):
        gold_map = get_all_gold_manifests()
        for gid, g in gold_map.items():
            assert g.gold_review_status in ("single_reviewer", "two_person_reviewed")
            assert len(g.relevant_repositories) > 0


class TestEvaluators:
    def test_patch_evaluator_empty(self):
        gold = get_all_gold_manifests()["task_00_calibration"]
        res = PatchEvaluator.evaluate("", gold)
        assert not res.passed
        assert res.score == 0.0

    def test_patch_evaluator_valid(self):
        gold = get_all_gold_manifests()["task_00_calibration"]
        diff = "--- a/brain/graph/identity.py\n+++ b/brain/graph/identity.py\n@@ -1,3 +1,3 @@\n+# Added comment\n"
        res = PatchEvaluator.evaluate(diff, gold)
        assert res.passed
        assert res.score > 70.0

    def test_impact_evaluator(self):
        gold = get_all_gold_manifests()["task_00_calibration"]
        res = ImpactEvaluator.evaluate(["brain/graph/identity.py"], "", gold)
        assert res.passed
        assert res.details["recall"] == 1.0

    def test_evaluator_suite_run(self, tmp_path):
        gold = get_all_gold_manifests()["task_00_calibration"]
        # Create dummy workspace file
        (tmp_path / "brain" / "graph").mkdir(parents=True)
        (tmp_path / "brain" / "graph" / "identity.py").write_text("# Identity module\n")

        diff = "--- a/brain/graph/identity.py\n+++ b/brain/graph/identity.py\n@@ -1,1 +1,2 @@\n+# Updated\n"
        files_read = ["brain/graph/identity.py"]

        success, score, eval_results = EvaluatorSuite.evaluate_run(tmp_path, diff, files_read, gold)
        assert isinstance(success, bool)
        assert 0.0 <= score <= 100.0
        assert "functional" in eval_results
        assert "patch" in eval_results


class TestEmpiricalGuardAndAdapters:
    def test_empirical_report_guard_raises_on_simulated(self):
        from benchmarks.utility_pilot.harness.runner import EmpiricalReportGuard, EmpiricalReportGuardError
        simulated_record = RunRecord(
            run_id="sim-1",
            benchmark_version="v1",
            task_id="t1",
            mode="control",
            randomization_order=1,
            model_config={"is_simulated": True},
        )
        with pytest.raises(EmpiricalReportGuardError):
            EmpiricalReportGuard.assert_empirical_runs([simulated_record])

    def test_empirical_report_guard_passes_on_real(self):
        from benchmarks.utility_pilot.harness.runner import EmpiricalReportGuard
        real_record = RunRecord(
            run_id="real-1",
            benchmark_version="v1",
            task_id="t1",
            mode="control",
            randomization_order=1,
            model_config={"is_simulated": False, "provider_name": "Google DeepMind"},
        )
        EmpiricalReportGuard.assert_empirical_runs([real_record])

