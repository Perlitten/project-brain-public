"""Treatment Capability Probe Runner for Utility Benchmark Pilot v2.

Executes a non-scored tool-capability probe to verify that the Treatment mode
enables the live model to autonomously call Project Brain tools and ingest evidence.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run_capability_probe():
    repo_root = Path(__file__).resolve().parent.parent.parent
    out_dir = repo_root / "reports" / "utility-benchmark-live-pilot-v2" / "capability-probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=== Executing Treatment Capability Probe ===")

    # Probe 1: Control Probe (Brain tools absent)
    ctrl_req = {
        "task_id": "probe_control",
        "mode": "control",
        "randomization_order": 1,
        "repo_path": str(repo_root),
        "prompt": "Inspect `brain/graph/identity.py` and report its primary function.",
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_req:
        json.dump(ctrl_req, f_req)
        ctrl_req_path = f_req.name

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_res:
        ctrl_res_path = f_res.name

    subprocess.run(
        [sys.executable, "-m", "benchmarks.utility_pilot.live_cell_worker", "--request", ctrl_req_path, "--result", ctrl_res_path],
        cwd=str(repo_root),
        check=True,
    )

    ctrl_res = json.loads(Path(ctrl_res_path).read_text(encoding="utf-8"))

    # Probe 2: Treatment Probe (Brain tools present)
    treat_req = {
        "task_id": "probe_treatment",
        "mode": "treatment",
        "randomization_order": 2,
        "repo_path": str(repo_root),
        "prompt": "Use Project Brain context tool `brain_context_pack` to inspect architectural drift rules and active symbols for `brain/graph/identity.py`.",
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_req:
        json.dump(treat_req, f_req)
        treat_req_path = f_req.name

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_res:
        treat_res_path = f_res.name

    subprocess.run(
        [sys.executable, "-m", "benchmarks.utility_pilot.live_cell_worker", "--request", treat_req_path, "--result", treat_res_path],
        cwd=str(repo_root),
        check=True,
    )

    treat_res = json.loads(Path(treat_res_path).read_text(encoding="utf-8"))

    # Verify Process Isolation (distinct PIDs)
    ctrl_pid = ctrl_res.get("process_id")
    treat_pid = treat_res.get("process_id")
    distinct_pids = ctrl_pid != treat_pid and ctrl_pid != os.getpid() and treat_pid != os.getpid()

    # Extract Brain Calls in Treatment Probe
    treat_brain_calls = treat_res.get("record", {}).get("brain_calls", [])
    brain_capability_exercised = len(treat_brain_calls) >= 1 or any(
        m.get("tool_call") == "brain_context_pack" or "Brain Context Evidence" in str(m.get("content", ""))
        for m in treat_res.get("transcript", [])
    )

    probe_summary = {
        "status": "PASSED" if distinct_pids else "WARNING_SAME_PID",
        "distinct_process_ids_proven": distinct_pids,
        "control_pid": ctrl_pid,
        "treatment_pid": treat_pid,
        "orchestrator_pid": os.getpid(),
        "brain_capability_exercised": brain_capability_exercised,
        "treatment_brain_calls_count": len(treat_brain_calls),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    (out_dir / "probe-summary.json").write_text(json.dumps(probe_summary, indent=2), encoding="utf-8")
    (out_dir / "control-probe.json").write_text(json.dumps(ctrl_res, indent=2), encoding="utf-8")
    (out_dir / "treatment-probe.json").write_text(json.dumps(treat_res, indent=2), encoding="utf-8")

    print(f"Control PID: {ctrl_pid} | Treatment PID: {treat_pid} | Orchestrator PID: {os.getpid()}")
    print(f"Distinct PIDs Proven: {distinct_pids}")
    print(f"Treatment Brain Capability Exercised: {brain_capability_exercised}")

    # Clean up temp files
    Path(ctrl_req_path).unlink(missing_ok=True)
    Path(ctrl_res_path).unlink(missing_ok=True)
    Path(treat_req_path).unlink(missing_ok=True)
    Path(treat_res_path).unlink(missing_ok=True)


if __name__ == "__main__":
    run_capability_probe()
