import json
import yaml
import pytest
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from brain.graph.schema import NodeType, RelationshipType
from brain.graph.graph_client import (
    FOCUS_DEFAULT_DEPTH,
    FOCUS_DEFAULT_MAX_NODES,
    FOCUS_MAX_DEPTH,
    FOCUS_MAX_NODES,
    GraphClient,
)
from brain.memory.decision_store import DecisionStore
from brain.memory.rule_store import RuleStore
from brain.integrations.obsidian import ObsidianIntegration
from brain.integrations.grafify import GrafifyIntegration, resolve_grafify_output_path


@pytest.mark.asyncio
async def test_graph_client_methods():
    # Mock Neo4j driver
    mock_driver = MagicMock()
    mock_session = AsyncMock()
    mock_driver.session.return_value.__aenter__.return_value = mock_session

    with patch("brain.graph.graph_client.neo4j_driver", mock_driver):
        client = GraphClient(repository_id=1)

        # Test create_node
        await client.create_node(NodeType.FILE.value, "test_file.py", {"size": 100})
        assert mock_session.run.called

        # Test create_relationship
        await client.create_relationship(
            NodeType.FILE.value, "a.py", NodeType.FILE.value, "b.py", RelationshipType.IMPORTS.value, {"line": 2}
        )
        assert mock_session.run.call_count == 2

        # Test get_neighbors
        mock_result = AsyncMock()

        class MockNeo4jObject(dict):
            def __init__(self, labels, props):
                super().__init__(props)
                self.labels = labels
                self.type = props.get("type", "IMPORTS")

        async def mock_async_iterator():
            yield {
                "n": MockNeo4jObject({"File"}, {"name": "a.py"}),
                "r": MockNeo4jObject(set(), {"line": 2, "type": "IMPORTS"}),
                "m": MockNeo4jObject({"File"}, {"name": "b.py"}),
            }

        mock_session.run.return_value = mock_result
        mock_result.__aiter__ = lambda self: mock_async_iterator()

        neighbors = await client.get_neighbors("a.py")
        assert len(neighbors) == 1
        assert neighbors[0]["source"]["properties"]["name"] == "a.py"
        assert neighbors[0]["relationship"]["type"] == "IMPORTS"
        assert neighbors[0]["target"]["properties"]["name"] == "b.py"

        # Test clear_graph
        await client.clear_graph()
        assert mock_session.run.called


# --------------------------------------------------------- focus traversal --
# A stand-in for a Neo4j session over an in-memory edge list. It honours the
# three things get_focus_neighborhood leans on — direction, $exclude and
# $limit — so a bound that only existed in the Cypher string would still fail
# these tests, and it records every query so the bounds can be asserted on the
# parameters the client actually sent.


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        async def _gen():
            for row in self._rows:
                yield row

        return _gen()

    async def single(self):
        return self._rows[0] if self._rows else None


class _FakeGraph:
    def __init__(self, nodes, edges):
        self.nodes = nodes
        self.edges = edges
        self.queries = []
        self._degree = {node_id: 0 for node_id in nodes}
        for src, dst, _ in edges:
            self._degree[src] = self._degree.get(src, 0) + 1
            self._degree[dst] = self._degree.get(dst, 0) + 1

    def row(self, node_id):
        node = self.nodes[node_id]
        return {
            "id": node_id,
            "name": node.get("name"),
            "label": node.get("label"),
            "kind": node.get("kind"),
            "path": node.get("path"),
            "degree": self._degree.get(node_id, 0),
        }


