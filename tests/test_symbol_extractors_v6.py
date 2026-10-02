"""Tests for Retrieval v6 P1 structural symbol + graph-edge extraction."""

from brain.indexers.symbol_extractors import (
    extract_import_target_symbols,
    extract_structural_symbols,
    resolve_imports_to_paths,
)
from brain.retrieval.graph_extractors import (
    extract_import_file_edges,
    extract_route_edges,
    extract_script_domain_edges,
)
from brain.search.surfaces import IMPORT_TARGET_KIND, V6_SYMBOL_KINDS

ROUTER_SRC = '''
from fastapi import APIRouter
router = APIRouter(prefix="/api/ml", tags=["ml"])

@router.get("/status")
async def ml_status():
    return {}

@router.post("/simulate")
async def run_simulation(request):
    return {}
'''

CLI_SRC = '''
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-manifest")
    parser.add_argument("-o", "--output")

if __name__ == "__main__":
    main()
'''


def test_route_symbols_include_prefix():
    syms = extract_structural_symbols(ROUTER_SRC, ".py", "src/server/routers/ml.py")
    routes = [s for s in syms if s["kind"] == "api_route"]
    names = {s["name"] for s in routes}
    assert "/api/ml/status" in names
    assert "/api/ml/simulate" in names
    assert all(s["kind"] in V6_SYMBOL_KINDS for s in syms)


def test_cli_flags_and_entrypoint():
    syms = extract_structural_symbols(CLI_SRC, ".py", "scripts/build_snapshot.py")
    kinds = {s["kind"] for s in syms}
    names = {s["name"] for s in syms}
    assert "cli_flag" in kinds
    assert "snapshot-manifest" in names
    assert "script_entrypoint" in kinds
    assert "build_snapshot" in names  # entrypoint named by stem


def test_structural_symbols_empty_for_non_python():
    assert extract_structural_symbols("export const x = 1", ".ts", "a.ts") == []


def test_resolve_imports_to_paths():
    content = "from src.engine.ai_engine import AIEngine\nimport src.server.executors\n"
    all_paths = [
        "src/engine/ai_engine.py", "src/server/executors.py", "tests/test_x.py",
    ]
    targets = resolve_imports_to_paths(content, ".py", "tests/test_gameplay.py", all_paths)
    assert "src/engine/ai_engine.py" in targets
    assert "src/server/executors.py" in targets


def test_import_target_symbols_kind():
    content = "from src.engine.ai_engine import AIEngine\n"
    syms = extract_import_target_symbols(content, ".py", "tests/test_x.py", ["src/engine/ai_engine.py"])
    assert syms and all(s["kind"] == IMPORT_TARGET_KIND for s in syms)
    assert syms[0]["name"] == "src/engine/ai_engine.py"


def test_route_edges():
    edges = extract_route_edges(ROUTER_SRC, "src/server/routers/ml.py", ".py")
    refs = {e.to_ref for e in edges}
    assert "/api/ml/simulate" in refs
    assert all(e.rel_type == "ROUTE" for e in edges)


def test_test_source_import_edge():
    content = "from src.engine.ai_engine import AIEngine\n"
    edges = extract_import_file_edges(content, "tests/test_gameplay.py", ".py", ["src/engine/ai_engine.py"])
    tests_edges = [e for e in edges if e.rel_type == "TESTS"]
    assert tests_edges and tests_edges[0].to_ref == "src/engine/ai_engine.py"


def test_script_domain_edges_share_tokens():
    edges = extract_script_domain_edges(
        "scripts/sync_hermes_rag.py",
        ["scripts/build_hermes_rag_snapshot.py", "scripts/unrelated_tool.py"],
    )
    refs = {e.to_ref for e in edges}
    assert "scripts/build_hermes_rag_snapshot.py" in refs
    assert "scripts/unrelated_tool.py" not in refs
