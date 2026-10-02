"""Conservative Test Selection Generator for Changed Entities."""

from __future__ import annotations

from pathlib import Path
from typing import List, Set

from brain.insights.impact_models import ImpactedEntity


class TestImpactSelector:
    """Recommends specific tests based on impacted source files and modules."""

    @staticmethod
    def select_tests(repo_path: Path, impacted_entities: List[ImpactedEntity]) -> List[str]:
        suggested: Set[str] = set()

        impacted_paths = {e.normalized_path for e in impacted_entities if e.normalized_path}

        # 1. Direct test file match
        for path_str in impacted_paths:
            if path_str.startswith("tests/test_") and path_str.endswith(".py"):
                suggested.add(path_str)

        # 2. Convention mapping: brain/insights/foo.py -> tests/test_foo.py or tests/test_insights.py
        for path_str in impacted_paths:
            file_name = Path(path_str).stem
            # Check if a matching test file exists
            test_candidates = [
                f"tests/test_{file_name}.py",
                f"tests/test_{path_str.replace('/', '_').removesuffix('.py')}.py",
            ]
            for cand in test_candidates:
                if (repo_path / cand).exists():
                    suggested.add(cand)

        # Fallback to general test suites if specific test was not found
        if not suggested:
            for p in (repo_path / "tests").glob("test_*.py"):
                rel = p.relative_to(repo_path).as_posix()
                suggested.add(rel)
                if len(suggested) >= 3:
                    break

        return sorted(list(suggested))