class _FakeGraphSession:
    def __init__(self, graph):
        self.graph = graph

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **params):
        self.graph.queries.append((query, params))
        if "n.name = $key" in query:
            rows = [
                self.graph.row(node_id)
                for node_id, node in self.graph.nodes.items()
                if params["key"] in (node.get("name"), node.get("path"))
            ]
            rows.sort(key=lambda r: r["degree"], reverse=True)
            return _FakeResult(rows[: params["limit"]])
        if "count(DISTINCT" in query:
            node_id = params["id"]
            incoming = {src for src, dst, _ in self.graph.edges if dst == node_id}
            outgoing = {dst for src, dst, _ in self.graph.edges if src == node_id}
            return _FakeResult([{"incoming": len(incoming), "outgoing": len(outgoing)}])

        against_arrows = "<-[r]-" in query
        frontier, exclude = set(params["frontier"]), set(params["exclude"])
        rows = []
        for src, dst, rel in self.graph.edges:
            parent, child = (dst, src) if against_arrows else (src, dst)
            if parent not in frontier or child in exclude:
                continue
            row = self.graph.row(child)
            row["parent"] = parent
            row["rel"] = rel
            rows.append(row)
        if "ORDER BY degree DESC" in query:
            rows.sort(key=lambda r: r["degree"], reverse=True)
        return _FakeResult(rows[: params["limit"]])


class _FakeDriver:
    def __init__(self, graph):
        self.graph = graph

    def session(self):
        return _FakeGraphSession(self.graph)


def _hub_graph():
    """One hub, 100 dependents and 100 dependencies, each fanning out twice more."""
    nodes = {"hub": {"name": "hub.py", "label": "File", "path": "hub.py"}}
    edges = []
    for side, direction in (("dep", "in"), ("use", "out")):
        for i in range(100):
            first = f"{side}-{i}"
            nodes[first] = {"name": f"{side}{i}.py", "label": "File", "path": f"{side}{i}.py"}
            edges.append((first, "hub", "IMPORTS") if direction == "in" else ("hub", first, "CALLS"))
            for j in range(5):
                second = f"{first}-{j}"
                nodes[second] = {"name": f"{side}{i}_{j}.py", "label": "File", "path": f"{side}{i}_{j}.py"}
                edges.append(
                    (second, first, "IMPORTS") if direction == "in" else (first, second, "CALLS")
                )
    return _FakeGraph(nodes, edges)


@pytest.mark.asyncio
async def test_focus_neighborhood_node_with_no_neighbours():
    graph = _FakeGraph({"n1": {"name": "lonely.py", "label": "File", "path": "lonely.py"}}, [])
    with patch("brain.graph.graph_client.neo4j_driver", _FakeDriver(graph)):
        result = await GraphClient(repository_id=1).get_focus_neighborhood("lonely.py")

    assert result["error"] is None
    assert result["matches"] == 1
    assert result["focus"]["name"] == "lonely.py"
    assert result["focus"]["degree"] == 0
    # Nothing invented for an isolated node: no levels, no edges, no blast, and
    # both direction totals measured as a real zero.
    assert result["levels"] == {"in": {}, "out": {}}
    assert result["edges"] == []
    assert result["blast"] == []
    assert result["totals"] == {"incoming": 0, "outgoing": 0}
    assert result["truncated"] == {"in": False, "out": False}
    assert isinstance(result["elapsed_ms"], float)


@pytest.mark.asyncio
async def test_focus_neighborhood_unknown_key_returns_no_focus():
    graph = _FakeGraph({"n1": {"name": "lonely.py", "label": "File", "path": "lonely.py"}}, [])
    with patch("brain.graph.graph_client.neo4j_driver", _FakeDriver(graph)):
        result = await GraphClient(repository_id=1).get_focus_neighborhood("nothing-matches-this")

    assert result["focus"] is None
    assert result["matches"] == 0
    assert result["error"] is None
    # One anchor query and nothing else — an unresolved key never traverses.
    assert len(graph.queries) == 1


