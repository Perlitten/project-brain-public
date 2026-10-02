"""Benchmark Task Runner for Utility Benchmark Pilot.

Executes a single benchmark run (Control or Treatment mode) in an isolated workspace,
collects telemetry, runs evaluators, and generates structured run records.
Supports RealAgentAdapter for real executions and SimulatedAgentAdapter for unit testing.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from benchmarks.utility_pilot.evaluators.evaluator_suite import EvaluatorSuite
from benchmarks.utility_pilot.gold.gold_manifests import get_all_gold_manifests
from benchmarks.utility_pilot.harness.isolation import RunWorkspace
from benchmarks.utility_pilot.harness.local_script_agent_adapter import LocalScriptAgentAdapter
from benchmarks.utility_pilot.harness.telemetry import TelemetryTracker
from benchmarks.utility_pilot.schemas.run_model import (
    EventType,
    ExecutionMode,
    ModelConfig,
    RunRecord,
)
from benchmarks.utility_pilot.tasks.task_definitions import TASKS


class EmpiricalReportGuardError(RuntimeError):
    """Raised when an empirical report generator receives simulated run data."""
    pass


class EmpiricalReportGuard:
    """Guards empirical benchmark reports against synthetic/simulated data."""

    @staticmethod
    def assert_empirical_runs(runs: List[RunRecord]):
        for r in runs:
            if getattr(r, "is_simulated", False) or r.model_config.get("is_simulated", False):
                raise EmpiricalReportGuardError(
                    f"Run '{r.run_id}' is simulated/synthetic. Cannot produce empirical conclusions from simulated runs."
                )


class SimulatedAgentAdapter:
    """Explicit synthetic run adapter for unit testing only."""

    @staticmethod
    def run(ws_dir: Path, mode: ExecutionMode, telemetry: TelemetryTracker, task_def: Dict[str, Any]):
        task_id = task_def["task_id"]
        if mode == ExecutionMode.TREATMENT:
            telemetry.record_brain_call(
                endpoint_or_tool="prepare_task_context",
                query=f"Simulated query for {task_id}",
                result_ids=["sym_1"],
                bytes_returned=1000,
                tokens_returned=250,
                latency_ms=10.0,
                used_by_agent=True,
                context_type="synthetic_simulated",
            )
            telemetry.add_tokens(input_tokens=500, output_tokens=200)
        else:
            telemetry.add_tokens(input_tokens=700, output_tokens=300)


class BenchmarkRunner:
    """Orchestrates single benchmark run execution."""

    def __init__(self, repo_path: Path, output_dir: Path):
        self.repo_path = repo_path.resolve()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def execute_run(
        self,
        task_id: str,
        mode: ExecutionMode,
        randomization_order: int,
        model_config: Optional[ModelConfig] = None,
        agent_executor_func: Optional[Any] = None,
        use_real_agent: bool = True,
        use_live_agent: bool = True,
    ) -> RunRecord:
        """Execute a single run cell (Control or Treatment)."""
        run_id = f"run_{task_id}_{mode.value}_{uuid.uuid4().hex[:8]}"
        m_config = model_config or ModelConfig()

        task_def = TASKS.get(task_id)
        if not task_def:
            raise ValueError(f"Task '{task_id}' not found in TASKS registry")

        gold_manifests = get_all_gold_manifests()
        gold = gold_manifests.get(task_id)
        if not gold:
            raise ValueError(f"Gold manifest for '{task_id}' not found")

        # Setup isolated workspace
        target_commit = task_def.get("target_commit", "4c4d973")
        workspace = RunWorkspace(run_id, self.repo_path, target_commit)
        ws_dir = workspace.setup()

        # Telemetry
        telemetry = TelemetryTracker(run_id)
        telemetry.log_event(
            EventType.RUN_STARTED,
            {"task_id": task_id, "mode": mode.value, "ws_dir": str(ws_dir)},
        )

        anomalies = []
        exit_status = "completed"
        is_simulated = False
        adapter_execution_metadata = {}

        # Execute agent task via LiveAgentAdapter, LocalScriptAgentAdapter, or custom executor
        try:
            if agent_executor_func:
                agent_executor_func(ws_dir, mode, telemetry, task_def)
            elif use_live_agent:
                from benchmarks.utility_pilot.harness.live_agent_adapter import LiveAgentAdapter
                adapter = LiveAgentAdapter(ws_dir, mode, telemetry, task_def)
                res = adapter.run()
                adapter_execution_metadata = res.get("adapter_execution_metadata", {})
                is_simulated = False
            elif use_real_agent:
                adapter = LocalScriptAgentAdapter(ws_dir, mode, telemetry, task_def)
                res = adapter.run()
                adapter_execution_metadata = res.get("adapter_execution_metadata", {})
                is_simulated = True  # Local script loop is simulated
            else:
                is_simulated = True
                SimulatedAgentAdapter.run(ws_dir, mode, telemetry, task_def)
        except Exception as exc:
            exit_status = "error"
            anomalies.append(f"Agent execution exception: {exc}")

        # Finalize telemetry
        wall_clock = telemetry.finalize()

        # Capture patch diff
        patch_diff = workspace.get_patch()

        # Run Evaluators
        task_success, task_score, eval_results = EvaluatorSuite.evaluate_run(
            ws_dir, patch_diff, telemetry.files_read, gold
        )

        model_cfg_dict = m_config.to_dict()
        model_cfg_dict["is_simulated"] = is_simulated
        if adapter_execution_metadata:
            model_cfg_dict.update(adapter_execution_metadata)

        # Build Run Record
        record = RunRecord(
            run_id=run_id,
            benchmark_version="v0.5.0-pilot-v1",
            task_id=task_id,
            mode=mode.value,
            randomization_order=randomization_order,
            model_config=model_cfg_dict,
            repository_id="project-brain",
            repository_commit=target_commit,
            brain_revision="v0.5.0-rc1",
            environment_fingerprint=hashlib.sha256(f"{os.name}_{ws_dir}".encode()).hexdigest()[:12],
            start_time_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(telemetry.start_time)),
            end_time_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            wall_clock_duration_s=wall_clock,
            input_tokens=telemetry.input_tokens,
            output_tokens=telemetry.output_tokens,
            total_tokens=telemetry.input_tokens + telemetry.output_tokens,
            tool_calls_count=telemetry.tool_calls_count,
            files_read=telemetry.files_read,
            files_changed=telemetry.files_changed,
            commands_run=telemetry.commands_run,
            brain_calls=[b.to_dict() for b in telemetry.brain_calls],
            patch_diff=patch_diff,
            test_results={"evaluator_summary": eval_results},
            evaluator_results=eval_results,
            task_success=task_success,
            task_score=task_score,
            exit_status=exit_status,
            anomalies=anomalies,
        )

        # Save run output files
        run_out_dir = self.output_dir / task_id / mode.value
        run_out_dir.mkdir(parents=True, exist_ok=True)
        (run_out_dir / "run_record.json").write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        (run_out_dir / "patch.diff").write_text(patch_diff, encoding="utf-8")
        (run_out_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(e.to_dict()) for e in telemetry.events), encoding="utf-8"
        )
        if adapter_execution_metadata:
            (run_out_dir / "adapter-execution-metadata.json").write_text(json.dumps(adapter_execution_metadata, indent=2), encoding="utf-8")

        # Cleanup workspace
        workspace.cleanup()

        return record
