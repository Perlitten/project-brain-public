"""Actionable Plan Schema & Deterministic Plan Validator for Project Brain v0.5.2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List
from brain.execution.models import ExecutionContract


@dataclass
class ActionablePlan:
    problem_statement: str
    primary_hypothesis: str
    confirmed_files: List[str]
    ordered_steps: List[Dict[str, str]]
    required_tests: List[str]
    arch_constraints: List[str] = field(default_factory=list)
    risk_level: str = "medium"
    unresolved_questions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "problem_statement": self.problem_statement,
            "primary_hypothesis": self.primary_hypothesis,
            "confirmed_files": self.confirmed_files,
            "ordered_steps": self.ordered_steps,
            "required_tests": self.required_tests,
            "arch_constraints": self.arch_constraints,
            "risk_level": self.risk_level,
            "unresolved_questions": self.unresolved_questions,
        }


class PlanValidator:
    """Deterministic validator for engineering plans before implementation."""

    @staticmethod
    def validate(plan: ActionablePlan, contract: ExecutionContract) -> List[str]:
        reasons = []

        if not plan.problem_statement or len(plan.problem_statement.strip()) < 10:
            reasons.append("Plan problem statement is missing or too vague (<10 chars).")

        if not plan.confirmed_files:
            reasons.append("Plan contains no confirmed target files.")
        elif len(plan.confirmed_files) > contract.max_changed_files:
            reasons.append(f"Plan targets {len(plan.confirmed_files)} files, exceeding contract limit of {contract.max_changed_files}.")

        if not plan.ordered_steps:
            reasons.append("Plan contains no ordered implementation steps.")

        for idx, step in enumerate(plan.ordered_steps, start=1):
            if "file" not in step or not step["file"]:
                reasons.append(f"Step {idx} is missing target 'file'.")
            if "expected_change" not in step or len(step["expected_change"].strip()) < 5:
                reasons.append(f"Step {idx} has missing or vague 'expected_change'.")

        if contract.required_arch_constraints:
            for constraint in contract.required_arch_constraints:
                if constraint not in plan.arch_constraints:
                    reasons.append(f"Plan omitted required architecture constraint: '{constraint}'.")

        # Verify no test weakening proposed
        plan_str = str(plan.to_dict()).lower()
        if "delete test" in plan_str or "remove assertion" in plan_str or "ignore failure" in plan_str:
            reasons.append("Plan proposes weakening or deleting tests, which is strictly prohibited.")

        return reasons