@pytest.mark.asyncio
async def test_focus_neighborhood_enforces_depth_and_node_bounds():
    graph = _hub_graph()
    with patch("brain.graph.graph_client.neo4j_driver", _FakeDriver(graph)):
        result = await GraphClient(repository_id=1).get_focus_neighborhood("hub.py", depth=9, max_nodes=5000)

    # Both bounds are clamped by the traversal, not trusted from the caller.
    assert result["bounds"] == {"depth": FOCUS_MAX_DEPTH, "max_nodes": FOCUS_MAX_NODES}

    per_side = {
        side: sum(len(rows) for rows in result["levels"][side].values()) for side in ("in", "out")
    }
    assert per_side["in"] <= FOCUS_MAX_NODES // 2
    assert per_side["out"] <= FOCUS_MAX_NODES - FOCUS_MAX_NODES // 2
    assert per_side["in"] + per_side["out"] <= FOCUS_MAX_NODES
    assert max(list(result["levels"]["in"]) + list(result["levels"]["out"])) <= FOCUS_MAX_DEPTH

    # The hub has 1200 reachable nodes, so both sides must report the stop.
    assert result["truncated"] == {"in": True, "out": True}
    assert result["totals"] == {"incoming": 100, "outgoing": 100}

    # Every expansion carried a LIMIT no larger than the side budget, and no
    # more than depth expansions ran per direction.
    expansions = [
        (q, p) for q, p in graph.queries if "$frontier" in q
    ]
    assert expansions, "the traversal must expand through a parameterised frontier"
    assert len(expansions) <= 2 * FOCUS_MAX_DEPTH
    for query, params in expansions:
        assert "LIMIT $limit" in query
        assert 0 < params["limit"] <= FOCUS_MAX_NODES // 2 + 1

    # One parent edge per discovered node: the walk is a tree, never a re-scan.
    assert len(result["edges"]) == per_side["in"] + per_side["out"]
    assert len(result["blast"]) == per_side["in"]
    assert all(node["depth"] >= 1 for node in result["blast"])


@pytest.mark.asyncio
async def test_focus_neighborhood_does_not_claim_truncation_it_did_not_hit():
    """A side with exactly as many neighbours as the budget is complete, not cut."""
    nodes = {"hub": {"name": "hub.py", "label": "File", "path": "hub.py"}}
    edges = []
    for i in range(5):
        nodes[f"d{i}"] = {"name": f"d{i}.py", "label": "File", "path": f"d{i}.py"}
        edges.append((f"d{i}", "hub", "IMPORTS"))
    graph = _FakeGraph(nodes, edges)
    with patch("brain.graph.graph_client.neo4j_driver", _FakeDriver(graph)):
        result = await GraphClient(repository_id=1).get_focus_neighborhood("hub.py", depth=1, max_nodes=10)

    assert len(result["levels"]["in"][1]) == 5
    assert result["truncated"] == {"in": False, "out": False}


@pytest.mark.asyncio
async def test_focus_neighborhood_defaults_to_one_hop():
    graph = _hub_graph()
    with patch("brain.graph.graph_client.neo4j_driver", _FakeDriver(graph)):
        result = await GraphClient(repository_id=1).get_focus_neighborhood("hub.py")

    assert result["bounds"] == {
        "depth": FOCUS_DEFAULT_DEPTH,
        "max_nodes": FOCUS_DEFAULT_MAX_NODES,
    }
    assert list(result["levels"]["in"]) == [1]
    assert list(result["levels"]["out"]) == [1]
    assert len(result["levels"]["in"][1]) == FOCUS_DEFAULT_MAX_NODES // 2
    # Direction is real: dependents arrive against the arrows, dependencies with
    # them, and each carries the relationship type that connected it.
    assert {e["to"] for e in result["edges"] if e["from"] != "hub"} == {"hub"}
    assert {n["name"] for n in result["levels"]["in"][1]} <= {f"dep{i}.py" for i in range(100)}
    assert {n["name"] for n in result["levels"]["out"][1]} <= {f"use{i}.py" for i in range(100)}


