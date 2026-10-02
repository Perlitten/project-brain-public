import json
from pathlib import Path
from typing import Dict, Any, Optional

from brain.config.paths import get_repo_root
from brain.config.settings import settings
from brain.graph.graph_client import GraphClient
from brain.graph.schema import VALID_NODE_TYPES, VALID_RELATIONSHIP_TYPES, NodeType, RelationshipType


def resolve_grafify_output_path(explicit: Optional[str] = None) -> Path:
    """Resolve Grafify JSON path from CLI arg, env, or common defaults."""
    if explicit:
        return Path(explicit).resolve()
    if settings.GRAFIFY_OUTPUT_PATH:
        return Path(settings.GRAFIFY_OUTPUT_PATH).resolve()

    repo_root = get_repo_root()
    candidates = [
        repo_root.parent / "graphify-out" / "graph.json",
        repo_root / "graphify-out" / "graph.json",
        Path("D:/Brain/graphify-out/graph.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0]

class GrafifyIntegration:
    """Helper to import a JSON output of Grafify into Neo4j via GraphClient."""

    def __init__(
        self,
        repository_id: Optional[int] = None,
        client: Optional[GraphClient] = None,
    ):
        if client is None and repository_id is None:
            raise ValueError("repository_id is required when no GraphClient is supplied")
        if client is None:
            assert repository_id is not None
            client = GraphClient(repository_id=repository_id)
        self.client = client

    async def import_json_file(self, file_path: str) -> None:
        """Reads a Grafify JSON output file and loads it into Neo4j."""
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        await self.import_data(data)

    async def import_data(self, data: Dict[str, Any]) -> None:
        """Processes a raw graph dictionary and imports nodes and relationships."""
        # 1. Parse and import nodes
        nodes_list = data.get("nodes", [])
        if not isinstance(nodes_list, list):
            nodes_list = []

        node_id_to_type: Dict[str, str] = {}
        node_id_to_name: Dict[str, str] = {}

        for node in nodes_list:
            if not isinstance(node, dict):
                continue

            node_id = str(node.get("id") or node.get("name") or "")
            if not node_id:
                continue

            name = str(node.get("name") or node.get("id") or "")
            node_type = str(node.get("type") or node.get("label") or node.get("node_type") or "")

            normalized_type = self._normalize_node_type(node_type)
            node_id_to_type[node_id] = normalized_type
            node_id_to_name[node_id] = name

            # Gather properties (either nested or standard keys)
            properties = node.get("properties")
            if not isinstance(properties, dict):
                properties = {
                    k: v for k, v in node.items()
                    if k not in {"id", "name", "type", "label", "node_type", "properties"}
                }

            await self.client.create_node(
                node_type=normalized_type,
                name=name,
                properties=properties
            )

        # 2. Parse and import edges/relationships
        edges_list = data.get("edges") or data.get("relationships") or data.get("links") or []
        if not isinstance(edges_list, list):
            edges_list = []

        for edge in edges_list:
            if not isinstance(edge, dict):
                continue

            source_id = str(edge.get("source") or edge.get("from") or "")
            target_id = str(edge.get("target") or edge.get("to") or "")

            if not source_id or not target_id:
                continue

            # Resolve names and types from nodes lookup
            from_name = node_id_to_name.get(source_id, source_id)
            from_node_type = node_id_to_type.get(source_id, NodeType.MODULE.value)

            to_name = node_id_to_name.get(target_id, target_id)
            to_node_type = node_id_to_type.get(target_id, NodeType.MODULE.value)

            rel_type = str(edge.get("type") or edge.get("label") or edge.get("rel_type") or edge.get("relationship_type") or "")
            normalized_rel = self._normalize_relationship_type(rel_type)

            properties = edge.get("properties")
            if not isinstance(properties, dict):
                properties = {
                    k: v for k, v in edge.items()
                    if k not in {"source", "from", "target", "to", "type", "label", "rel_type", "relationship_type", "properties"}
                }

            await self.client.create_relationship(
                from_node_type=from_node_type,
                from_name=from_name,
                to_node_type=to_node_type,
                to_name=to_name,
                rel_type=normalized_rel,
                properties=properties
            )

    def _normalize_node_type(self, node_type: str) -> str:
        if not node_type:
            return NodeType.MODULE.value

        if node_type in VALID_NODE_TYPES:
            return node_type

        for valid in VALID_NODE_TYPES:
            if valid.lower() == node_type.lower():
                return valid

        return NodeType.MODULE.value

    def _normalize_relationship_type(self, rel_type: str) -> str:
        if not rel_type:
            return RelationshipType.RELATED_TO.value

        if rel_type in VALID_RELATIONSHIP_TYPES:
            return rel_type

        for valid in VALID_RELATIONSHIP_TYPES:
            if valid.upper() == rel_type.upper():
                return valid

        return RelationshipType.RELATED_TO.value
