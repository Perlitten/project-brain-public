"""Unit & integration tests for Graphify v2 Foundation (Workstream A)."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.graph.builder_v2 import GraphBuilderV2
from brain.graph.extractors_v2 import PythonStaticExtractor
from brain.graph.generation_manager import GraphGenerationManager
from brain.graph.identity import build_symbol_qualified_id
from brain.graph.schema_v2 import GraphNodeV2, NodeTypeV2


def test_graph_v2_schema_and_identity():
    node = GraphNodeV2(
        node_type=NodeTypeV2.CLASS,
        qualified_id="sym:repo:gen:python:app.py:class:MyClass",
        repository_id="repo",
        generation_id="gen",
        normalized_path="app.py",
    )
    d = node.to_dict()
    assert d["node_type"] == "Class"
    assert d["qualified_id"] == "sym:repo:gen:python:app.py:class:MyClass"

    sym_id = build_symbol_qualified_id("myrepo", "gen1", "python", "src/foo.py", "func", "bar")
    assert sym_id == "sym:myrepo:gen1:python:src/foo.py:func:bar"


def test_python_static_extractor(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "class Base:\n"
        "    pass\n\n"
        "class Derived(Base):\n"
        "    def method_a(self):\n"
        "        return 42\n",
        encoding="utf-8",
    )

    extractor = PythonStaticExtractor("repo", "gen", tmp_path)
    nodes, rels = extractor.extract_file(f)

    assert len(nodes) >= 4  # File, Module, Class Base, Class Derived, Method method_a
    class_names = [n.properties.get("class_name") for n in nodes if n.node_type == NodeTypeV2.CLASS]
    assert "Base" in class_names
    assert "Derived" in class_names


def test_graph_builder_and_quality_gate(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("def hello(): return 'world'\n", encoding="utf-8")

    builder = GraphBuilderV2(tmp_path)
    meta, report = builder.build_generation("HEAD")

    assert meta.status == "ready"
    assert meta.node_count > 0
    assert report.passed is True

    mgr = GraphGenerationManager(tmp_path / ".brain")
    assert mgr.activate_generation(meta.generation_id) is True
    assert mgr.get_active_generation_id() == meta.generation_id


def test_graph_v2_api_endpoints(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.graph_v2.resolve_repo_path", return_value=tmp_path):
            # 1. Build
            r1 = client.post(f"/insights/graph/build?repo_path={tmp_path}&activate=true")
            assert r1.status_code == 200
            data1 = r1.json()
            assert data1["status"] == "success"
            gen_id = data1["generation"]["generation_id"]

            # 2. List
            r2 = client.get(f"/insights/graph/generations?repo_path={tmp_path}")
            assert r2.status_code == 200
            assert r2.json()["active_generation_id"] == gen_id

            # 3. Activate
            r3 = client.post(f"/insights/graph/activate/{gen_id}?repo_path={tmp_path}")
            assert r3.status_code == 200
    finally:
        app.dependency_overrides.clear()
