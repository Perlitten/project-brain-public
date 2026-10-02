"""Holdout Evaluation Runner for Project Brain v0.5.2 (30 Live Cells).

Executes a 30-cell 3-mode empirical matrix across the 10 unseen holdout tasks (H1 - H10):
- Control (Single-loop control)
- Phased No-Brain (Phased execution loop without Brain)
- Phased Brain (Phased execution loop with routed Brain)
"""

import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from benchmarks.utility_pilot.tasks.holdout_v052_tasks import HOLDOUT_TASKS


def run_holdout_evaluation():
    repo_root = Path(__file__).resolve().parent.parent.parent
    base_out_dir = repo_root / "reports" / "v0.5.2-agent-execution" / "holdout-evaluation"
    base_out_dir.mkdir(parents=True, exist_ok=True)

    task_ids = list(HOLDOUT_TASKS.keys())
    modes = ["control", "treatment_phased_loop", "treatment_phased_brain"]

    print("=== Executing 30-Cell Unseen Holdout Evaluation (v0.5.2 RC1) ===", flush=True)
    print(f"Matrix: {len(task_ids)} tasks x {len(modes)} modes = {len(task_ids) * len(modes)} live runs\n", flush=True)

    random.seed(9988)
    task_results = {}

    for t_idx, task_id in enumerate(task_ids, start=1):
        task_def = HOLDOUT_TASKS[task_id]
        print(f"[{t_idx}/{len(task_ids)}] Holdout Task: {task_id}", flush=True)

        task_modes = list(modes)
        random.shuffle(task_modes)

        task_dir = base_out_dir / f"holdout-0{t_idx}" if t_idx < 10 else base_out_dir / f"holdout-{t_idx}"
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
            "treatment_phased_loop": mode_runs.get("treatment_phased_loop", {}).get("record", {}),
            "treatment_phased_brain": mode_runs.get("treatment_phased_brain", {}).get("record", {}),
        }
        (task_dir / "comparison-holdout.json").write_text(json.dumps(task_comp, indent=2), encoding="utf-8")
        task_results[task_id] = task_comp

    ctrl_scores = [t.get("control", {}).get("task_score", 0.0) for t in task_results.values()]
    phased_no_brain_scores = [t.get("treatment_phased_loop", {}).get("task_score", 0.0) for t in task_results.values()]
    phased_brain_scores = [t.get("treatment_phased_brain", {}).get("task_score", 0.0) for t in task_results.values()]

    agg = {
        "benchmark_version": "v0.5.2-holdout-evaluation",
        "total_tasks": 10,
        "total_runs": 30,
        "mode_summary": {
            "control_single": {
                "avg_score": round(sum(ctrl_scores) / 10.0, 2),
                "successes": sum(1 for t in task_results.values() if t.get("control", {}).get("task_success")),
            },
            "phased_no_brain": {
                "avg_score": round(sum(phased_no_brain_scores) / 10.0, 2),
                "successes": sum(1 for t in task_results.values() if t.get("treatment_phased_loop", {}).get("task_success")),
            },
            "phased_brain": {
                "avg_score": round(sum(phased_brain_scores) / 10.0, 2),
                "successes": sum(1 for t in task_results.values() if t.get("treatment_phased_brain", {}).get("task_success")),
            },
        },
        "tasks": task_results,
    }

    (base_out_dir / "aggregate-holdout-results.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")
    print("\n=== Holdout Evaluation Complete ===", flush=True)


if __name__ == "__main__":
    run_holdout_evaluation()
