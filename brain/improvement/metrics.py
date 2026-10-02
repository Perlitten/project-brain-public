"""Trajectory Metrics and Diagnostic Score Calculator for Project Brain v0.7.0."""

from typing import List


def calculate_diagnostic_score(
    functional_correctness: float,  # 40%
    spec_coverage: float,           # 15%
    arch_compliance: float,         # 10%
    required_file_coverage: float,  # 10%
    test_plan_quality: float,       # 10%
    patch_minimality: float,        # 5%
    trajectory_discipline: float,   # 5%
    cost_efficiency: float,         # 5%
) -> float:
    """Calculates composite diagnostic score (0-100 pts) to explain trajectory outcome."""
    score = (
        0.40 * functional_correctness +
        0.15 * spec_coverage +
        0.10 * arch_compliance +
        0.10 * required_file_coverage +
        0.10 * test_plan_quality +
        0.05 * patch_minimality +
        0.05 * trajectory_discipline +
        0.05 * cost_efficiency
    )
    return round(score, 2)


def is_empty_or_noop_patch(patch_diff: str) -> bool:
    """Detects empty or non-functional no-op patches."""
    if not patch_diff or patch_diff.strip() == "":
        return True

    meaningful_lines = []
    for raw_line in patch_diff.splitlines():
        line = raw_line.strip()
        if line.startswith("+") or line.startswith("-"):
            content = line[1:].strip()
            if content and not content.startswith("#") and not content.startswith("//"):
                meaningful_lines.append(content)

    return len(meaningful_lines) == 0


def calculate_plan_recall(planned_files: List[str], required_files: List[str]) -> float:
    """Calculates plan recall ratio: (planned_files ∩ required_files) / required_files."""
    if not required_files:
        return 1.0
    planned_set = set(planned_files)
    required_set = set(required_files)
    matched = planned_set.intersection(required_set)
    return len(matched) / len(required_set)
