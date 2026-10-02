from brain.graph.graph_client import (
    GRAPH_SCHEMA_VERSION,
    EXCLUDED_HUB_SYMBOLS,
    GraphClient,
    build_qualified_symbol_identity,
)

def test_graph_schema_version_is_v3():
    assert GRAPH_SCHEMA_VERSION == 3

def test_excluded_hub_symbols_contains_builtins():
    for symbol in ["str", "Path", "get", "append", "list", "dict"]:
        assert symbol in EXCLUDED_HUB_SYMBOLS

def test_qualified_symbol_identity_prevents_bare_name_collision():
    # Identical bare symbol names in different files must generate distinct identities
    id_1 = build_qualified_symbol_identity(repository_id=1, file_path="apps/api/auth.py", qualified_name="get", symbol_kind="function")
    id_2 = build_qualified_symbol_identity(repository_id=1, file_path="brain/search/filters.py", qualified_name="get", symbol_kind="function")

    assert id_1 != id_2
    assert id_1 == "1:apps/api/auth.py:get:function"
    assert id_2 == "1:brain/search/filters.py:get:function"

def test_graph_client_constructs_repo_scoped_identity():
    client = GraphClient(repository_id=42)
    assert client.repository_id == 42
