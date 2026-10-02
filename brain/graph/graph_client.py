from time import perf_counter
from typing import Optional, List, Dict, Any
from brain.database.session import neo4j_driver
from brain.graph.schema import VALID_NODE_TYPES, VALID_RELATIONSHIP_TYPES

GRAPH_SCHEMA_VERSION = 3

EXCLUDED_HUB_SYMBOLS = {
    "str",
    "Path",
    "get",
    "append",
    "list",
    "dict",
    "int",
    "float",
    "bool",
    "set",
    "len",
    "type",
}

# Bounds for get_focus_neighborhood. The graph this dashboard reads has ~24k
# nodes and ~123k relationships and carries no index on :*(name), so every
# traversal has to declare its own ceiling — an unbounded expansion from a hub
# node (a Repository CONTAINS every Module and File) walks the whole store.
FOCUS_DEFAULT_DEPTH = 1
FOCUS_MAX_DEPTH = 3
FOCUS_DEFAULT_MAX_NODES = 40
FOCUS_MAX_NODES = 120
# How many nodes may match one focus key before we stop looking for more. The
# key is matched against .name/.path, which are not unique across labels.
FOCUS_ANCHOR_LIMIT = 5


async def ensure_graph_schema(driver=None) -> None:
    """Create the repository-identity indexes required by scoped graph reads."""
    graph_driver = driver or neo4j_driver
    async with graph_driver.session() as session:
        for node_type in sorted(VALID_NODE_TYPES):
            index_name = f"graph_{node_type.lower()}_repository_id"
            await session.run(f"CREATE INDEX {index_name} IF NOT EXISTS FOR (n:{node_type}) ON (n.repository_id)")
            identity_index_name = f"graph_{node_type.lower()}_identity"
            await session.run(
                f"CREATE INDEX {identity_index_name} IF NOT EXISTS FOR (n:{node_type}) "
                "ON (n.graph_schema_version, n.repository_id, n.identity)"
            )
def build_qualified_symbol_identity(
    repository_id: int,
    file_path: str,
    qualified_name: str,
    symbol_kind: Optional[str] = None,
) -> str:
    """Construct a collision-proof qualified symbol identity for Neo4j MERGE.

    Prevents symbols named 'get' or 'str' across different files/repositories
    from collapsing into a single node.
    """
    clean_path = file_path.replace("\\", "/").strip().lstrip("/")
    kind_str = symbol_kind.lower().strip() if symbol_kind else "symbol"
    return f"{repository_id}:{clean_path}:{qualified_name}:{kind_str}"


