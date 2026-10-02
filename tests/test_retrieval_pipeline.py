"""Tests for hybrid retrieval pipeline routing and fusion."""


from brain.retrieval.pipeline import classify_query_route
from brain.retrieval.types import QueryRoute


def test_route_exact_identifier_for_class_names():
    route = classify_query_route("bugfix", ["HealthCheckService"], "Fix HealthCheckService timeout")
    assert route == QueryRoute.EXACT_IDENTIFIER


def test_route_dependency_for_import_language():
    route = classify_query_route("refactor", ["import"], "Update dependency imports in api layer")
    assert route == QueryRoute.DEPENDENCY


def test_route_decision_rule():
    route = classify_query_route("other", ["rule"], "Ensure ADR decision policy is respected")
    assert route == QueryRoute.DECISION_RULE


def test_route_conceptual_for_broad_feature():
    route = classify_query_route(
        "feature",
        ["retrieval", "hybrid", "context", "pack"],
        "Improve hybrid context retrieval in context pack builder",
    )
    assert route == QueryRoute.CONCEPTUAL
