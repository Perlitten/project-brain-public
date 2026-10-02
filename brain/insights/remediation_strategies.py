"""Remediation Strategy Registry for Deterministic Architectural Findings."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.insights.remediation_models import RemediationOption, RemediationPlan


class RemediationStrategyRegistry:
    """Generates structured remediation plans for common architectural findings."""

    @staticmethod
    def create_plan_for_finding(
        repo_name: str,
        source_rev: str,
        finding_dict: Dict[str, Any],
        repo_path: Path,
    ) -> RemediationPlan:

        rule_id = finding_dict.get("rule_id", "DRIFT-001")
        file_path = finding_dict.get("file_path") or finding_dict.get("source_path", "unknown")
        desc = finding_dict.get("description", "Architectural finding detected")

        file_hashes: Dict[str, str] = {}
        target_file = repo_path / file_path
        if target_file.exists():
            file_hashes[file_path] = hashlib.sha256(target_file.read_bytes()).hexdigest()

        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        plan_id = f"plan-{rule_id.lower()}-{hashlib.sha256((repo_name + file_path + rule_id).encode()).hexdigest()[:8]}"

        options: List[RemediationOption] = []
        impl_steps: List[str] = []
        val_steps: List[str] = []
        patch_diff: Optional[str] = None

        if rule_id in {"COUPLING-001", "DRIFT-001"}:
            # Forbidden Subsystem / Direct API DB bypass
            options.append(
                RemediationOption(
                    option_id="opt-1",
                    title="Introduce Interface or Service Facade",
                    description="Extract database operation into a public service method in the target subsystem",
                    architectural_improvement="high",
                    implementation_effort="medium",
                    blast_radius="low",
                    operational_risk="low",
                    pros=["Enforces clean encapsulation", "Decouples callers from database implementation"],
                    cons=["Requires adding new service method"],
                )
            )
            options.append(
                RemediationOption(
                    option_id="opt-2",
                    title="Apply Documented Migration Waiver",
                    description="Record an explicit temporary waiver with expiration date",
                    architectural_improvement="low",
                    implementation_effort="low",
                    blast_radius="none",
                    operational_risk="low",
                    pros=["Unblocks immediate release"],
                    cons=["Does not resolve underlying debt"],
                )
            )
            impl_steps = [
                f"Identify direct access pattern in '{file_path}'",
                "Define public service contract method",
                "Replace direct import with public facade invocation",
            ]
            val_steps = [
                "Run unit test suite for target component",
                "Run Project Brain change guard evaluation",
            ]
            patch_diff = (
                f"--- a/{file_path}\n"
                f"+++ b/{file_path}\n"
                f"@@ -1 +1 @@\n"
                f"-# Direct forbidden access\n"
                f"+# Refactored via public service facade\n"
            )

        elif rule_id in {"COUPLING-004", "DRIFT-002"}:
            # Worker to UI coupling
            options.append(
                RemediationOption(
                    option_id="opt-1",
                    title="Decouple Asset Rendering to Static Pre-builder",
                    description="Remove UI import from worker thread and pass pre-rendered payload",
                    architectural_improvement="high",
                    implementation_effort="low",
                    blast_radius="low",
                    operational_risk="low",
                )
            )
            impl_steps = [
                f"Remove UI import from worker file '{file_path}'",
                "Pass plain data payload directly into worker task",
            ]
            val_steps = ["Run background worker regression tests"]

        else:
            # Generic finding plan
            options.append(
                RemediationOption(
                    option_id="opt-1",
                    title="Refactor Code to Comply with Policy",
                    description=desc,
                    architectural_improvement="high",
                    implementation_effort="medium",
                    blast_radius="medium",
                    operational_risk="low",
                )
            )
            impl_steps = [f"Refactor '{file_path}' to adhere to rule '{rule_id}'"]
            val_steps = ["Run pytest regression suite"]

        return RemediationPlan(
            plan_id=plan_id,
            schema_version=1,
            repository=repo_name,
            source_revision=source_rev,
            finding_ids=[rule_id],
            graph_generation="active",
            problem_statement=f"Finding '{rule_id}' in '{file_path}': {desc}",
            evidence={"finding": finding_dict},
            affected_files=[file_path],
            affected_symbols=[],
            impacted_subsystems=[],
            remediation_options=options,
            recommended_option_index=0,
            implementation_steps=impl_steps,
            validation_steps=val_steps,
            tests_to_run=["tests/test_architecture_change_guard.py"],
            rollback_plan=["Revert patch diff or restore prior git commit"],
            risks=["Minor regression risk during refactoring"],
            human_approvals_required=["@architecture-leads"],
            state="proposed",
            created_at_utc=now_str,
            updated_at_utc=now_str,
            patch_diff=patch_diff,
            file_hashes=file_hashes,
        )
