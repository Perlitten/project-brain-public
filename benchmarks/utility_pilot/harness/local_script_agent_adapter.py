"""Local Script Agent Adapter for Utility Benchmark Pilot.

Executes a local scripted tool loop against a disposable workspace directory for harness testing.
NOTE: This adapter is SIMULATED and NOT eligible for empirical benchmark reports.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from benchmarks.utility_pilot.harness.telemetry import TelemetryTracker
from benchmarks.utility_pilot.schemas.run_model import ExecutionMode


class LocalScriptAgentAdapter:
    """Executes a local scripted tool loop against a disposable workspace."""

    PROVIDER_NAME = "Local Scripted Loop"
    MODEL_IDENTIFIER = "local-script-v1"
    EXECUTION_KIND = "simulated"
    EMPIRICAL_ELIGIBLE = False

    def __init__(self, workspace_dir: Path, mode: ExecutionMode, telemetry: TelemetryTracker, task_def: Dict[str, Any]):
        self.workspace_dir = workspace_dir.resolve()
        self.mode = mode
        self.telemetry = telemetry
        self.task_def = task_def
        self.task_id = task_def["task_id"]
        self.prompt = task_def["prompt"]
        self.process_id = os.getpid()
        self.session_id = f"sess-{uuid.uuid4().hex[:12]}"
        self.harness_request_id = f"req-{uuid.uuid4().hex[:12]}"

    def run(self) -> Dict[str, Any]:
        """Execute the scripted tool loop."""
        start_time = time.time()

        # Step 1: Enforce Mode Isolation
        if self.mode == ExecutionMode.CONTROL:
            os.environ["BRAIN_DISABLED"] = "1"
            os.environ["BRAIN_CAPABILITIES_DENIED"] = "1"
        else:
            os.environ.pop("BRAIN_DISABLED", None)
            os.environ["BRAIN_CAPABILITIES_ENABLED"] = "1"

        transcript = []
        transcript.append({"role": "system", "content": "You are a local scripted test agent."})
        transcript.append({"role": "user", "content": self.prompt})

        # Step 2: Query Brain in Treatment Mode if requested
        if self.mode == ExecutionMode.TREATMENT:
            try:
                from brain.insights.drift_rules import get_default_rule_registry
                registry = get_default_rule_registry()
                active_rules = [r.rule_id for r in registry.list_active_rules()]

                ctx_info = {
                    "task_id": self.task_id,
                    "target_symbols": ["normalize_repo_path", "build_repo_qualified_id", "build_generation_qualified_id"],
                    "active_rules": active_rules,
                    "context_pack_type": "code_symbols_and_drift_rules",
                }
                pack_content = json.dumps(ctx_info, indent=2)
                pack_bytes = len(pack_content.encode("utf-8"))
                pack_tokens = pack_bytes // 4

                self.telemetry.record_brain_call(
                    endpoint_or_tool="prepare_task_context",
                    query=self.prompt,
                    result_ids=["brain.graph.identity", "brain.insights.drift_rules"],
                    bytes_returned=pack_bytes,
                    tokens_returned=pack_tokens,
                    latency_ms=14.2,
                    used_by_agent=True,
                    context_type="code_symbols",
                )
                transcript.append({"role": "system", "content": f"Project Brain Context Evidence:\n{pack_content}"})
                transcript.append({"role": "assistant", "content": f"Analyzed Project Brain context pack ({pack_tokens} tokens)."})
            except Exception:
                self.telemetry.record_brain_call(
                    endpoint_or_tool="prepare_task_context",
                    query=self.prompt,
                    result_ids=[],
                    bytes_returned=0,
                    tokens_returned=0,
                    latency_ms=5.0,
                    used_by_agent=False,
                    context_type="error",
                )

        # Step 3: Tool Loop
        target_file = self._resolve_target_file()
        if target_file and target_file.exists():
            rel_file = target_file.relative_to(self.workspace_dir).as_posix()
            content = target_file.read_text(encoding="utf-8", errors="ignore")

            self.telemetry.record_tool_call("view_file", {"AbsolutePath": str(target_file)})
            transcript.append({"role": "assistant", "tool_call": "view_file", "args": {"file": rel_file}})
            transcript.append({"role": "tool_result", "content": f"Read {len(content.splitlines())} lines from {rel_file}"})

            modified = self._apply_code_change(target_file, content)
            if modified:
                self.telemetry.record_tool_call("replace_file_content", {"TargetFile": str(target_file)})
                transcript.append({"role": "assistant", "tool_call": "replace_file_content", "args": {"file": rel_file}})
                transcript.append({"role": "tool_result", "content": f"Successfully updated {rel_file}"})

        # Step 4: Verification Command
        test_cmd = "py -3 -m pytest tests/test_graph_v3_identity.py -v"
        try:
            res = subprocess.run(
                test_cmd,
                shell=True,
                cwd=str(self.workspace_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.telemetry.record_tool_call("run_command", {"CommandLine": test_cmd})
            transcript.append({"role": "assistant", "tool_call": "run_command", "args": {"command": test_cmd}})
            transcript.append({"role": "tool_result", "exit_code": res.returncode, "stdout": res.stdout[:500]})
        except Exception as exc:
            transcript.append({"role": "tool_result", "exit_code": -1, "error": str(exc)})

        # Step 5: Token Accounting
        prompt_chars = sum(len(str(m.get("content", ""))) for m in transcript)
        response_chars = sum(len(str(m.get("tool_call", ""))) + len(str(m.get("args", ""))) for m in transcript)

        actual_input_tokens = prompt_chars // 4
        actual_output_tokens = response_chars // 4
        self.telemetry.add_tokens(actual_input_tokens, actual_output_tokens)

        duration = round(time.time() - start_time, 3)

        adapter_execution_metadata = {
            "harness_adapter_name": "LocalScriptAgentAdapter (Local Scripted Loop)",
            "execution_kind": "simulated",
            "empirical_eligible": False,
            "external_llm_api_invoked": False,
            "provider_name": self.PROVIDER_NAME,
            "model_identifier": self.MODEL_IDENTIFIER,
            "harness_request_id": self.harness_request_id,
            "process_id": self.process_id,
            "session_id": self.session_id,
            "mode": self.mode.value,
            "token_usage_status": "estimated_from_transcript_chars",
            "prompt_chars": prompt_chars,
            "response_chars": response_chars,
            "estimated_input_tokens": actual_input_tokens,
            "estimated_output_tokens": actual_output_tokens,
            "wall_clock_ms": round(duration * 1000, 2),
        }

        return {
            "transcript": transcript,
            "adapter_execution_metadata": adapter_execution_metadata,
            "duration_s": duration,
        }

    def _resolve_target_file(self) -> Optional[Path]:
        if self.task_id == "task_00_calibration":
            return self.workspace_dir / "brain" / "graph" / "identity.py"
        elif self.task_id == "task_01_bug_localization":
            return self.workspace_dir / "brain" / "context" / "budget.py"
        elif self.task_id == "task_02_multifile_change":
            return self.workspace_dir / "brain" / "config" / "settings.py"
        elif self.task_id == "task_03_arch_boundary":
            return self.workspace_dir / "apps" / "api" / "routers" / "core.py"
        elif self.task_id == "task_04_cross_repo_contract":
            return self.workspace_dir / "brain" / "portfolio" / "models.py"
        elif self.task_id == "task_05_regression_remediation":
            return self.workspace_dir / "brain" / "insights" / "remediation_strategies.py"
        return None

    def _apply_code_change(self, target_file: Path, content: str) -> bool:
        if self.task_id == "task_00_calibration":
            if '"""' in content and "# Calibrated" not in content:
                new_content = content.replace(
                    '"""Graphify v2 Qualified Identity Generators."""',
                    '"""Graphify v2 Qualified Identity Generators.\n# Calibrated scripted agent run.\n"""'
                )
                target_file.write_text(new_content, encoding="utf-8")
                return True
        return False
