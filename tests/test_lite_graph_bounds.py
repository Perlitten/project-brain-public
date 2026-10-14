"""High-degree traversal has a work bound and cancellation stops the worker."""
import asyncio
import json
import threading
from unittest.mock import Mock

import pytest

from brain.graph.sqlite_graph import SQLiteGraph


def populate(graph, count):
    with graph.connection() as db:
        rows = [(1, "File", "hub", json.dumps({"name": "hub", "path": "src/hub.py"}))]
        rows += [(1, "File", f"leaf-{i}", json.dumps({"name": f"src/leaf_{i}.py"})) for i in range(count)]
        db.executemany("INSERT INTO nodes(repo,label,identity,props) VALUES (?,?,?,?)", rows)
        seed = db.execute("SELECT id FROM nodes WHERE identity='hub'").fetchone()[0]
        leaves = db.execute("SELECT id FROM nodes WHERE identity!='hub'").fetchall()
        db.executemany("INSERT INTO edges(source,target,type,props) VALUES (?,?,?,?)",
                       [(seed, leaf[0], "USES", "{}") for leaf in leaves])


@pytest.mark.asyncio
async def test_high_degree_hub_never_materializes_its_full_neighborhood(tmp_path, monkeypatch):
    import brain.graph.sqlite_graph as module
    graph = SQLiteGraph(tmp_path)
    populate(graph, 2500)
    hydrated = Mock(wraps=module.GraphNode)
    monkeypatch.setattr(module, "GraphNode", hydrated)
    monkeypatch.setattr(graph, "_snapshot", Mock(side_effect=AssertionError("full graph snapshot")))
    rows = await graph.bounded_impact(1, ["hub"])
    assert len(rows) == 8
    assert hydrated.call_count <= 129  # One seed and the capped 128-edge projection.
    assert all(row["distance"] == 1 for row in rows)


@pytest.mark.asyncio
async def test_traversal_preserves_scope_two_hops_and_small_result_limits(tmp_path):
    graph = SQLiteGraph(tmp_path)
    populate(graph, 3)
    with graph.connection() as db:
        db.execute("INSERT INTO nodes(repo,label,identity,props) VALUES (2,'File','other',?)", (json.dumps({"name": "other/hub.py"}),))
        other = db.execute("SELECT id FROM nodes WHERE repo=2").fetchone()[0]
        first = db.execute("SELECT min(id) FROM nodes WHERE repo=1 AND identity!='hub'").fetchone()[0]
        db.execute("INSERT INTO edges VALUES (?,?,'USES','{}')", (first, other))
    assert await graph.bounded_impact(1, ["hub"], limit=0) == []
    rows = await graph.bounded_impact(1, ["hub"], limit=2)
    assert len(rows) == 2
    assert all(row["m"]["name"] != "other/hub.py" for row in rows)


@pytest.mark.asyncio
async def test_cancelling_async_traversal_signals_the_worker_to_stop(tmp_path, monkeypatch):
    graph = SQLiteGraph(tmp_path)
    started, stopped = threading.Event(), threading.Event()

    def worker(repo, keywords, limit, stop, deadline):
        started.set()
        assert stop.wait(1), "cancelled request left its worker running"
        stopped.set()
        return []

    monkeypatch.setattr(graph, "_bounded_impact", worker)
    request = asyncio.create_task(graph.bounded_impact(1, ["hub"]))
    assert await asyncio.to_thread(started.wait, 1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert await asyncio.to_thread(stopped.wait, 1)


@pytest.mark.asyncio
async def test_expired_sqlite_work_budget_is_a_typed_timeout(tmp_path, monkeypatch):
    from brain.config.settings import settings
    graph = SQLiteGraph(tmp_path)
    monkeypatch.setattr(settings, "AGENT_IMPACT_DEADLINE_S", 0)
    with pytest.raises(TimeoutError):
        await graph.bounded_impact(1, ["hub"])


@pytest.mark.asyncio
async def test_high_outgoing_degree_cannot_starve_incoming_file_edges(tmp_path):
    graph = SQLiteGraph(tmp_path)
    populate(graph, 2500)
    with graph.connection() as db:
        db.execute("UPDATE nodes SET label='Function' WHERE identity!='hub'")
        db.execute("INSERT INTO nodes(repo,label,identity,props) VALUES (1,'File','caller',?)",
                   (json.dumps({"name": "src/caller.py"}),))
        caller = db.execute("SELECT id FROM nodes WHERE identity='caller'").fetchone()[0]
        hub = db.execute("SELECT id FROM nodes WHERE identity='hub'").fetchone()[0]
        db.execute("INSERT INTO edges VALUES (?,?,'USES','{}')", (caller, hub))
    rows = await graph.bounded_impact(1, ["hub"])
    assert len(rows) == 8
    assert rows[0]["m"]["name"] == "src/caller.py"
    assert rows[0]["distance"] == 1