@pytest.mark.asyncio
async def test_decision_store_methods():
    mock_session = AsyncMock()
    # session.add is synchronous on AsyncSession; a bare AsyncMock would return
    # an unawaited coroutine and emit RuntimeWarning.
    mock_session.add = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    with patch("brain.memory.decision_store.async_session_factory", mock_session_factory):
        # Setup execute result
        mock_execute_result = MagicMock()
        mock_execute_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_execute_result

        # Test add_decision
        async def mock_refresh(obj):
            obj.id = 42

        mock_session.refresh = mock_refresh

        dec_id = await DecisionStore.add_decision(
            title="Use SQL", repo_path="/indexed/example/", description="Use SQL for everything", status="active"
        )
        assert dec_id == 42
        assert mock_session.add.called
        assert mock_session.commit.called
        added_decision = mock_session.add.call_args.args[0]
        assert added_decision.repo_path == "/indexed/example"

        # Test list_decisions
        mock_result = MagicMock()
        mock_scalar = MagicMock()
        mock_scalar.all.return_value = [MagicMock(id=1, title="Dec 1"), MagicMock(id=2, title="Dec 2")]
        mock_result.scalars.return_value = mock_scalar
        mock_session.execute.return_value = mock_result

        decs = await DecisionStore.list_decisions()
        assert len(decs) == 2
        assert decs[0].title == "Dec 1"

        # Test search_decisions
        search_res = await DecisionStore.search_decisions("SQL")
        assert len(search_res) == 2

        # Test deprecate_decision
        mock_exec_res = MagicMock()
        mock_exec_res.scalar_one_or_none.return_value = MagicMock(id=1, status="active")
        mock_session.execute.return_value = mock_exec_res

        success = await DecisionStore.deprecate_decision(1)
        assert success is True
        assert mock_session.commit.called


@pytest.mark.asyncio
async def test_decision_store_updates_existing_normalized_title():
    existing = MagicMock(
        id=7,
        title="Canonical decision",
        description="Old description",
        status="active",
        date=datetime.date(2026, 6, 1),
        reason=None,
        consequences=None,
        affected_features=None,
        affected_modules=None,
        affected_files=None,
        repo_path="/indexed/Eunoia",
    )
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = existing
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.execute.return_value = execute_result
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    with patch(
        "brain.memory.decision_store.async_session_factory",
        mock_session_factory,
    ):
        decision_id = await DecisionStore.add_decision(
            title="  Canonical decision  ",
            repo_path="/indexed/Eunoia/",
            description="Current description",
            status="superseded",
        )

    assert decision_id == 7
    assert existing.title == "Canonical decision"
    assert existing.description == "Current description"
    assert existing.status == "superseded"
    assert existing.repo_path == "/indexed/Eunoia"
    assert existing.date == datetime.date(2026, 6, 1)
    mock_session.add.assert_not_called()
    mock_session.commit.assert_awaited_once()
    mock_session.refresh.assert_awaited_once_with(existing)


@pytest.mark.asyncio
async def test_rule_store_methods(tmp_path):
    mock_session = AsyncMock()
    # session.add is synchronous on AsyncSession; a bare AsyncMock would return
    # an unawaited coroutine and emit RuntimeWarning.
    mock_session.add = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    with patch("brain.memory.rule_store.async_session_factory", mock_session_factory):
        # Setup execute result
        mock_execute_result = MagicMock()
        mock_execute_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_execute_result

        # Test add_rule
        rule_id = await RuleStore.add_rule(
            name="Test Rule",
            repo_path="/indexed/example/",
            description="A test rule",
            type="architecture",
        )
        assert rule_id == "test-rule"
        assert mock_session.add.called
        assert mock_session.add.call_args.args[0].repo_path == "/indexed/example"
        assert mock_session.commit.called

        # Test list_rules
        mock_result = MagicMock()
        mock_scalar = MagicMock()
        mock_scalar.all.return_value = [MagicMock(id="test-rule", name="Test Rule")]
        mock_result.scalars.return_value = mock_scalar
        mock_session.execute.return_value = mock_result

        rules = await RuleStore.list_rules()
        assert len(rules) == 1
        assert rules[0].id == "test-rule"

        active_rules = await RuleStore.list_active_rules("/indexed/example")
        assert len(active_rules) == 1
        active_query = str(mock_session.execute.call_args.args[0])
        assert "rules.repo_path IS NULL" in active_query

        # Test sync_rules_from_yaml
        yaml_content = {
            "rules": [{"name": "YAML Rule", "description": "From yaml", "type": "design", "severity": "high"}]
        }
        yaml_file = tmp_path / "project_rules.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f)

        mock_session.execute.return_value = mock_execute_result
        await RuleStore.sync_rules_from_yaml(str(yaml_file))
        assert mock_session.add.called
        assert mock_session.commit.called


