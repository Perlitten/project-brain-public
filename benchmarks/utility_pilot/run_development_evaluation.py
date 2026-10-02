"""Development Set Evaluation Runner.

Evaluates 4 modes across 5 development tasks (20 live cells in separate OS processes):
- Mode 1 (control): No Brain tools in schema.
- Mode 2 (treatment_optional): Optional Brain tools in schema.
- Mode 3 (treatment_conditioned): Optional Brain tools + workflow policy.
- Mode 4 (treatment_automatic_routed): Deterministic Task Utility Router + validated Evidence Pack v2.

Outputs results to reports/v0.5.1-brain-utility/development-evaluation/.
"""

import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from benchmarks.utility_pilot.tasks.task_definitions import TASKS


def run_development_evaluation():
    repo_root = Path(__file__).resolve().parent.parent.parent
    base_out_dir = repo_root / "reports" / "v0.5.1-brain-utility" / "development-evaluation"
    tasks_out_dir = base_out_dir / "tasks"
    tasks_out_dir.mkdir(parents=True, exist_ok=True)

    task_ids = [
        "task_01_bug_localization",
        "task_02_multifile_change",
        "task_03_arch_boundary",
        "task_04_cross_repo_contract",
        "task_05_regression_remediation",
    ]

    modes = ["control", "treatment_optional", "treatment_conditioned", "treatment_automatic_routed"]

    print("=== Executing Development Set 4-Mode Evaluation ===", flush=True)
    print(f"Matrix: {len(task_ids)} tasks x {len(modes)} modes = {len(task_ids) * len(modes)} live runs\n", flush=True)

    random.seed(777)
    task_results = {}

    for t_idx, task_id in enumerate(task_ids, start=1):
        task_def = TASKS[task_id]
        print(f"[{t_idx}/{len(task_ids)}] Task: {task_id}", flush=True)

        task_modes = list(modes)
        random.shuffle(task_modes)

        task_dir = tasks_out_dir / f"task-0{t_idx}"
        task_dir.mkdir(parents=True, exist_ok=True)

        mode_runs = {}

        for m_idx, mode_str in enumerate(task_modes, start=1):
            print(f"  --> Running cell {mode_str.upper()} (order {m_idx})...", flush=True)

            req_payload = {
                "task_id": task_id,
                "mode": mode_str,
                "randomization_order": m_idx,
                "repo_path": str(repo_root),
                "prompt": task_def["prompt"],
            }

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_req:
                json.dump(req_payload, f_req)
                req_path = f_req.name

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_res:
                res_path = f_res.name

            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "benchmarks.utility_pilot.live_cell_worker",
                        "--request",
                        req_path,
                        "--result",
                        res_path,
                    ],
                    cwd=str(repo_root),
                    check=True,
                    timeout=300,
                )
            except Exception as exc:
                print(f"  [ERROR] Cell {mode_str.upper()} failed/timed out: {exc}", flush=True)
                fallback_res = {
                    "record": {
                        "run_id": f"run_{task_id}_{mode_str}_error",
                        "task_id": task_id,
                        "mode": mode_str,
                        "randomization_order": m_idx,
                        "model_config": {"process_id": os.getpid(), "error": str(exc)},
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "wall_clock_duration_s": 300.0,
                        "task_success": False,
                        "task_score": 0.0,
                        "brain_calls": [],
                        "patch_diff": "",
                        "test_results": {},
                    },
                    "transcript": [{"role": "error", "content": str(exc)}],
                }
                Path(res_path).write_text(json.dumps(fallback_res, indent=2), encoding="utf-8")

            res_data = json.loads(Path(res_path).read_text(encoding="utf-8"))
            mode_runs[mode_str] = res_data

            cell_dir = task_dir / mode_str
            cell_dir.mkdir(parents=True, exist_ok=True)

            rec = res_data["record"]
            (cell_dir / "run.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
            (cell_dir / "provider-metadata.json").write_text(json.dumps(rec.get("model_config", {}), indent=2), encoding="utf-8")
            (cell_dir / "transcript.json").write_text(json.dumps(res_data.get("transcript", []), indent=2), encoding="utf-8")
            (cell_dir / "patch.diff").write_text(rec.get("patch_diff", ""), encoding="utf-8")

            Path(req_path).unlink(missing_ok=True)
            Path(res_path).unlink(missing_ok=True)

        task_comp = {
            "task_id": task_id,
            "title": task_def["title"],
            "randomization_order": task_modes,
            "control": mode_runs.get("control", {}).get("record", {}),
            "treatment_optional": mode_runs.get("treatment_optional", {}).get("record", {}),
            "treatment_conditioned": mode_runs.get("treatment_conditioned", {}).get("record", {}),
            "treatment_automatic_routed": mode_runs.get("treatment_automatic_routed", {}).get("record", {}),
        }
        (task_dir / "comparison-dev.json").write_text(json.dumps(task_comp, indent=2), encoding="utf-8")
        task_results[task_id] = task_comp

    agg = {
        "benchmark_version": "v0.5.1-dev-eval",
        "total_tasks": 5,
        "total_runs": 20,
        "mode_summary": {
            "control": {
                "successes": sum(1 for t in task_results.values() if t.get("control", {}).get("task_success")),
                "avg_score": round(sum(t.get("control", {}).get("task_score", 0.0) for t in task_results.values()) / 5.0, 2),
                "avg_tokens": round(sum(t.get("control", {}).get("total_tokens", 0) for t in task_results.values()) / 5.0, 2),
            },
            "treatment_automatic_routed": {
                "successes": sum(1 for t in task_results.values() if t.get("treatment_automatic_routed", {}).get("task_success")),
                "avg_score": round(sum(t.get("treatment_automatic_routed", {}).get("task_score", 0.0) for t in task_results.values()) / 5.0, 2),
                "avg_tokens": round(sum(t.get("treatment_automatic_routed", {}).get("total_tokens", 0) for t in task_results.values()) / 5.0, 2),
                "brain_calls": sum(len(t.get("treatment_automatic_routed", {}).get("brain_calls", [])) for t in task_results.values()),
            },
        },
        "tasks": task_results,
    }

    (base_out_dir / "aggregate-dev-results.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")
    print("\n=== Development Set Evaluation Complete ===", flush=True)


if __name__ == "__main__":
    run_development_evaluation()
