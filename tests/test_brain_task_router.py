"""Tests for Brain Task Utility Router."""

from pathlib import Path
from brain.routing.models import RoutingModeEnum, TaskAssessment, TaskRouteEnum
from brain.routing.router import BrainTaskRouter


def test_task_assessment_defaults():
    ta = TaskAssessment(prompt="Simple prompt")
    assert ta.prompt == "Simple prompt"
    assert ta.graph_fresh is True
    assert ta.to_dict()["graph_fresh"] is True


def test_router_trivial_single_file(tmp_path: Path):
    (tmp_path / "foo.py").write_text("print(1)")
    router = BrainTaskRouter(mode=RoutingModeEnum.OBSERVE)
    decision = router.route_task(tmp_path, prompt="Fix docstring in foo.py")
    assert decision.route == TaskRouteEnum.NO_BRAIN
    assert decision.expected_brain_calls == 0


def test_router_architecture_sensitive(tmp_path: Path):
    router = BrainTaskRouter()
    decision = router.route_task(tmp_path, prompt="Refactor architecture boundary between API layer and DB facade")
    assert decision.route == TaskRouteEnum.ARCHITECTURE_CONTEXT
    assert decision.expected_brain_calls == 1


def test_router_cross_repo_sensitive(tmp_path: Path):
    router = BrainTaskRouter()
    decision = router.route_task(tmp_path, prompt="Synchronize portfolio API contract across repositories")
    assert decision.route == TaskRouteEnum.CROSS_REPO_IMPACT
    assert decision.max_evidence_tokens == 3200


def test_router_stale_graph():
    assessment = TaskAssessment(prompt="Complex task", graph_fresh=False)
    router = BrainTaskRouter()
    decision = router.route(assessment)
    assert decision.route == TaskRouteEnum.ABSTAIN_STALE
    assert decision.expected_brain_calls == 0
