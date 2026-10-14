"""Repository-scoped persisted adjacency for local indexing and impact reads."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import threading
from collections import deque
from pathlib import Path
from contextlib import contextmanager

from brain.graph.schema import VALID_NODE_TYPES, VALID_RELATIONSHIP_TYPES


class GraphNode(dict):
    def __init__(self, node_id: int, label: str, props: dict):
        super().__init__(props)
        self.element_id = str(node_id)
        self.labels = {label}


class SQLiteGraph:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "graph.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id INTEGER PRIMARY KEY, repo INTEGER NOT NULL, label TEXT NOT NULL,
                    identity TEXT NOT NULL, props TEXT NOT NULL, UNIQUE(repo,label,identity));
                CREATE TABLE IF NOT EXISTS edges (
                    source INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
                    target INTEGER REFERENCES nodes(id) ON DELETE CASCADE, type TEXT NOT NULL,
                    props TEXT NOT NULL, PRIMARY KEY(source,target,type));
                CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def _snapshot(self, repository_id):
        with self.connection() as db:
            nodes = {row[0]: GraphNode(row[0], row[1], json.loads(row[2])) for row in db.execute(
                "SELECT id,label,props FROM nodes WHERE repo=? ORDER BY id", (repository_id,))}
            edges = [row for row in db.execute(
                "SELECT source,target,type,props FROM edges WHERE source IN (SELECT id FROM nodes WHERE repo=?) ORDER BY source,target,type",
                (repository_id,)) if row[1] in nodes]
        return nodes, edges

    async def bounded_impact(self, repository_id: int, keywords: list[str], limit=480):
        from brain.config.settings import settings
        stop = threading.Event()
        deadline = time.monotonic() + max(0.0, settings.AGENT_IMPACT_DEADLINE_S * 0.8)
        try:
            return await asyncio.to_thread(self._bounded_impact, repository_id, keywords, limit, stop, deadline)
        finally:
            # Cancelling to_thread alone leaves its worker running. SQL progress
            # and traversal loops both observe this cooperative stop signal.
            stop.set()

    def _bounded_impact(self, repository_id, keywords, limit, stop, deadline):
        # Fetch adjacency on demand: do not copy the repository's full graph.
        # Each of at most 60 seeds visits <=64 nodes and reads <=128 edges.
        output = []
        if limit <= 0 or stop.is_set():
            return output
        db = sqlite3.connect(self.path, timeout=0.05)
        db.set_progress_handler(lambda: int(stop.is_set() or time.monotonic() >= deadline), 100)
        try:
            db.execute("PRAGMA query_only=ON")
            for kw in keywords[:12]:
                if stop.is_set():
                    return output
                if time.monotonic() >= deadline:
                    raise TimeoutError("Lite impact deadline exceeded")
                seeds = db.execute("""SELECT id,label,props FROM nodes WHERE repo=? AND
                    (instr(lower(COALESCE(json_extract(props,'$.name'),'')),?)>0 OR
                     instr(lower(COALESCE(json_extract(props,'$.path'),'')),?)>0)
                    ORDER BY CASE WHEN lower(json_extract(props,'$.name'))=? THEN 0
                                  WHEN label='File' THEN 1 ELSE 2 END,
                             json_extract(props,'$.name'),id LIMIT 5""", (repository_id,kw,kw,kw)).fetchall()
                for seed_id, label, props in seeds:
                    seed = GraphNode(seed_id, label, json.loads(props))
                    queue = deque([(seed_id, 0)])
                    seen = {seed_id}
                    nodes = {seed_id: seed}
                    neighbors = []
                    edges_left = 128
                    while queue:
                        if stop.is_set():
                            return output
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Lite impact deadline exceeded")
                        node_id, distance = queue.popleft()
                        if distance:
                            neighbors.append((node_id, distance))
                        if distance >= 2 or edges_left <= 0 or len(seen) >= 64:
                            continue
                        outgoing = db.execute("SELECT target FROM edges WHERE source=? LIMIT ?",
                                              (node_id, edges_left)).fetchall()
                        incoming = db.execute("SELECT source FROM edges WHERE target=? LIMIT ?",
                                              (node_id, edges_left - len(outgoing))).fetchall()
                        edges_left -= len(outgoing) + len(incoming)
                        adjacent = list(dict.fromkeys(row[0] for row in outgoing + incoming if row[0] not in seen))
                        if not adjacent:
                            continue
                        placeholders = ",".join("?" for _ in adjacent)
                        rows = db.execute(f"SELECT id,label,props FROM nodes WHERE repo=? AND id IN ({placeholders})",
                                          (repository_id, *adjacent)).fetchall()
                        hydrated = [GraphNode(row[0], row[1], json.loads(row[2])) for row in rows]
                        hydrated.sort(key=lambda node: (0 if "File" in node.labels else 1, str(node.get("name", "")), int(node.element_id)))
                        for neighbor in hydrated:
                            neighbor_id = int(neighbor.element_id)
                            if len(seen) >= 64:
                                break
                            seen.add(neighbor_id)
                            nodes[neighbor_id] = neighbor
                            queue.append((neighbor_id, distance + 1))
                    neighbors.sort(key=lambda item: (0 if "File" in nodes[item[0]].labels else 1,
                                                      item[1], str(nodes[item[0]].get("name", ""))))
                    for node_id, distance in neighbors[:8] or [(seed_id, 0)]:
                        if len(output) >= min(limit, 480):
                            return output
                        output.append({"kw":kw,"n":seed,"m":nodes[node_id] if distance else None,"distance":distance})
            return output
        except sqlite3.OperationalError as exc:
            if stop.is_set():
                return output
            if time.monotonic() >= deadline:
                raise TimeoutError("Lite impact deadline exceeded") from exc
            raise
        finally:
            db.close()

    async def verify_connectivity(self):
        with self.connection() as db:
            db.execute("SELECT 1")

    async def close(self):
        pass

    def session(self):
        raise NotImplementedError("Lite graph supports scoped adjacency, not arbitrary Cypher; use full mode for graph administration")


class SQLiteGraphClient:
    def __init__(self, repository_id: int, driver: SQLiteGraph):
        if repository_id is None:
            raise ValueError("repository_id is required")
        self.repository_id = repository_id
        self.driver = driver

    @staticmethod
    def _label(value, allowed):
        label = next((label for label in allowed if label.lower() == value.lower()), None)
        if label is None:
            raise ValueError(f"Invalid graph label/type: {value}")
        return label

    def _node(self, db, node_type, name, properties=None, identity=None):
        label = self._label(node_type, VALID_NODE_TYPES)
        identity = identity if identity is not None else name
        row = db.execute("SELECT id,props FROM nodes WHERE repo=? AND label=? AND identity=?",
                         (self.repository_id, label, identity)).fetchone()
        props = json.loads(row[1]) if row else {"name": name}
        props.update(properties or {})
        props.update(repository_id=self.repository_id, identity=identity, graph_schema_version=3)
        db.execute("INSERT INTO nodes(repo,label,identity,props) VALUES (?,?,?,?) ON CONFLICT(repo,label,identity) DO UPDATE SET props=excluded.props",
                   (self.repository_id, label, identity, json.dumps(props)))
        return db.execute("SELECT id FROM nodes WHERE repo=? AND label=? AND identity=?", (self.repository_id, label, identity)).fetchone()[0]

    async def create_node(self, node_type, name, properties=None, identity=None):
        def write():
            with self.driver.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._node(db, node_type, name, properties, identity)
        await asyncio.to_thread(write)

    async def create_relationship(self, from_node_type, from_name, to_node_type, to_name, rel_type,
                                  from_identity=None, to_identity=None, from_properties=None,
                                  to_properties=None, properties=None):
        await self.create_relationships([dict(from_node_type=from_node_type, from_name=from_name,
            to_node_type=to_node_type, to_name=to_name, rel_type=rel_type, from_identity=from_identity,
            to_identity=to_identity, from_properties=from_properties, to_properties=to_properties, properties=properties)])

    async def create_relationships(self, rows):
        def write():
            with self.driver.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for row in rows:
                    source = self._node(db, row["from_node_type"], row["from_name"], row.get("from_properties"), row.get("from_identity"))
                    target = self._node(db, row["to_node_type"], row["to_name"], row.get("to_properties"), row.get("to_identity"))
                    rel = self._label(row["rel_type"], VALID_RELATIONSHIP_TYPES)
                    existing = db.execute("SELECT props FROM edges WHERE source=? AND target=? AND type=?", (source,target,rel)).fetchone()
                    props = json.loads(existing[0]) if existing else {}
                    props.update(row.get("properties") or {})
                    db.execute("INSERT OR REPLACE INTO edges VALUES (?,?,?,?)", (source,target,rel,json.dumps(props)))
        await asyncio.to_thread(write)

    async def get_neighbors(self, name_or_path):
        def read():
            nodes, edges = self.driver._snapshot(self.repository_id)
            seeds = {node_id for node_id, node in nodes.items() if node.get("name") == name_or_path or node.get("path") == name_or_path}
            output = []
            for source, target, rel, props in edges:
                for seed, other in [(source,target), (target,source)]:
                    if seed in seeds:
                        output.append({"source": {"labels": list(nodes[seed].labels), "properties": dict(nodes[seed])},
                            "relationship": {"type": rel, "properties": json.loads(props)},
                            "target": {"labels": list(nodes[other].labels), "properties": dict(nodes[other])}})
            return output
        return await asyncio.to_thread(read)

    async def purge_repository(self):
        def write():
            with self.driver.connection() as db:
                db.execute("DELETE FROM nodes WHERE repo=?", (self.repository_id,))
        await asyncio.to_thread(write)

    async def get_focus_neighborhood(self, key, depth=1, max_nodes=40):
        depth, max_nodes = max(1, min(int(depth),3)), max(2, min(int(max_nodes),120))
        def read():
            started = time.perf_counter()
            nodes, edges = self.driver._snapshot(self.repository_id)
            anchors = [node_id for node_id, node in nodes.items() if node.get("name") == key or node.get("path") == key][:5]
            output = {"key":key,"focus":None,"matches":len(anchors),"levels":{"in":{},"out":{}},"edges":[],
                      "blast":[],"totals":{"incoming":None,"outgoing":None},"bounds":{"depth":depth,"max_nodes":max_nodes},
                      "truncated":{"in":False,"out":False},"elapsed_ms":0.0,"error":None}
            if not anchors:
                return output
            def row(node_id):
                node = nodes[node_id]
                return {"id":str(node_id),"name":node.get("name"),"label":next(iter(node.labels)),
                        "kind":node.get("kind"),"path":node.get("path"),
                        "degree":sum(1 for source,target,_,_ in edges if node_id in (source,target))}
            anchor = anchors[0]
            output["focus"] = row(anchor)
            output["totals"] = {"incoming":sum(target==anchor for _,target,_,_ in edges),
                                "outgoing":sum(source==anchor for source,_,_,_ in edges)}
            seen = {anchor}
            for direction, budget in [("in",max_nodes//2),("out",max_nodes-max_nodes//2)]:
                frontier = [anchor]
                for level in range(1,depth+1):
                    found = []
                    for parent in frontier:
                        for source,target,rel,_ in edges:
                            child = source if direction == "in" and target == parent else target if direction == "out" and source == parent else None
                            if child is None or child in seen:
                                continue
                            if budget <= 0:
                                output["truncated"][direction] = True
                                break
                            seen.add(child)
                            found.append({**row(child),"parent":str(parent),"rel":rel})
                            output["edges"].append({"from":str(source),"to":str(target),"type":rel})
                            budget -= 1
                    if found:
                        output["levels"][direction][level] = found
                    if direction == "in":
                        output["blast"].extend({**item,"depth":level} for item in found)
                    frontier = [int(item["id"]) for item in found]
                    if not frontier:
                        break
            output["elapsed_ms"] = round((time.perf_counter()-started)*1000,2)
            return output
        return await asyncio.to_thread(read)

    async def clear_graph(self):
        # The repository-owned interface never clears another repository.
        await self.purge_repository()

    async def delete_repository_files_not_in(self, valid_paths: set[str]):
        def write():
            with self.driver.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute("SELECT id,label,props FROM nodes WHERE repo=?", (self.repository_id,)).fetchall()
                removed = [node_id for node_id, label, raw in rows
                           if (label == "File" and json.loads(raw).get("path") not in valid_paths)
                           or (label == "Symbol" and json.loads(raw).get("file_path")
                               and json.loads(raw)["file_path"] not in valid_paths)]
                db.executemany("DELETE FROM nodes WHERE id=?", [(node_id,) for node_id in removed])
        await asyncio.to_thread(write)
