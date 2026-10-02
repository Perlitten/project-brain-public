"""Unit Tests for Selective Execution Policy in Project Brain v0.5.3."""

from fastapi.testclient import TestClient
from brain.routing.models import (
    ExecutionRoute,
    RoutingContext,
)
from brain.routing.policy_engine import SelectivePolicyEngine
from brain.routing.cli import main as cli_main
from apps.api.main import app

client = TestClient(app)


def test_policy_engine_routing():
    engine = SelectivePolicyEngine()

    # Trivial fast path
    ctx_trivial = RoutingContext(task_category="trivial_localized", file_count=1, task_complexity="trivial")
    res_trivial = engine.evaluate(ctx_trivial)
    assert res_trivial.selected_route == ExecutionRoute.LEGACY_NO_BRAIN

    # Architecture boundary
    ctx_arch = RoutingContext(task_category="architecture_boundary", architecture_sensitivity=True)
    res_arch = engine.evaluate(ctx_arch)
    assert res_arch.selected_route == ExecutionRoute.PHASED_BRAIN_SELECTIVE
    assert res_arch.brain_route == "architecture_context"

    # Insufficient evidence fail-closed
    ctx_stale = RoutingContext(task_category="insufficient_evidence", evidence_sufficiency="insufficient")
    res_stale = engine.evaluate(ctx_stale)
    assert res_stale.selected_route == ExecutionRoute.ABSTAIN_STALE

    # Destructive human review
    ctx_destr = RoutingContext(task_category="migration", migration_requirement=True)
    res_destr = engine.evaluate(ctx_destr)
    assert res_destr.selected_route == ExecutionRoute.HUMAN_REVIEW_REQUIRED


def test_routing_api_endpoints():
    policy_resp = client.get("/routing/policy")
    assert policy_resp.status_code in (200, 401, 403, 422)

    summary_resp = client.get("/routing/summary")
    assert summary_resp.status_code in (200, 401, 403, 422)


def test_cli_import():
    assert callable(cli_main)
