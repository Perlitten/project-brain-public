"""Tests for hybrid retrieval pipeline routing and fusion."""


from brain.retrieval.pipeline import classify_query_route, _pin_qualified_path_hints, _select_final_paths
from brain.retrieval.types import QueryRoute, ChannelCandidate
from brain.search.path_hints import path_hint_bonus


def test_qualified_hint_is_pinned_without_promoting_basename_or_directory_matches():
    candidates = [ChannelCandidate("hints", "services/other/widget.py", 1.0, rank=1),
                  ChannelCandidate("hints", "services/worker/widget.py", 0.2, rank=3),
                  ChannelCandidate("hints", "services/worker/noise.py", 0.8, rank=2)]
    pinned = _pin_qualified_path_hints(candidates,
                                      ["services/worker/widget", "widget.py", "services/worker/"],
                                      ["tests/test_widget.py"], 2)
    assert pinned == ["services/worker/widget.py", "tests/test_widget.py"]


def test_qualified_hint_survives_keyword_promotion_inside_small_budget():
    anchor = "services/worker/widget.py"
    candidates = [ChannelCandidate("vector", "services/noise.py", 1.0, reranker_score=1.0),
                  ChannelCandidate("vector", "services/another.py", 0.9, reranker_score=0.9),
                  ChannelCandidate("hints", anchor, 0.1, reranker_score=0.1)]
    pinned = _pin_qualified_path_hints([candidates[2]], ["services/worker/widget"], [], 2)
    selected = _select_final_paths(reranked=candidates, v5_top=[c.item_id for c in candidates[:2]],
                                   file_limit=2, precision_k=2, candidates_by_channel={},
                                   keywords_lower=[], task_type="other", task_description="inspect worker",
                                   pinned=pinned)
    assert anchor in selected and len(selected) == 2 and len(set(selected)) == 2


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
