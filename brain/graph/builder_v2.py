"""Graphify v2 Repository Graph Builder."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Tuple

from brain.graph.extractors_v2 import ConfigExtractor, PythonStaticExtractor
from brain.graph.generation_manager import GenerationMetadata, GraphGenerationManager, InMemoryGraphStore
from brain.graph.quality_gate import GraphQualityReport, GraphQualityValidator


class GraphBuilderV2:
    """Builds a typed Graphify v2 generation for a repository."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self.repo_id = self.repo_path.name
        self.brain_dir = self.repo_path / ".brain"
        self.mgr = GraphGenerationManager(self.brain_dir)

    def build_generation(self, git_revision: str = "HEAD") -> Tuple[GenerationMetadata, GraphQualityReport]:
        meta = self.mgr.create_generation(self.repo_id, git_revision)
        store = InMemoryGraphStore(meta.generation_id)

        python_extractor = PythonStaticExtractor(self.repo_id, meta.generation_id, self.repo_path)
        config_extractor = ConfigExtractor(self.repo_id, meta.generation_id, self.repo_path)

        start_time = time.time()

        # Scan repository files
        for p in self.repo_path.rglob("*"):
            if not p.is_file():
                continue

            # Exclude hidden directories like .git, .brain, __pycache__, node_modules
            parts = p.relative_to(self.repo_path).parts
            if any(part.startswith(".") or part in {"__pycache__", "node_modules", "venv"} for part in parts):
                continue

            if p.suffix == ".py":
                nodes, rels = python_extractor.extract_file(p)
                for n in nodes:
                    store.add_node(n)
                for r in rels:
                    store.add_relationship(r)
            elif p.suffix in {".yaml", ".yml", ".json"} and not p.name.endswith(".jsonl"):
                nodes, rels = config_extractor.extract_config(p)
                for n in nodes:
                    store.add_node(n)
                for r in rels:
                    store.add_relationship(r)

        duration = time.time() - start_time

        # Validate
        report = GraphQualityValidator.validate(store, self.repo_id)

        meta.metrics = report.to_dict()
        meta.metrics["build_duration_seconds"] = duration

        self.mgr.save_graph_store(store, meta)

        return meta, report
