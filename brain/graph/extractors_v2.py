"""Graphify v2 Static Python and Configuration Extractors."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import List, Tuple

import yaml

from brain.graph.identity import (
    build_api_endpoint_qualified_id,
    build_config_qualified_id,
    build_file_qualified_id,
    build_module_qualified_id,
    build_symbol_qualified_id,
    build_worker_task_qualified_id,
)
from brain.graph.schema_v2 import GraphNodeV2, GraphRelationshipV2, NodeTypeV2, RelationshipTypeV2


class PythonStaticExtractor:
    """Extracts typed nodes and relationships from Python files using AST."""

    def __init__(self, repo_id: str, generation_id: str, repo_root: Path):
        self.repo_id = repo_id
        self.generation_id = generation_id
        self.repo_root = repo_root.resolve()

    def extract_file(self, file_path: Path) -> Tuple[List[GraphNodeV2], List[GraphRelationshipV2]]:
        nodes: List[GraphNodeV2] = []
        rels: List[GraphRelationshipV2] = []

        rel_path = file_path.relative_to(self.repo_root).as_posix()
        file_id = build_file_qualified_id(self.repo_id, self.generation_id, rel_path)

        file_node = GraphNodeV2(
            node_type=NodeTypeV2.FILE,
            qualified_id=file_id,
            repository_id=self.repo_id,
            generation_id=self.generation_id,
            normalized_path=rel_path,
            language="python",
        )
        nodes.append(file_node)

        module_name = rel_path.replace("/", ".").removesuffix(".py")
        module_id = build_module_qualified_id(self.repo_id, self.generation_id, module_name)

        module_node = GraphNodeV2(
            node_type=NodeTypeV2.MODULE,
            qualified_id=module_id,
            repository_id=self.repo_id,
            generation_id=self.generation_id,
            normalized_path=rel_path,
            language="python",
            properties={"module_name": module_name},
        )
        nodes.append(module_node)

        rels.append(
            GraphRelationshipV2(
                rel_type=RelationshipTypeV2.CONTAINS,
                source_id=file_id,
                target_id=module_id,
                provenance="ast_module",
                extractor="PythonStaticExtractor",
            )
        )

        try:
            source_code = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source_code, filename=rel_path)
        except Exception as e:
            file_node.properties["parse_error"] = str(e)
            return nodes, rels

        # Traverse AST
        class ASTVisitor(ast.NodeVisitor):
            def __init__(self_ast):
                self_ast.current_class: str = ""
                self_ast.current_class_id: str = ""

            def visit_Import(self_ast, node: ast.Import):
                for alias in node.names:
                    imp_module_id = build_module_qualified_id(self.repo_id, self.generation_id, alias.name)
                    rels.append(
                        GraphRelationshipV2(
                            rel_type=RelationshipTypeV2.IMPORTS,
                            source_id=module_id,
                            target_id=imp_module_id,
                            provenance="ast_import",
                            extractor="PythonStaticExtractor",
                            confidence="exact",
                            source_location={"line": node.lineno},
                        )
                    )

            def visit_ImportFrom(self_ast, node: ast.ImportFrom):
                if node.module:
                    imp_module_id = build_module_qualified_id(self.repo_id, self.generation_id, node.module)
                    rels.append(
                        GraphRelationshipV2(
                            rel_type=RelationshipTypeV2.IMPORTS,
                            source_id=module_id,
                            target_id=imp_module_id,
                            provenance="ast_import_from",
                            extractor="PythonStaticExtractor",
                            confidence="exact",
                            source_location={"line": node.lineno},
                        )
                    )

            def visit_ClassDef(self_ast, node: ast.ClassDef):
                class_id = build_symbol_qualified_id(
                    self.repo_id, self.generation_id, "python", rel_path, "class", node.name
                )
                c_node = GraphNodeV2(
                    node_type=NodeTypeV2.CLASS,
                    qualified_id=class_id,
                    repository_id=self.repo_id,
                    generation_id=self.generation_id,
                    normalized_path=rel_path,
                    language="python",
                    properties={"class_name": node.name},
                    source_evidence={"line": node.lineno},
                )
                nodes.append(c_node)

                rels.append(
                    GraphRelationshipV2(
                        rel_type=RelationshipTypeV2.DECLARES,
                        source_id=module_id,
                        target_id=class_id,
                        provenance="ast_class_def",
                        extractor="PythonStaticExtractor",
                    )
                )

                # Inheritance
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        base_class_id = build_symbol_qualified_id(
                            self.repo_id, self.generation_id, "python", rel_path, "class", base.id
                        )
                        rels.append(
                            GraphRelationshipV2(
                                rel_type=RelationshipTypeV2.INHERITS,
                                source_id=class_id,
                                target_id=base_class_id,
                                provenance="ast_inheritance",
                                extractor="PythonStaticExtractor",
                                confidence="inferred",
                            )
                        )

                old_class = self_ast.current_class
                old_class_id = self_ast.current_class_id
                self_ast.current_class = node.name
                self_ast.current_class_id = class_id

                self_ast.generic_visit(node)

                self_ast.current_class = old_class
                self_ast.current_class_id = old_class_id

            def visit_FunctionDef(self_ast, node: ast.FunctionDef):
                self_ast._handle_func(node)

            def visit_AsyncFunctionDef(self_ast, node: ast.AsyncFunctionDef):
                self_ast._handle_func(node)

            def _handle_func(self_ast, node: ast.FunctionDef | ast.AsyncFunctionDef):
                is_method = bool(self_ast.current_class)
                symbol_kind = "method" if is_method else "function"
                node_type = NodeTypeV2.METHOD if is_method else NodeTypeV2.FUNCTION
                qual_name = f"{self_ast.current_class}.{node.name}" if is_method else node.name

                func_id = build_symbol_qualified_id(
                    self.repo_id, self.generation_id, "python", rel_path, symbol_kind, qual_name
                )
                f_node = GraphNodeV2(
                    node_type=node_type,
                    qualified_id=func_id,
                    repository_id=self.repo_id,
                    generation_id=self.generation_id,
                    normalized_path=rel_path,
                    language="python",
                    properties={"name": node.name, "qualified_name": qual_name},
                    source_evidence={"line": node.lineno},
                )
                nodes.append(f_node)

                parent_id = self_ast.current_class_id if is_method else module_id
                rels.append(
                    GraphRelationshipV2(
                        rel_type=RelationshipTypeV2.DECLARES,
                        source_id=parent_id,
                        target_id=func_id,
                        provenance="ast_func_def",
                        extractor="PythonStaticExtractor",
                    )
                )

                # Decorators check for FastAPI routes or Worker tasks
                for dec in node.decorator_list:
                    dec_str = ast.unparse(dec) if hasattr(ast, "unparse") else ""
                    if "router." in dec_str or "app." in dec_str or "get(" in dec_str or "post(" in dec_str:
                        # Extract route
                        endpoint_id = build_api_endpoint_qualified_id(
                            self.repo_id, self.generation_id, "POST" if "post" in dec_str else "GET", dec_str
                        )
                        ep_node = GraphNodeV2(
                            node_type=NodeTypeV2.API_ENDPOINT,
                            qualified_id=endpoint_id,
                            repository_id=self.repo_id,
                            generation_id=self.generation_id,
                            normalized_path=rel_path,
                            properties={"route_decorator": dec_str},
                        )
                        nodes.append(ep_node)
                        rels.append(
                            GraphRelationshipV2(
                                rel_type=RelationshipTypeV2.EXPOSES,
                                source_id=func_id,
                                target_id=endpoint_id,
                                provenance="fastapi_decorator",
                                extractor="PythonStaticExtractor",
                            )
                        )
                    elif "task" in dec_str or "worker" in dec_str:
                        worker_id = build_worker_task_qualified_id(self.repo_id, self.generation_id, qual_name)
                        wt_node = GraphNodeV2(
                            node_type=NodeTypeV2.WORKER_TASK,
                            qualified_id=worker_id,
                            repository_id=self.repo_id,
                            generation_id=self.generation_id,
                            normalized_path=rel_path,
                            properties={"task_name": qual_name},
                        )
                        nodes.append(wt_node)
                        rels.append(
                            GraphRelationshipV2(
                                rel_type=RelationshipTypeV2.EXPOSES,
                                source_id=func_id,
                                target_id=worker_id,
                                provenance="worker_task_decorator",
                                extractor="PythonStaticExtractor",
                            )
                        )

                self_ast.generic_visit(node)

        visitor = ASTVisitor()
        visitor.visit(tree)

        return nodes, rels


class ConfigExtractor:
    """Extracts Configuration nodes and relationships from YAML / JSON files."""

    def __init__(self, repo_id: str, generation_id: str, repo_root: Path):
        self.repo_id = repo_id
        self.generation_id = generation_id
        self.repo_root = repo_root.resolve()

    def extract_config(self, file_path: Path) -> Tuple[List[GraphNodeV2], List[GraphRelationshipV2]]:
        nodes: List[GraphNodeV2] = []
        rels: List[GraphRelationshipV2] = []

        rel_path = file_path.relative_to(self.repo_root).as_posix()
        config_id = build_config_qualified_id(self.repo_id, self.generation_id, rel_path)

        c_node = GraphNodeV2(
            node_type=NodeTypeV2.CONFIGURATION,
            qualified_id=config_id,
            repository_id=self.repo_id,
            generation_id=self.generation_id,
            normalized_path=rel_path,
            language="yaml" if file_path.suffix in {".yaml", ".yml"} else "json",
        )
        nodes.append(c_node)

        try:
            content = file_path.read_text(encoding="utf-8")
            if file_path.suffix in {".yaml", ".yml"}:
                parsed = yaml.safe_load(content)
            else:
                parsed = json.loads(content)

            if isinstance(parsed, dict) and "services" in parsed:
                # Docker Compose services configuration
                for service_name, service_def in parsed["services"].items():
                    svc_id = build_config_qualified_id(self.repo_id, self.generation_id, f"service:{service_name}")
                    svc_node = GraphNodeV2(
                        node_type=NodeTypeV2.CONFIGURATION,
                        qualified_id=svc_id,
                        repository_id=self.repo_id,
                        generation_id=self.generation_id,
                        normalized_path=rel_path,
                        properties={"service_name": service_name},
                    )
                    nodes.append(svc_node)
                    rels.append(
                        GraphRelationshipV2(
                            rel_type=RelationshipTypeV2.CONFIGURES,
                            source_id=config_id,
                            target_id=svc_id,
                            provenance="docker_compose_service",
                            extractor="ConfigExtractor",
                        )
                    )
        except Exception as e:
            c_node.properties["parse_error"] = str(e)

        return nodes, rels
