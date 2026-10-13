"""Repository graph enrichment with explicit writes, reads and progress ownership.

SQL indexing, providers and worker fencing remain with FileIndexer. This stage
receives its already-scoped graph client and preserves per-file write order,
partial-failure handling, buffer clearing and progress publication.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from brain.graph.write_buffer import GraphWriteBuffer
from brain.graph.schema import NodeType, RelationshipType
from brain.retrieval import graph_extractors as extractors


class RepositoryGraphEnricher:
    def __init__(
        self, graph_client: GraphWriteBuffer, progress: dict[str, Any],
        failure: Callable[..., None], publish_progress: Callable[[], Awaitable[None]],
        read_file: Callable[..., str],
    ) -> None:
        self.graph_client = graph_client
        self.progress = progress
        self._failure = failure
        self._publish_progress = publish_progress
        self.read_file = read_file

    async def enrich(self, repo_name: str, repo_path: Path, scanned_files: list[Path]) -> None:
        logger.info("Neo4j: Starting post-processing to enrich graph relationships...")

        # 1. Create Repository node
        try:
            await self.graph_client.create_node(
                node_type=NodeType.REPOSITORY.value, name=repo_name, properties={"path": repo_path.as_posix()}
            )
        except Exception as e:
            logger.warning(f"Neo4j: Failed to create Repository node: {e}")
            self.progress["graph"]["failed"] += 1
            self._failure("graph_repository", e)

        # 2. Parse feature_map.yaml if it exists
        feature_map = {}
        for feature_map_filename in ["rules/feature_map.yaml", "feature_map.yaml"]:
            f_map_path = repo_path / feature_map_filename
            if f_map_path.exists():
                import yaml

                try:
                    _map_text = await asyncio.to_thread(self.read_file, f_map_path, "strict")
                    data = yaml.safe_load(_map_text) or {}
                    feature_map = data.get("features", data)
                    logger.info(f"Neo4j: Loaded feature map from {feature_map_filename}")
                    break
                except Exception as e:
                    logger.warning(f"Neo4j: Failed to parse feature map {feature_map_filename}: {e}")

        # Ensure Feature nodes are created
        if feature_map:
            for feat_name in feature_map.keys():
                try:
                    await self.graph_client.create_node(node_type=NodeType.FEATURE.value, name=feat_name, properties={})
                except Exception as exc:
                    self.progress["graph"]["failed"] += 1
                    self._failure("graph_feature", exc)



        all_rel_paths = [fp.relative_to(repo_path).as_posix() for fp in scanned_files]
        by_stem: dict[str, list[str]] = defaultdict(list)
        for fp, rel in zip(scanned_files, all_rel_paths):
            by_stem[fp.stem].append(rel)
        self.progress["graph"]["discovered"] = len(scanned_files)

        # Track created modules to avoid duplicate queries
        created_modules: set[str] = set()

        for filepath in scanned_files:
            try:
                rel_path = filepath.relative_to(repo_path).as_posix()
                ext = filepath.suffix.lower()
                content = ""

                await self._link_hierarchy(repo_name, rel_path, created_modules)
                await self._link_features(rel_path, feature_map)
                content = await self._link_imports(filepath, ext, rel_path, by_stem)
                await self._link_semantics(filepath, ext, rel_path, content, all_rel_paths, feature_map)
                await self.graph_client.flush()
            except Exception as e:
                logger.error(f"Neo4j: Error post-processing file {filepath}: {e}")
                self.progress["graph"]["failed"] += 1
                self._failure("graph_post_process", e, rel_path)
                self.graph_client.rows.clear()
            else:
                self.progress["graph"]["processed"] += 1
            await self._publish_progress()

    async def _link_hierarchy(self, repo_name: str, rel_path: str, created_modules: set[str]) -> None:
        # A. Create Modules and BELONGS_TO/CONTAINS hierarchy
        parts = Path(rel_path).parent.parts
        current_module = None

        for idx, part in enumerate(parts):
            # We define module name/id by its relative subpath, e.g. "apps/api"
            module_path = "/".join(parts[: idx + 1])
            if module_path not in created_modules:
                await self.graph_client.create_node(
                    node_type=NodeType.MODULE.value, name=module_path, properties={"folder_name": part}
                )
                created_modules.add(module_path)

                # Link parent module or repository
                if idx == 0:
                    # Link root module to repository
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.REPOSITORY.value,
                        from_name=repo_name,
                        to_node_type=NodeType.MODULE.value,
                        to_name=module_path,
                        rel_type=RelationshipType.CONTAINS.value,
                    )
                else:
                    parent_path = "/".join(parts[:idx])
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.MODULE.value,
                        from_name=parent_path,
                        to_node_type=NodeType.MODULE.value,
                        to_name=module_path,
                        rel_type=RelationshipType.CONTAINS.value,
                    )
            current_module = module_path

        # Link file to its parent module
        if current_module:
            await self.graph_client.create_relationship(
                from_node_type=NodeType.MODULE.value,
                from_name=current_module,
                to_node_type=NodeType.FILE.value,
                to_name=rel_path,
                rel_type=RelationshipType.CONTAINS.value,
            )
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=rel_path,
                to_node_type=NodeType.MODULE.value,
                to_name=current_module,
                rel_type=RelationshipType.BELONGS_TO.value,
            )
        else:
            # Link top-level files directly to Repository
            await self.graph_client.create_relationship(
                from_node_type=NodeType.REPOSITORY.value,
                from_name=repo_name,
                to_node_type=NodeType.FILE.value,
                to_name=rel_path,
                rel_type=RelationshipType.CONTAINS.value,
            )


    async def _link_features(self, rel_path: str, feature_map: dict[str, Any]) -> None:
        # B. Mapped Features
        for feat_name, patterns in feature_map.items():
            for pattern in patterns:
                if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(rel_path, f"*{pattern}*"):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=rel_path,
                        to_node_type=NodeType.FEATURE.value,
                        to_name=feat_name,
                        rel_type=RelationshipType.BELONGS_TO.value,
                    )
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FEATURE.value,
                        from_name=feat_name,
                        to_node_type=NodeType.FILE.value,
                        to_name=rel_path,
                        rel_type=RelationshipType.RELATED_TO.value,
                    )
                    break


    async def _link_imports(self, filepath: Path, ext: str, rel_path: str, by_stem: dict[str, list[str]]) -> str:
        content = ""
        # C. Extract IMPORTS from Python, JS, TS
        if ext in [".py", ".js", ".ts", ".tsx"]:
            try:
                content = await asyncio.to_thread(self.read_file, filepath)

                imported_modules = []
                if ext == ".py":
                    matches1 = re.findall(r"^\s*import\s+([a-zA-Z0-9_\.]+)", content, re.MULTILINE)
                    matches2 = re.findall(r"^\s*from\s+([a-zA-Z0-9_\.]+)\s+import", content, re.MULTILINE)
                    imported_modules = matches1 + matches2
                else:
                    matches1 = re.findall(r'from\s+[\'"]([^\'"]+)[\'"]', content)
                    matches2 = re.findall(r'require\(\s*[\'"]([^\'"]+)[\'"]\s*\)', content)
                    imported_modules = matches1 + matches2

                for imp in imported_modules:
                    imp_cleaned = imp.replace(".", "/").strip("/")
                    imp_name = imp_cleaned.split("/")[-1]

                    for cand_rel in by_stem.get(imp_name, []):
                        if imp_cleaned in cand_rel or "/" not in imp_cleaned:
                            await self.graph_client.create_relationship(
                                from_node_type=NodeType.FILE.value,
                                from_name=rel_path,
                                to_node_type=NodeType.FILE.value,
                                to_name=cand_rel,
                                rel_type=RelationshipType.IMPORTS.value,
                            )
                            break
            except Exception as ie:
                logger.warning(f"Neo4j: Failed to parse imports from {rel_path}: {ie}")
                self.progress["graph"]["failed"] += 1
                self._failure("graph_import", ie, rel_path)

        return content

    async def _link_semantics(self, filepath: Path, ext: str, rel_path: str, content: str,
                              all_rel_paths: list[str], feature_map: dict[str, Any]) -> None:
        # D. TESTED_BY + CALLS/USES/AFFECTS enrichment
        file_content = ""
        if ext in {".py", ".js", ".ts", ".tsx"}:
            file_content = content  # already read in section C
        # Re-read if C didn't set it (covers .py whose section-C read
        # failed, plus the languages C doesn't handle).
        if not file_content and ext in {".py", ".kt", ".kts", ".go", ".rs", ".java"}:
            try:
                file_content = await asyncio.to_thread(self.read_file, filepath)
            except Exception:
                file_content = ""

        for edge in extractors.extract_tested_by(rel_path, all_rel_paths):
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.FILE.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.TESTED_BY.value,
                properties={
                    "confidence": edge.confidence,
                    "provenance": edge.provenance,
                    "inferred": edge.inferred,
                },
            )

        for edge in extractors.extract_tab_core_links(rel_path, all_rel_paths):
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.FILE.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.RELATED_TO.value,
                properties={
                    "confidence": edge.confidence,
                    "provenance": edge.provenance,
                    "inferred": edge.inferred,
                },
            )

        for edge in extractors.extract_calls(file_content, rel_path):
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.SYMBOL.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.CALLS.value,
                properties={
                    "confidence": edge.confidence,
                    "provenance": edge.provenance,
                    "inferred": edge.inferred,
                },
            )

        for edge in extractors.extract_uses(file_content, rel_path, ext):
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.SYMBOL.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.USES.value,
                properties={
                    "confidence": edge.confidence,
                    "provenance": edge.provenance,
                    "inferred": edge.inferred,
                },
            )

        for edge in extractors.extract_affects(rel_path, feature_map):
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.FEATURE.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.AFFECTS.value,
                properties={
                    "confidence": edge.confidence,
                    "provenance": edge.provenance,
                    "inferred": edge.inferred,
                },
            )

        # v6 P1 structural edges: ROUTE->endpoint, TEST->SOURCE (imports), SCRIPT->DOMAIN
        for edge in extractors.extract_route_edges(file_content, rel_path, ext):
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.API_ENDPOINT.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.ROUTE.value,
                properties={"confidence": edge.confidence, "provenance": edge.provenance},
            )
        for edge in extractors.extract_import_file_edges(file_content, rel_path, ext, all_rel_paths):
            if edge.rel_type != "TESTS":
                continue
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.FILE.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.TESTS.value,
                properties={"confidence": edge.confidence, "provenance": edge.provenance},
            )
        for edge in extractors.extract_script_domain_edges(rel_path, all_rel_paths):
            await self.graph_client.create_relationship(
                from_node_type=NodeType.FILE.value,
                from_name=edge.from_path,
                to_node_type=NodeType.FILE.value,
                to_name=edge.to_ref,
                rel_type=RelationshipType.SCRIPT_DOMAIN.value,
                properties={"confidence": edge.confidence, "provenance": edge.provenance},
            )
