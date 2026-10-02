import ast
import re
from pathlib import Path

import pytest

from brain.graph.graph_client import (
    GRAPH_SCHEMA_VERSION,
    GraphClient,
    ensure_graph_schema,
)
from brain.graph.schema import VALID_NODE_TYPES
from brain.graph.schema import NodeType, RelationshipType


def test_production_graphclient_calls_always_pass_repository_identity():
    """Regression gate for the production /context failure from 2026-07-28."""
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for source_root in ("brain", "apps"):
        for path in (root / source_root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called_name = getattr(node.func, "id", None)
                if called_name != "GraphClient":
                    continue
                has_positional_identity = bool(node.args)
                has_keyword_identity = any(keyword.arg == "repository_id" for keyword in node.keywords)
                if not (has_positional_identity or has_keyword_identity):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


class _EmptyResult:
    def __aiter__(self):
        async def iterator():
            if False:
                yield None

        return iterator()

    async def single(self):
        return None


class _StatefulSession:
    def __init__(self, state):
        self.state = state
        self.calls = []

    async def run(self, query, **params):
        self.calls.append((query, params))
        node_match = re.search(r"MERGE \(n:([A-Za-z]+)", query)
        relationship_match = re.search(
            r"MERGE \(a:([A-Za-z]+).*MERGE \(b:([A-Za-z]+).*MERGE \(a\)-\[r:([A-Z_]+)\]",
            query,
        )
        if relationship_match:
            from_label, to_label, rel_type = relationship_match.groups()
            from_key = (
                params["graph_schema_version"],
                params["repository_id"],
                params["from_identity"],
            )
            to_key = (
                params["graph_schema_version"],
                params["repository_id"],
                params["to_identity"],
            )
            self.state["nodes"][(from_label, *from_key)] = dict(params["from_props"])
            self.state["nodes"][(to_label, *to_key)] = dict(params["to_props"])
            self.state["relationships"].add(
                (params["repository_id"], from_label, from_key[-1], rel_type, to_label, to_key[-1])
            )
        elif node_match:
            label = node_match.group(1)
            key = (
                label,
                params["graph_schema_version"],
                params["repository_id"],
                params["identity"],
            )
            self.state["nodes"][key] = dict(params["props"])
        return _EmptyResult()


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StatefulDriver:
    def __init__(self):
        self.state = {"nodes": {}, "relationships": set()}
        self.session_instance = _StatefulSession(self.state)

    def session(self):
        return _SessionContext(self.session_instance)


@pytest.mark.asyncio
async def test_identical_files_and_symbols_remain_isolated_across_repositories():
    """Two repos with the same filename should not collide in the graph."""
    driver = _StatefulDriver()
    repo_one = GraphClient(repository_id=101, driver=driver)
    repo_two = GraphClient(repository_id=202, driver=driver)
    symbol_identity = "README.md::function::render::1"

    for client in (repo_one, repo_two):
        await client.create_node(
            NodeType.FILE.value,
            "README.md",
            {"path": "README.md"},
        )
        await client.create_node(
            NodeType.SYMBOL.value,
            "render",
            {"file_path": "README.md", "kind": "function"},
            identity=symbol_identity,
        )
        await client.create_relationship(
            NodeType.FILE.value,
            "README.md",
            NodeType.SYMBOL.value,
            "render",
            RelationshipType.CONTAINS.value,
            to_identity=symbol_identity,
            to_properties={"file_path": "README.md", "kind": "function"},
        )

    assert ("File", GRAPH_SCHEMA_VERSION, 101, "README.md") in driver.state["nodes"]
    assert ("File", GRAPH_SCHEMA_VERSION, 202, "README.md") in driver.state["nodes"]
    assert (
        "Symbol",
        GRAPH_SCHEMA_VERSION,
        101,
        symbol_identity,
    ) in driver.state["nodes"]
    assert (
        "Symbol",
        GRAPH_SCHEMA_VERSION,
        202,
        symbol_identity,
    ) in driver.state["nodes"]


@pytest.mark.asyncio
async def test_relationship_endpoints_require_one_repository_fence():
    """Relationships bind both endpoints to the same repository."""
    driver = _StatefulDriver()
    client = GraphClient(repository_id=7, driver=driver)

    await client.create_relationship(
        NodeType.FILE.value,
        "a.py",
        NodeType.FILE.value,
        "b.py",
        RelationshipType.CALLS.value,
        from_properties={"path": "a.py"},
        to_properties={"path": "b.py"},
    )

    # Check that both nodes have the same repository_id
    a_key = ("File", GRAPH_SCHEMA_VERSION, 7, "a.py")
    b_key = ("File", GRAPH_SCHEMA_VERSION, 7, "b.py")
    assert a_key in driver.state["nodes"]
    assert b_key in driver.state["nodes"]


@pytest.mark.asyncio
async def test_neighbor_reads_reject_unscoped_relationships():
    """get_neighbors should filter by repository_id."""
    driver = _StatefulDriver()
    client = GraphClient(repository_id=7, driver=driver)

    # Create a node and check that reads are scoped
    result = await client.get_neighbors("a.py")
    assert result == []

    # Verify the query includes repository scoping
    query, params = driver.session_instance.calls[-1]
    assert params["repository_id"] == 7
    assert "repository_id IS NULL" not in query
    assert query.count("repository_id = $repository_id") == 2


@pytest.mark.asyncio
async def test_all_focus_reads_reject_legacy_unscoped_nodes():
    driver = _StatefulDriver()
    client = GraphClient(repository_id=7, driver=driver)

    await client.get_focus_neighborhood("a.py")

    assert driver.session_instance.calls
    for query, params in driver.session_instance.calls:
        assert params["repository_id"] == 7
        assert "repository_id IS NULL" not in query


@pytest.mark.asyncio
async def test_repository_purge_is_label_scoped_and_indexable():
    driver = _StatefulDriver()
    client = GraphClient(repository_id=7, driver=driver)

    await client.purge_repository()

    calls = driver.session_instance.calls
    assert len(calls) == len(VALID_NODE_TYPES)
    assert all("MATCH (n:" in query for query, _ in calls)
    assert all(params["repository_id"] == 7 for _, params in calls)


@pytest.mark.asyncio
async def test_graph_schema_installs_repository_indexes_for_every_label():
    driver = _StatefulDriver()

    await ensure_graph_schema(driver)

    queries = [query for query, _ in driver.session_instance.calls]
    assert len(queries) == 2 * len(VALID_NODE_TYPES)
    assert all("CREATE INDEX" in query for query in queries)
    assert sum("ON (n.repository_id)" in query for query in queries) == len(VALID_NODE_TYPES)
    assert sum("n.graph_schema_version, n.repository_id, n.identity" in query for query in queries) == len(
        VALID_NODE_TYPES
    )


@pytest.mark.asyncio
async def test_symbol_nodes_require_file_scoped_stable_identity():
    """Symbol identity must include file path and location."""
    driver = _StatefulDriver()
    client = GraphClient(repository_id=1, driver=driver)

    # Valid identity format
    valid_identity = "service.py::function::process::42"
    await client.create_node(
        NodeType.SYMBOL.value,
        "process",
        {"file_path": "service.py", "kind": "function", "line": 42},
        identity=valid_identity,
    )

    key = ("Symbol", GRAPH_SCHEMA_VERSION, 1, valid_identity)
    assert key in driver.state["nodes"]


@pytest.mark.asyncio
async def test_graphclient_requires_repository_id_at_construction():
    """GraphClient must fail loudly if constructed without repository_id.

    This test ensures that the production failure where FileIndexer constructed
    GraphClient without repository_id (causing null-property merges) is caught
    at construction time, not at write time. The test would have failed with
    the old code that allowed Optional[int] = None, and now passes because
    we require repository_id as a mandatory int parameter.
    """
    driver = _StatefulDriver()

    # Should raise ValueError when repository_id is None
    with pytest.raises(ValueError, match="repository_id is required"):
        GraphClient(repository_id=None, driver=driver)

    # Should also raise when called without any repository_id argument
    with pytest.raises(TypeError):
        GraphClient(driver=driver)

    # Should succeed with a valid repository_id
    client = GraphClient(repository_id=42, driver=driver)
    assert client.repository_id == 42
