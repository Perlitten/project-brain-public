"""3-Mode Oracle Evidence Diagnostic Experiment Runner.

Executes a 15-cell empirical matrix (5 tasks x 3 modes):
- Mode A (control): No evidence pack. Standard repository inspection tools.
- Mode B (brain_evidence): Frozen Evidence Pack v2 from Brain index.
- Mode C (oracle_evidence): Manually curated, reviewed ideal Oracle Evidence Pack.

Uses EvaluatorV2 to compute diagnostic_score_v2 alongside legacy_score.
Outputs results to reports/v0.5.1-oracle-ablation/.
"""

import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from benchmarks.utility_pilot.tasks.task_definitions import TASKS


def run_oracle_ablation_experiment():
    repo_root = Path(__file__).resolve().parent.parent.parent
    base_out_dir = repo_root / "reports" / "v0.5.1-oracle-ablation"
    tasks_out_dir = base_out_dir / "tasks"
    tasks_out_dir.mkdir(parents=True, exist_ok=True)

    task_ids = [
        "task_01_bug_localization",
        "task_02_multifile_change",
        "task_03_arch_boundary",
        "task_04_cross_repo_contract",
        "task_05_regression_remediation",
    ]

    modes = ["control", "treatment_automatic_routed", "treatment_oracle"]

    print("=== Executing 3-Mode Oracle Evidence Diagnostic Experiment ===", flush=True)
    print(f"Matrix: {len(task_ids)} tasks x {len(modes)} modes = {len(task_ids) * len(modes)} live runs\n", flush=True)

    random.seed(3333)
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
            "treatment_automatic_routed": mode_runs.get("treatment_automatic_routed", {}).get("record", {}),
            "treatment_oracle": mode_runs.get("treatment_oracle", {}).get("record", {}),
        }
        (task_dir / "comparison-oracle.json").write_text(json.dumps(task_comp, indent=2), encoding="utf-8")
        task_results[task_id] = task_comp

    ctrl_scores = [t.get("control", {}).get("task_score", 0.0) for t in task_results.values()]
    brain_scores = [t.get("treatment_automatic_routed", {}).get("task_score", 0.0) for t in task_results.values()]
    oracle_scores = [t.get("treatment_oracle", {}).get("task_score", 0.0) for t in task_results.values()]

    ctrl_avg = round(sum(ctrl_scores) / 5.0, 2)
    brain_avg = round(sum(brain_scores) / 5.0, 2)
    oracle_avg = round(sum(oracle_scores) / 5.0, 2)

    # Physical Diagnosis Matrix Logic:
    if oracle_avg > ctrl_avg and brain_avg <= ctrl_avg:
        diagnosis = "CASE_1_BRAIN_RETRIEVAL_WEAK (Evidence helps, but Brain retrieval quality is insufficient)"
        next_step = "IMPROVE_BRAIN_RETRIEVAL"
    elif oracle_avg > brain_avg and brain_avg > ctrl_avg:
        diagnosis = "CASE_2_BRAIN_EVIDENCE_USEFUL_BUT_SUBOPTIMAL (Brain provides useful evidence but below achievable quality)"
        next_step = "IMPROVE_BRAIN_RANKING_AND_FUSION"
    elif brain_avg >= oracle_avg and brain_avg > ctrl_avg:
        diagnosis = "CASE_3_BRAIN_EVIDENCE_ADEQUATE (Brain evidence quality is adequate; routing/integration is main issue)"
        next_step = "IMPROVE_MODEL_INTEGRATION"
    elif brain_avg <= ctrl_avg and oracle_avg <= ctrl_avg:
        diagnosis = "CASE_4_MODEL_OR_EVALUATOR_CAPACITY_LIMITATION (Neither Brain nor Oracle evidence improves performance)"
        next_step = "EVALUATE_MODEL_CAPACITY_OR_TASK_DESIGN"
    else:
        diagnosis = "CASE_5_EVIDENCE_DISTRACTION (Extra context distracts model or tasks do not justify extra context)"
        next_step = "RESTRICT_BRAIN_PRODUCT_SCOPE"

    agg = {
        "benchmark_version": "v0.5.1-oracle-ablation",
        "total_tasks": 5,
        "total_runs": 15,
        "mode_summary": {
            "control": {
                "successes": sum(1 for t in task_results.values() if t.get("control", {}).get("task_success")),
                "avg_score": ctrl_avg,
                "avg_tokens": round(sum(t.get("control", {}).get("total_tokens", 0) for t in task_results.values()) / 5.0, 2),
            },
            "brain_evidence": {
                "successes": sum(1 for t in task_results.values() if t.get("treatment_automatic_routed", {}).get("task_success")),
                "avg_score": brain_avg,
                "avg_tokens": round(sum(t.get("treatment_automatic_routed", {}).get("total_tokens", 0) for t in task_results.values()) / 5.0, 2),
            },
            "oracle_evidence": {
                "successes": sum(1 for t in task_results.values() if t.get("treatment_oracle", {}).get("task_success")),
                "avg_score": oracle_avg,
                "avg_tokens": round(sum(t.get("treatment_oracle", {}).get("total_tokens", 0) for t in task_results.values()) / 5.0, 2),
            },
        },
        "physical_diagnosis": diagnosis,
        "recommended_next_action": next_step,
        "tasks": task_results,
    }

    (base_out_dir / "aggregate-oracle-results.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")

    diag_md = (
        "# Project Brain Oracle Evidence Diagnostic Report\n\n"
        f"## Physical Diagnosis Outcome\n`{diagnosis}`\n\n"
        f"## Recommended Next Action\n`{next_step}`\n\n"
        "## Summary Scores\n"
        f"- **Control (Mode A)**: Avg Score {ctrl_avg}\n"
        f"- **Brain Evidence Pack (Mode B)**: Avg Score {brain_avg}\n"
        f"- **Oracle Evidence Pack (Mode C)**: Avg Score {oracle_avg}\n"
    )
    (base_out_dir / "final-diagnosis.md").write_text(diag_md, encoding="utf-8")

    print("\n=== Oracle Evidence Diagnostic Experiment Complete ===", flush=True)
    print(f"Diagnosis: {diagnosis}")


if __name__ == "__main__":
    run_oracle_ablation_experiment()