class GraphClient:
    """Client for interacting with the Neo4j graph database."""

    def __init__(self, repository_id: int, driver=None):
        if repository_id is None:
            raise ValueError("repository_id is required and cannot be None")
        self.repository_id = repository_id
        self.driver = driver or neo4j_driver

    async def create_node(
        self, node_type: str, name: str, properties: Optional[dict] = None, identity: Optional[str] = None
    ) -> None:
        """Runs a Cypher query to MERGE a node of that label with name property and other properties."""
        # Validate node type, with case-insensitive normalization if possible
        if node_type not in VALID_NODE_TYPES:
            normalized = next((v for v in VALID_NODE_TYPES if v.lower() == node_type.lower()), None)
            if normalized:
                node_type = normalized
            else:
                raise ValueError(f"Invalid node type: {node_type}. Must be one of: {sorted(list(VALID_NODE_TYPES))}")

        props = properties or {}
        identity_key = identity if identity is not None else name
        query = (
            f"MERGE (n:{node_type} "
            f"{{graph_schema_version: $graph_schema_version, repository_id: $repository_id, identity: $identity}}) "
            f"ON CREATE SET n.name = $name, n += $props "
            f"ON MATCH SET n += $props"
        )
        async with self.driver.session() as session:
            await session.run(
                query,
                graph_schema_version=GRAPH_SCHEMA_VERSION,
                repository_id=self.repository_id,
                identity=identity_key,
                name=name,
                props=props,
            )

    async def create_relationship(
        self,
        from_node_type: str,
        from_name: str,
        to_node_type: str,
        to_name: str,
        rel_type: str,
        from_identity: Optional[str] = None,
        to_identity: Optional[str] = None,
        from_properties: Optional[dict] = None,
        to_properties: Optional[dict] = None,
        properties: Optional[dict] = None,
    ) -> None:
        """Runs a Cypher query to MERGE nodes and a MERGE relationship between them."""
        # Validate from_node_type
        if from_node_type not in VALID_NODE_TYPES:
            normalized = next((v for v in VALID_NODE_TYPES if v.lower() == from_node_type.lower()), None)
            if normalized:
                from_node_type = normalized
            else:
                raise ValueError(f"Invalid from_node_type: {from_node_type}")

        # Validate to_node_type
        if to_node_type not in VALID_NODE_TYPES:
            normalized = next((v for v in VALID_NODE_TYPES if v.lower() == to_node_type.lower()), None)
            if normalized:
                to_node_type = normalized
            else:
                raise ValueError(f"Invalid to_node_type: {to_node_type}")

        # Validate relationship type
        if rel_type not in VALID_RELATIONSHIP_TYPES:
            normalized = next((v for v in VALID_RELATIONSHIP_TYPES if v.upper() == rel_type.upper()), None)
            if normalized:
                rel_type = normalized
            else:
                raise ValueError(
                    f"Invalid rel_type: {rel_type}. Must be one of: {sorted(list(VALID_RELATIONSHIP_TYPES))}"
                )

        from_identity_key = from_identity if from_identity is not None else from_name
        to_identity_key = to_identity if to_identity is not None else to_name
        from_props = from_properties or {}
        to_props = to_properties or {}
        rel_props = properties or {}
        query = (
            f"MERGE (a:{from_node_type} "
            f"{{graph_schema_version: $graph_schema_version, repository_id: $repository_id, identity: $from_identity}}) "
            f"ON CREATE SET a.name = $from_name, a += $from_props "
            f"ON MATCH SET a += $from_props "
            f"MERGE (b:{to_node_type} "
            f"{{graph_schema_version: $graph_schema_version, repository_id: $repository_id, identity: $to_identity}}) "
            f"ON CREATE SET b.name = $to_name, b += $to_props "
            f"ON MATCH SET b += $to_props "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            f"ON CREATE SET r += $rel_props "
            f"ON MATCH SET r += $rel_props"
        )
        async with self.driver.session() as session:
            await session.run(
                query,
                graph_schema_version=GRAPH_SCHEMA_VERSION,
                repository_id=self.repository_id,
                from_identity=from_identity_key,
                to_identity=to_identity_key,
                from_name=from_name,
                to_name=to_name,
                from_props=from_props,
                to_props=to_props,
                rel_props=rel_props,
            )

    async def get_neighbors(self, name_or_path: str) -> List[Dict[str, Any]]:
        """Returns nodes and relationships directly connected to a node matching name or path."""
        query = (
            "MATCH (n) "
            "WHERE (n.name = $target OR n.path = $target) "
            "AND n.repository_id = $repository_id "
            "MATCH (n)-[r]-(m) "
            "WHERE m.repository_id = $repository_id "
            "RETURN n, r, m"
        )
        neighbors = []
        async with self.driver.session() as session:
            result = await session.run(query, target=name_or_path, repository_id=self.repository_id)
            async for record in result:
                node_n = record["n"]
                rel_r = record["r"]
                node_m = record["m"]

                neighbors.append(
                    {
                        "source": {"labels": list(node_n.labels), "properties": dict(node_n)},
                        "relationship": {"type": rel_r.type, "properties": dict(rel_r)},
                        "target": {"labels": list(node_m.labels), "properties": dict(node_m)},
                    }
                )
        return neighbors

    async def create_relationships(self, rows: list[dict]) -> None:
        """UNWIND writes grouped by validated labels/type; identities stay scoped."""
        from collections import defaultdict
        groups = defaultdict(list)
        for row in rows:
            source, target, rel = row["from_node_type"], row["to_node_type"], row["rel_type"]
            if source not in VALID_NODE_TYPES or target not in VALID_NODE_TYPES or rel not in VALID_RELATIONSHIP_TYPES:
                raise ValueError("Invalid graph relationship label/type")
            groups[(source, target, rel)].append({
                "source": row.get("from_identity") or row["from_name"],
                "target": row.get("to_identity") or row["to_name"],
                "from_name": row["from_name"], "to_name": row["to_name"],
                "from_props": row.get("from_properties") or {}, "to_props": row.get("to_properties") or {},
                "props": row.get("properties") or {},
            })
        async with self.driver.session() as session:
            for (source, target, rel), batch in sorted(groups.items()):
                result = await session.run(
                    f"UNWIND $rows AS row "
                    f"MERGE (a:{source} {{graph_schema_version: $version, repository_id: $repo, identity: row.source}}) "
                    "ON CREATE SET a.name = row.from_name SET a += row.from_props "
                    f"MERGE (b:{target} {{graph_schema_version: $version, repository_id: $repo, identity: row.target}}) "
                    "ON CREATE SET b.name = row.to_name SET b += row.to_props "
                    f"MERGE (a)-[r:{rel}]->(b) SET r += row.props",
                    rows=batch, version=GRAPH_SCHEMA_VERSION, repo=self.repository_id,
                )
                await result.consume()

    # ---------------------------------------------------------------- focus --
    # get_neighbors above answers "what touches this node?" in one undirected,
    # unbounded hop. The dependency-focus screen needs the same walk with three
    # things it does not have: a direction split (dependents vs dependencies),
    # a transitive blast radius, and a ceiling on both so the dashboard cannot
    # be made to hang by focusing a hub. Hence a sibling method rather than a
    # flag on that one — its callers want the raw undirected dump.

    @staticmethod
    def _node_row(record: Any) -> Dict[str, Any]:
        """One traversal row -> the fields the screen renders. No defaults are
        invented: a missing label/kind/degree stays None so the caller can omit
        the fragment instead of printing something it never measured."""
        return {
            "id": record["id"],
            "name": record["name"],
            "label": record["label"],
            "kind": record["kind"],
            "path": record["path"],
            "degree": record["degree"],
        }

    async def _focus_anchor(self, session: Any, key: str) -> List[Dict[str, Any]]:
        """Resolve a focus key (.name or .path, as get_neighbors matches) to nodes."""
        query = (
            "MATCH (n) "
            "WHERE (n.name = $key OR n.path = $key) "
            "AND n.repository_id = $repository_id "
            "WITH n, COUNT { (n)--() } AS degree "
            "RETURN elementId(n) AS id, n.name AS name, labels(n)[0] AS label, "
            "       n.kind AS kind, n.path AS path, degree "
            "ORDER BY degree DESC "
            "LIMIT $limit"
        )
        rows = []
        res = await session.run(query, key=key, limit=FOCUS_ANCHOR_LIMIT, repository_id=self.repository_id)
        async for record in res:
            rows.append(self._node_row(record))
        return rows

    async def _focus_side_counts(self, session: Any, node_id: str) -> Dict[str, Optional[int]]:
        """Distinct neighbour counts per direction for the focus node only.

        Counted with count(DISTINCT m), not the relationship degree: two nodes
        joined by both CONTAINS and CALLS are one neighbour, and the "+N more"
        chip must not claim otherwise. Cost is O(degree(focus)) and it runs for
        exactly one node.
        """
        query = (
            "MATCH (n) WHERE elementId(n) = $id "
            "AND n.repository_id = $repository_id "
            "CALL { WITH n MATCH (n)<-[]-(m) WHERE m.repository_id = $repository_id RETURN count(DISTINCT m) AS incoming } "
            "CALL { WITH n MATCH (n)-[]->(m2) WHERE m2.repository_id = $repository_id RETURN count(DISTINCT m2) AS outgoing } "
            "RETURN incoming, outgoing"
        )
        res = await session.run(query, id=node_id, repository_id=self.repository_id)
        record = await res.single()
        if record is None:
            return {"incoming": None, "outgoing": None}
        return {"incoming": record["incoming"], "outgoing": record["outgoing"]}

    async def _expand_level(
        self,
        session: Any,
        frontier: List[str],
        exclude: List[str],
        direction: str,
        limit: int,
        rank: bool,
    ) -> List[Dict[str, Any]]:
        """One BFS hop from `frontier`, at most `limit` new nodes.

        `rank=True` sorts by degree so the first hop off the focus shows the
        heaviest neighbours first; Neo4j serves that with a top-k heap, so the
        cost stays O(degree(frontier)). Deeper hops pass rank=False: without an
        ORDER BY the LIMIT short-circuits the expansion instead of draining
        every relationship of every frontier node first.
        """
        arrow = "<-[r]-" if direction == "in" else "-[r]->"
        query = (
            f"MATCH (a){arrow}(m) "
            "WHERE elementId(a) IN $frontier AND NOT elementId(m) IN $exclude "
            "AND a.repository_id = $repository_id "
            "AND m.repository_id = $repository_id "
            "RETURN elementId(a) AS parent, elementId(m) AS id, m.name AS name, "
            "       labels(m)[0] AS label, m.kind AS kind, m.path AS path, "
            "       type(r) AS rel, COUNT { (m)--() } AS degree "
            + ("ORDER BY degree DESC " if rank else "")
            + "LIMIT $limit"
        )
        rows = []
        res = await session.run(
            query, frontier=frontier, exclude=exclude, limit=limit, repository_id=self.repository_id
        )
        async for record in res:
            row = self._node_row(record)
            row["parent"] = record["parent"]
            row["rel"] = record["rel"]
            rows.append(row)
        return rows

    async def get_focus_neighborhood(
        self,
        key: str,
        depth: int = FOCUS_DEFAULT_DEPTH,
        max_nodes: int = FOCUS_DEFAULT_MAX_NODES,
    ) -> Dict[str, Any]:
        """Bounded dependency neighbourhood of one node.

        Returns the focus node, its incoming neighbours (things that depend on
        it) and outgoing neighbours (things it depends on) split by BFS level,
        the edge that put each node in the set, and the blast radius — every
        node transitively reachable against the arrows, i.e. everything that can
        break when the focus changes.

        Both bounds are explicit and both are clamped here, not by the caller:
          depth      1..FOCUS_MAX_DEPTH   (BFS levels per direction)
          max_nodes  2..FOCUS_MAX_NODES   (total nodes, split evenly per side)

        Every node is visited at most once, so the walk emits a tree: each
        non-focus node carries exactly one parent edge and cross-links between
        two nodes already in the set are not re-reported.
        """
        depth = max(1, min(int(depth or FOCUS_DEFAULT_DEPTH), FOCUS_MAX_DEPTH))
        max_nodes = max(2, min(int(max_nodes or FOCUS_DEFAULT_MAX_NODES), FOCUS_MAX_NODES))

        out: Dict[str, Any] = {
            "key": key,
            "focus": None,
            "matches": 0,
            "levels": {"in": {}, "out": {}},
            "edges": [],
            "blast": [],
            "totals": {"incoming": None, "outgoing": None},
            "bounds": {"depth": depth, "max_nodes": max_nodes},
            "truncated": {"in": False, "out": False},
            "elapsed_ms": None,
            "error": None,
        }
        if not key:
            return out

        started = perf_counter()
        try:
            async with self.driver.session() as session:
                anchors = await self._focus_anchor(session, key)
                out["matches"] = len(anchors)
                if not anchors:
                    out["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
                    return out

                focus = anchors[0]
                out["focus"] = focus
                out["totals"] = await self._focus_side_counts(session, focus["id"])

                visited = [focus["id"]]
                budgets = {"in": max_nodes // 2, "out": max_nodes - max_nodes // 2}

                for direction in ("in", "out"):
                    remaining = budgets[direction]
                    frontier = [focus["id"]]
                    for level in range(1, depth + 1):
                        if remaining <= 0 or not frontier:
                            if remaining <= 0:
                                out["truncated"][direction] = True
                            break
                        rows = await self._expand_level(
                            session,
                            frontier=frontier,
                            exclude=visited,
                            direction=direction,
                            # One row past the budget: enough to tell "the bound
                            # cut this level" from "that is all there was",
                            # without paying for a second counting query.
                            limit=remaining + 1,
                            rank=(level == 1),
                        )
                        level_nodes: list[dict[str, Any]] = []
                        seen_here: set[Any] = set()
                        for row in rows:
                            if row["id"] in seen_here:
                                continue
                            # The LIMIT is in the Cypher too; the count bound is
                            # re-applied here so it holds whatever the server
                            # hands back.
                            if len(level_nodes) >= remaining:
                                out["truncated"][direction] = True
                                break
                            seen_here.add(row["id"])
                            visited.append(row["id"])
                            level_nodes.append(row)
                            # Edges always point from dependent to dependency,
                            # so the canvas can draw every one of them left to
                            # right without deciding direction itself.
                            if direction == "in":
                                edge = {"from": row["id"], "to": row["parent"], "type": row["rel"]}
                            else:
                                edge = {"from": row["parent"], "to": row["id"], "type": row["rel"]}
                            out["edges"].append(edge)
                        remaining -= len(level_nodes)
                        if level_nodes:
                            out["levels"][direction][level] = level_nodes
                        frontier = [n["id"] for n in level_nodes]

                # Blast radius = the dependents side, at every depth reached.
                for level in sorted(out["levels"]["in"]):
                    for node in out["levels"]["in"][level]:
                        out["blast"].append({**node, "depth": level})
        except Exception as exc:
            out["error"] = str(exc)

        out["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
        return out

    async def clear_graph(self) -> None:
        """Runs a Cypher query MATCH (n) DETACH DELETE n to wipe all nodes and edges."""
        query = "MATCH (n) DETACH DELETE n"
        async with self.driver.session() as session:
            await session.run(query)

    async def purge_repository(self) -> None:
        """Delete only nodes owned by this client repository."""
        async with self.driver.session() as session:
            # One label-scoped query per known node type lets Neo4j use the
            # repository_id indexes installed by ensure_graph_schema().
            for node_type in sorted(VALID_NODE_TYPES):
                await session.run(
                    f"MATCH (n:{node_type}) WHERE n.repository_id = $repository_id "
                    "CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS",
                    repository_id=self.repository_id,
                )

    async def delete_repository_files_not_in(self, valid_paths: set[str]) -> None:
        """Remove File nodes for a repository whose paths are no longer indexed."""
        if not valid_paths:
            return
        query = "MATCH (f:File) WHERE f.repository_id = $repository_id AND NOT f.path IN $paths DETACH DELETE f"
        async with self.driver.session() as session:
            await session.run(
                query,
                repository_id=self.repository_id,
                paths=list(valid_paths),
            )
