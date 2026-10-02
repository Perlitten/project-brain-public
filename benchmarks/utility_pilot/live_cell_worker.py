"""Live Cell Worker CLI script.

Executes a single benchmark cell (Control or Treatment) in an isolated OS process.
Receives request config via JSON file/stdin and outputs cell results to JSON file.
"""

import argparse
import json
import os
import uuid
from pathlib import Path

from benchmarks.utility_pilot.gold.gold_manifests import get_all_gold_manifests
from benchmarks.utility_pilot.harness.isolation import RunWorkspace
from benchmarks.utility_pilot.harness.live_agent_adapter import LiveAgentAdapter
from benchmarks.utility_pilot.harness.telemetry import TelemetryTracker
from benchmarks.utility_pilot.schemas.run_model import ExecutionMode, RunRecord
from benchmarks.utility_pilot.evaluators.evaluator_suite import EvaluatorSuite


def main():
    parser = argparse.ArgumentParser(description="Live Cell Worker")
    parser.add_argument("--request", required=True, help="Path to request JSON file")
    parser.add_argument("--result", required=True, help="Path to result JSON file")
    args = parser.parse_args()

    req_path = Path(args.request).resolve()
    res_path = Path(args.result).resolve()

    req_data = json.loads(req_path.read_text(encoding="utf-8"))

    task_id = req_data["task_id"]
    mode_str = req_data["mode"]
    randomization_order = req_data.get("randomization_order", 1)
    repo_path = Path(req_data.get("repo_path", ".")).resolve()

    if mode_str == "control":
        mode = ExecutionMode.CONTROL
    elif mode_str == "treatment_conditioned":
        mode = ExecutionMode.TREATMENT_CONDITIONED
    elif mode_str == "treatment_automatic_routed":
        mode = ExecutionMode.TREATMENT_AUTOMATIC_ROUTED
    elif mode_str == "treatment_oracle":
        mode = ExecutionMode.TREATMENT_ORACLE
    elif mode_str == "treatment_phased_loop":
        mode = ExecutionMode.TREATMENT_PHASED_LOOP
    elif mode_str == "treatment_phased_brain":
        mode = ExecutionMode.TREATMENT_PHASED_BRAIN
    else:
        mode = ExecutionMode.TREATMENT_OPTIONAL
    run_id = f"run_{task_id}_{mode_str}_{uuid.uuid4().hex[:8]}"

    # Setup isolated workspace
    workspace = RunWorkspace(run_id=run_id, repo_path=repo_path, target_commit="1bf3e0d")
    ws_dir = workspace.setup()

    telemetry = TelemetryTracker(run_id=run_id)
    task_def = {
        "task_id": task_id,
        "prompt": req_data.get("prompt", "Solve task"),
    }

    gold_map = get_all_gold_manifests()
    if task_id in gold_map:
        gold = gold_map[task_id]
    else:
        from benchmarks.utility_pilot.tasks.holdout_v052_tasks import HOLDOUT_TASKS
        if task_id in HOLDOUT_TASKS:
            gold = HOLDOUT_TASKS[task_id]["gold"]
        else:
            gold = list(gold_map.values())[0]

    if mode in (ExecutionMode.TREATMENT_PHASED_LOOP, ExecutionMode.TREATMENT_PHASED_BRAIN):
        from benchmarks.utility_pilot.harness.phased_agent_adapter import PhasedAgentAdapter
        res = PhasedAgentAdapter.run_phased_execution(
            task_id=task_id,
            mode=mode_str,
            workspace_dir=ws_dir,
            prompt=task_def["prompt"],
            gold=gold,
            randomization_order=req_data.get("randomization_order", 1),
        )
    else:
        adapter = LiveAgentAdapter(ws_dir, mode, telemetry, task_def)
        res = adapter.run()

    adapter_metadata = res.get("adapter_execution_metadata", {})
    wall_clock = telemetry.finalize()
    patch_diff = workspace.get_patch()

    gold_map = get_all_gold_manifests()
    gold = gold_map.get(task_id, list(gold_map.values())[0])

    task_success, task_score, eval_results = EvaluatorSuite.evaluate_run(
        ws_dir, patch_diff, telemetry.files_read, gold
    )

    model_config = {
        "provider": "NVIDIA NIM API",
        "model_identifier": "meta/llama-3.1-70b-instruct",
        "is_simulated": False,
        "execution_kind": "empirical",
        "empirical_eligible": True,
        "external_llm_api_invoked": True,
        "session_id": adapter_metadata.get("session_id", "sess-phased"),
        "process_id": os.getpid(),
        "harness_request_id": adapter_metadata.get("harness_request_id", "req-phased"),
        **adapter_metadata,
    }

    rec = RunRecord(
        run_id=run_id,
        benchmark_version="v0.5.0-live-pilot-v2",
        task_id=task_id,
        mode=mode.value,
        randomization_order=randomization_order,
        model_config=model_config,
        repository_id="project-brain",
        repository_commit="1bf3e0d",
        brain_revision="v0.5.0-rc1",
        wall_clock_duration_s=wall_clock,
        input_tokens=telemetry.input_tokens,
        output_tokens=telemetry.output_tokens,
        total_tokens=telemetry.input_tokens + telemetry.output_tokens,
        tool_calls_count=len(telemetry.events),
        files_read=telemetry.files_read,
        files_changed=telemetry.files_changed,
        commands_run=[str(e.details.get("CommandLine", "")) for e in telemetry.events if e.event_type == "test_executed"],
        brain_calls=[c.to_dict() for c in telemetry.brain_calls],
        patch_diff=patch_diff,
        test_results=eval_results,
        task_success=task_success,
        task_score=task_score,
        exit_status="completed",
    )

    result_payload = {
        "record": rec.to_dict(),
        "transcript": res.get("transcript", []),
        "adapter_execution_metadata": adapter_metadata,
        "process_id": os.getpid(),
    }

    res_path.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")
    workspace.cleanup()


if __name__ == "__main__":
    main()
