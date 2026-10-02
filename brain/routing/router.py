"""Deterministic Task Utility Router.

Maps TaskAssessment to TaskRouteEnum decisions using explicit deterministic rules.
Guarantees NO_BRAIN for trivial localized edits and routes complex architectural
or cross-repo tasks to specialized evidence capabilities.
"""

from __future__ import annotations

from pathlib import Path
from brain.routing.complexity import RepositoryComplexityAnalyzer
from brain.routing.models import RouteDecision, RoutingModeEnum, TaskAssessment, TaskRouteEnum


class BrainTaskRouter:
    """Authoritative task utility router."""

    def __init__(self, mode: RoutingModeEnum = RoutingModeEnum.OBSERVE):
        self.mode = mode

    def route(self, assessment: TaskAssessment) -> RouteDecision:
        # Check stale graph or unverified lineage first
        if not assessment.graph_fresh or not assessment.graph_available:
            return RouteDecision(
                route=TaskRouteEnum.ABSTAIN_STALE,
                confidence=0.95,
                reasons=["Graph metadata is stale or unavailable. Fallback to ordinary search."],
                expected_brain_calls=0,
                max_evidence_tokens=0,
            )

        # Rule 1: Trivial localized edit (explicit file named, simple scope, single file)
        if len(assessment.file_references) == 1 and assessment.expected_affected_files == 1 and not (assessment.architecture_sensitive or assessment.cross_repo_sensitive or assessment.dependency_sensitive):
            return RouteDecision(
                route=TaskRouteEnum.NO_BRAIN,
                confidence=0.90,
                reasons=["Trivial localized edit with explicit file reference. Brain overhead exceeds expected benefit."],
                expected_brain_calls=0,
                max_evidence_tokens=0,
            )

        # Rule 2: Cross-Repository Contract
        if assessment.cross_repo_sensitive:
            return RouteDecision(
                route=TaskRouteEnum.CROSS_REPO_IMPACT,
                confidence=0.92,
                reasons=["Cross-repository contract synchronization detected."],
                expected_brain_calls=1,
                max_evidence_tokens=3200,
            )

        # Rule 3: Architecture Sensitivity
        if assessment.architecture_sensitive:
            return RouteDecision(
                route=TaskRouteEnum.ARCHITECTURE_CONTEXT,
                confidence=0.88,
                reasons=["Architecture boundary or drift sensitivity detected."],
                expected_brain_calls=1,
                max_evidence_tokens=2400,
            )

        # Rule 4: Dependency Analysis
        if assessment.dependency_sensitive:
            return RouteDecision(
                route=TaskRouteEnum.IMPACT_ANALYSIS,
                confidence=0.85,
                reasons=["Dependency decoupling or cycle analysis detected."],
                expected_brain_calls=1,
                max_evidence_tokens=2000,
            )

        # Rule 5: Multi-file or Unfamiliar Repository
        if assessment.expected_affected_files > 2 or assessment.unfamiliar_repo:
            return RouteDecision(
                route=TaskRouteEnum.TARGETED_CONTEXT_PACK,
                confidence=0.82,
                reasons=["Multi-file change or unfamiliar repository structure."],
                expected_brain_calls=1,
                max_evidence_tokens=2000,
            )

        # Default Fallback: NO_BRAIN
        return RouteDecision(
            route=TaskRouteEnum.NO_BRAIN,
            confidence=0.75,
            reasons=["Default task assessment: Ordinary file inspection is sufficient."],
            expected_brain_calls=0,
            max_evidence_tokens=0,
        )

    def route_task(self, repo_path: Path, prompt: str, task_id: str = "") -> RouteDecision:
        assessment = RepositoryComplexityAnalyzer.analyze(repo_path, prompt, task_id)
        return self.route(assessment)
