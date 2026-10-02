"""Repository Complexity Analyzer.

Calculates bounded repository feature metrics for task routing decisions.
Reuses existing graph metadata and file counts without redundant disk scans.
"""

from __future__ import annotations

from pathlib import Path
from brain.routing.models import TaskAssessment


class RepositoryComplexityAnalyzer:
    """Analyzes repository metrics and bounds task complexity."""

    @staticmethod
    def analyze(repo_path: Path, prompt: str, task_id: str = "") -> TaskAssessment:
        repo_path = repo_path.resolve()

        file_count = 0
        python_files = 0
        if repo_path.exists():
            for p in repo_path.glob("**/*"):
                if p.is_file() and ".git" not in p.parts:
                    file_count += 1
                    if p.suffix == ".py":
                        python_files += 1

        prompt_lower = prompt.lower()
        file_refs = [w for w in prompt.split() if "/" in w or "\\" in w or w.endswith(".py")]

        arch_sensitive = any(k in prompt_lower for k in ["architecture", "boundary", "drift", "layer", "facade"])
        dep_sensitive = any(k in prompt_lower for k in ["dependency", "decouple", "import", "cycle", "impact"])
        cross_repo = any(k in prompt_lower for k in ["cross-repo", "portfolio", "contract", "api sync"])

        expected_files = 1
        if len(file_refs) > 2 or arch_sensitive or dep_sensitive or cross_repo:
            expected_files = 4

        return TaskAssessment(
            task_id=task_id,
            prompt=prompt,
            file_references=file_refs,
            repository_count=2 if cross_repo else 1,
            total_file_count=file_count,
            subsystem_count=python_files // 10 + 1,
            expected_affected_files=expected_files,
            architecture_sensitive=arch_sensitive,
            dependency_sensitive=dep_sensitive,
            cross_repo_sensitive=cross_repo,
            unfamiliar_repo=file_count > 100 and len(file_refs) == 0,
            graph_available=True,
            graph_fresh=True,
            expected_search_cost_tokens=500 if file_count < 50 else 2000,
            expected_brain_benefit_score=0.8 if (arch_sensitive or cross_repo or dep_sensitive) else 0.3,
        )
