"""Structured Testing, Repair Loop, and Final Verification Gate for Project Brain v0.5.2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List
from brain.execution.models import ExecutionContract


@dataclass
class StructuredTestResult:
    command: str
    exit_code: int
    duration_seconds: float
    passed_count: int
    failed_count: int
    failing_test_ids: List[str] = field(default_factory=list)
    failure_category: str = "unknown"  # implementation_defect, incomplete_implementation, architecture_violation
    stdout_snippet: str = ""
    stderr_snippet: str = ""


@dataclass
class RepairHypothesis:
    observed_failure: str
    likely_cause: str
    target_file: str
    proposed_correction: str
    iteration: int


class BoundedRepairLoop:
    """Manages bounded repair iterations (max 3) and prevents repeated failure loops."""

    def __init__(self, max_repairs: int = 3):
        self.max_repairs = max_repairs
        self.repairs_attempted = 0
        self.seen_signatures: List[str] = []

    def can_repair(self, failure_signature: str) -> tuple[bool, str]:
        if self.repairs_attempted >= self.max_repairs:
            return False, f"Max repair iterations ({self.max_repairs}) reached."
        if failure_signature in self.seen_signatures:
            return False, f"Failure signature '{failure_signature}' already attempted. Stopping loop to prevent retrying same failed approach."
        return True, ""

    def record_attempt(self, failure_signature: str, hypothesis: RepairHypothesis):
        self.repairs_attempted += 1
        self.seen_signatures.append(failure_signature)


class VerificationGate:
    """Final verification gate checking functional pass, arch compliance, and patch scope."""

    @staticmethod
    def verify(
        test_results: List[StructuredTestResult],
        arch_findings: int,
        modified_files: List[str],
        contract: ExecutionContract,
    ) -> tuple[bool, List[str]]:
        violations = []

        for tr in test_results:
            if tr.exit_code != 0:
                violations.append(f"Test command failed: '{tr.command}' (exit code {tr.exit_code})")

        if arch_findings > 0:
            violations.append(f"Found {arch_findings} architecture boundary violations.")

        if len(modified_files) > contract.max_changed_files:
            violations.append(f"Modified {len(modified_files)} files, exceeding contract limit of {contract.max_changed_files}.")

        for f in modified_files:
            if f.startswith(".git") or ".git/" in f:
                violations.append(f"Illegal modification of .git directory file: {f}")

        return (len(violations) == 0), violations
