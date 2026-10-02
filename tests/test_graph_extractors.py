"""Tests for graph edge extractors."""

from brain.retrieval.graph_extractors import (
    extract_affects,
    extract_calls,
    extract_tab_core_links,
    extract_tested_by,
    extract_uses,
    retrieval_tab_core_boost,
)


def test_extract_calls_finds_invocations():
    content = "def run():\n    result = calculate_score()\n    return result\n"
    edges = extract_calls(content, "game/engine.py")
    names = {e.to_ref for e in edges}
    assert "calculate_score" in names
    assert all(e.provenance for e in edges)


def test_extract_tested_by_stem_match():
    paths = ["src/foo.py", "tests/test_foo.py", "other.py"]
    edges = extract_tested_by("src/foo.py", paths)
    assert any(e.to_ref == "tests/test_foo.py" for e in edges)


def test_extract_tab_core_links_tab_to_core():
    paths = [
        "extension/src/options/core.ts",
        "extension/src/options/tabs/DecksTab.tsx",
    ]
    edges = extract_tab_core_links("extension/src/options/tabs/DecksTab.tsx", paths)
    assert any(e.to_ref == "extension/src/options/core.ts" for e in edges)


def test_retrieval_tab_core_boost():
    boost = retrieval_tab_core_boost(
        "extension/src/options/core.ts",
        ["extension/src/options/tabs/DecksTab.tsx"],
        "Wire extension options tab to core module",
    )
    assert boost > 0.2


def test_extract_affects_from_feature_map():
    edges = extract_affects("apps/api/main.py", {"api": ["apps/api/*"]})
    assert len(edges) == 1
    assert edges[0].rel_type == "AFFECTS"
    assert edges[0].to_ref == "api"


def test_extract_uses_type_refs():
    content = "class Handler:\n    def go(self, req: RequestDTO) -> ResponseDTO:\n        pass\n"
    edges = extract_uses(content, "handlers.py", ".py")
    assert any(e.to_ref == "RequestDTO" for e in edges)
