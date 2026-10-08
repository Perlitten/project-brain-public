"""Tests for hybrid retrieval pipeline routing and fusion."""


from brain.retrieval.pipeline import classify_query_route
from brain.retrieval.types import QueryRoute
from brain.search.path_hints import path_hint_bonus


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


def test_concrete_path_hint_beats_filename_and_directory_hints():
    path = "services/payments/entry.py"
    concrete = path_hint_bonus(path, ["services/payments/entry"])
    filename = path_hint_bonus(path, ["entry.py"])
    directory = path_hint_bonus(path, ["services/payments/"])
    assert concrete > filename > directory > 0
    assert path_hint_bonus("services/payments/entry_backup.py", ["services/payments/entry"]) < concrete
    assert path_hint_bonus("services/payments/models.py", ["services/orders/models"]) == 0


def test_path_hint_handles_windows_case_and_does_not_stack_duplicate_hints():
    direct = path_hint_bonus("Services\\Payments\\Entry.py", ["services/payments/entry.py"])
    assert direct == path_hint_bonus("services/payments/entry.py",
                                     ["services", "entry.py", "services/payments/entry.py"])
