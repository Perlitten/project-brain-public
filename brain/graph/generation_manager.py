"""Graphify v2 Versioned Generations & In-Memory / Neo4j Graph Store."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.graph.schema_v2 import GraphNodeV2, GraphRelationshipV2, NodeTypeV2, RelationshipTypeV2


@dataclass
class GenerationMetadata:
    generation_id: str
    repository_id: str
    git_revision: str
    created_at_utc: str
    status: str  # 'inactive', 'building', 'ready', 'active', 'failed', 'rolled_back'
    node_count: int = 0
    relationship_count: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class InMemoryGraphStore:
    """In-memory typed graph storage with fast index structures."""

    def __init__(self, generation_id: str):
        self.generation_id = generation_id
        self.nodes: Dict[str, GraphNodeV2] = {}
        self.relationships: List[GraphRelationshipV2] = []
        self.outgoing_edges: Dict[str, List[GraphRelationshipV2]] = {}
        self.incoming_edges: Dict[str, List[GraphRelationshipV2]] = {}

    def add_node(self, node: GraphNodeV2) -> None:
        self.nodes[node.qualified_id] = node

    def add_relationship(self, rel: GraphRelationshipV2) -> None:
        self.relationships.append(rel)
        self.outgoing_edges.setdefault(rel.source_id, []).append(rel)
        self.incoming_edges.setdefault(rel.target_id, []).append(rel)

    def get_node(self, qualified_id: str) -> Optional[GraphNodeV2]:
        return self.nodes.get(qualified_id)

    def get_outgoing(self, source_id: str) -> List[GraphRelationshipV2]:
        return self.outgoing_edges.get(source_id, [])

    def get_incoming(self, target_id: str) -> List[GraphRelationshipV2]:
        return self.incoming_edges.get(target_id, [])

    def find_nodes_by_type(self, node_type: NodeTypeV2) -> List[GraphNodeV2]:
        return [n for n in self.nodes.values() if n.node_type == node_type]


class GraphGenerationManager:
    """Manages versioned graph generation state, active pointer, and storage persistence."""

    def __init__(self, brain_dir: Path):
        self.brain_dir = brain_dir.resolve()
        self.generations_file = self.brain_dir / "graph_generations.jsonl"
        self.active_pointer_file = self.brain_dir / "active_graph_generation.json"
        self.store_dir = self.brain_dir / "graph_stores"
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _load_generations(self) -> List[GenerationMetadata]:
        if not self.generations_file.exists():
            return []
        items: List[GenerationMetadata] = []
        corrupted = False
        try:
            with open(self.generations_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        items.append(GenerationMetadata(**d))
                    except Exception:
                        corrupted = True
        except Exception:
            corrupted = True

        if corrupted and self.generations_file.exists():
            import shutil

            backup_file = self.generations_file.with_suffix(".jsonl.corrupted")
            try:
                shutil.copy2(self.generations_file, backup_file)
            except Exception:
                pass
        return items

    def _save_generations(self, items: List[GenerationMetadata]) -> None:
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self.generations_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item.to_dict()) + "\n")
        temp_file.replace(self.generations_file)

    def create_generation(self, repository_id: str, git_revision: str) -> GenerationMetadata:
        gen_id = f"gen-{int(time.time() * 1000)}"
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        meta = GenerationMetadata(
            generation_id=gen_id,
            repository_id=repository_id,
            git_revision=git_revision,
            created_at_utc=now_str,
            status="building",
        )

        items = self._load_generations()
        items.append(meta)
        self._save_generations(items)
        return meta

    def save_graph_store(self, store: InMemoryGraphStore, meta: GenerationMetadata) -> None:
        store_file = self.store_dir / f"{store.generation_id}.json"
        data = {
            "generation_id": store.generation_id,
            "nodes": [n.to_dict() for n in store.nodes.values()],
            "relationships": [r.to_dict() for r in store.relationships],
        }
        with open(store_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        meta.node_count = len(store.nodes)
        meta.relationship_count = len(store.relationships)
        meta.status = "ready"
        self.update_generation_metadata(meta)

    def load_graph_store(self, generation_id: str) -> Optional[InMemoryGraphStore]:
        store_file = self.store_dir / f"{generation_id}.json"
        if not store_file.exists():
            return None
        try:
            with open(store_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            store = InMemoryGraphStore(generation_id)
            for nd in data.get("nodes", []):
                nd["node_type"] = NodeTypeV2(nd["node_type"])
                store.add_node(GraphNodeV2(**nd))
            for rd in data.get("relationships", []):
                rd["rel_type"] = RelationshipTypeV2(rd["rel_type"])
                store.add_relationship(GraphRelationshipV2(**rd))
            return store
        except Exception:
            return None

    def update_generation_metadata(self, meta: GenerationMetadata) -> None:
        items = self._load_generations()
        for i, item in enumerate(items):
            if item.generation_id == meta.generation_id:
                items[i] = meta
                break
        self._save_generations(items)

    def get_active_generation_id(self) -> Optional[str]:
        if not self.active_pointer_file.exists():
            return None
        try:
            with open(self.active_pointer_file, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("active_generation_id")
        except Exception:
            return None

    def get_active_graph_store(self) -> Optional[InMemoryGraphStore]:
        gen_id = self.get_active_generation_id()
        if not gen_id:
            return None
        return self.load_graph_store(gen_id)

    def activate_generation(self, generation_id: str) -> bool:
        items = self._load_generations()
        target: Optional[GenerationMetadata] = None
        for item in items:
            if item.generation_id == generation_id:
                target = item
                break

        if not target or target.status not in {"ready", "active"}:
            return False

        # Mark all currently active generations as inactive
        for item in items:
            if item.status == "active":
                item.status = "ready"
        target.status = "active"
        self._save_generations(items)

        with open(self.active_pointer_file, "w", encoding="utf-8") as f:
            json.dump({"active_generation_id": generation_id, "activated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)

        return True

    def rollback(self) -> Optional[str]:
        items = self._load_generations()
        current_active = self.get_active_generation_id()

        ready_generations = [i for i in items if i.status in {"ready", "active"} and i.generation_id != current_active]
        if not ready_generations:
            return None

        # Pick the most recent prior ready generation
        prior = ready_generations[-1]
        if self.activate_generation(prior.generation_id):
            return prior.generation_id
        return None

    def list_generations(self) -> List[GenerationMetadata]:
        return self._load_generations()