@pytest.mark.asyncio
async def test_obsidian_integration(tmp_path):
    # Create temporary vault
    vault_dir = tmp_path / "obsidian_vault"
    vault_dir.mkdir()

    # Create decision MD file
    dec_file = vault_dir / "ADR-001.md"
    dec_file.write_text(
        """---
type: decision
title: Use Postgres for storage
status: active
date: 2026-06-25
reason: ACID compliance
consequences: reliable storage
affected_features: ["core"]
---
This is a decision details body.
""",
        encoding="utf-8",
    )

    # Create rule MD file
    rule_file = vault_dir / "Rule-001.md"
    rule_file.write_text(
        """---
type: rule
name: Use standard imports
description: Always import from brain
rule_type: architecture
severity: critical
status: active
applies_to:
  modules: ["brain"]
---
This is a rule details body.
""",
        encoding="utf-8",
    )

    integration = ObsidianIntegration(str(vault_dir))
    items = integration.scan_vault()
    assert len(items) == 2

    # Mock DecisionStore and RuleStore
    mock_add_decision = AsyncMock(return_value=123)
    mock_add_rule = AsyncMock(return_value="use-standard-imports")

    with (
        patch("brain.integrations.obsidian.DecisionStore.add_decision", mock_add_decision),
        patch("brain.integrations.obsidian.RuleStore.add_rule", mock_add_rule),
    ):
        results = await integration.import_to_stores()
        assert results["decisions"] == [123]
        assert results["rules"] == ["use-standard-imports"]

        mock_add_decision.assert_called_once_with(
            title="Use Postgres for storage",
            repo_path=None,
            description="This is a decision details body.",
            status="active",
            date=datetime.date(2026, 6, 25),
            reason="ACID compliance",
            consequences="reliable storage",
            affected_features=["core"],
            affected_modules=[],
            affected_files=[],
        )

        mock_add_rule.assert_called_once_with(
            name="Use standard imports",
            repo_path=None,
            description="Always import from brain",
            type="architecture",
            severity="critical",
            status="active",
            applies_to={"modules": ["brain"]},
            rule_id=None,
        )


@pytest.mark.asyncio
async def test_grafify_integration(tmp_path):
    mock_client = MagicMock()
    mock_client.create_node = AsyncMock()
    mock_client.create_relationship = AsyncMock()

    grafify_data = {
        "nodes": [
            {"id": "node_1", "name": "AuthService", "type": "Module", "path": "brain/auth.py"},
            {"id": "node_2", "name": "Database", "label": "DatabaseEntity", "description": "Stores user records"},
        ],
        "edges": [{"source": "node_1", "target": "node_2", "type": "USES", "weight": 5}],
    }

    json_file = tmp_path / "grafify_output.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(grafify_data, f)

    integration = GrafifyIntegration(client=mock_client)
    await integration.import_json_file(str(json_file))

    assert mock_client.create_node.call_count == 2
    mock_client.create_node.assert_any_call(
        node_type="Module", name="AuthService", properties={"path": "brain/auth.py"}
    )
    mock_client.create_node.assert_any_call(
        node_type="DatabaseEntity", name="Database", properties={"description": "Stores user records"}
    )

    mock_client.create_relationship.assert_called_once_with(
        from_node_type="Module",
        from_name="AuthService",
        to_node_type="DatabaseEntity",
        to_name="Database",
        rel_type="USES",
        properties={"weight": 5},
    )


def test_resolve_grafify_output_path_explicit(tmp_path):
    json_file = tmp_path / "custom-graph.json"
    json_file.write_text("{}", encoding="utf-8")
    assert resolve_grafify_output_path(str(json_file)) == json_file.resolve()


def test_resolve_grafify_output_path_env(monkeypatch, tmp_path):
    json_file = tmp_path / "graph.json"
    json_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GRAFIFY_OUTPUT_PATH", str(json_file))
    from brain.config.settings import Settings

    monkeypatch.setattr("brain.integrations.grafify.settings", Settings())
    assert resolve_grafify_output_path() == json_file.resolve()
