"""Ordered graph writes, partial failures and cancellation captured from v0.9.2."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

import brain.indexers.file_indexer as module
from tests.graph_enrichment_fixture import RecordingGraph, normalized_calls, prepare_repository


CASES = json.loads((Path(__file__).parent / "fixtures" / "graph_enrichment_contract.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
async def test_graph_enrichment_preserves_order_and_failure_boundaries(case, tmp_path, monkeypatch):
    scanned = prepare_repository(tmp_path, case.get("fallback", False))
    indexer = module.FileIndexer()
    graph = RecordingGraph(case.get("failure"), case.get("cancel", False))
    indexer.graph_client = graph
    indexer.progress = {"graph": {"discovered": 0, "processed": 0, "failed": 0}}
    failures = []
    published = []

    def failure(phase, exc, path="", chunk_index=None):
        failures.append({"phase": phase, "error": type(exc).__name__, "message": str(exc),
                         "path": path, "chunk_index": chunk_index})

    async def publish():
        published.append(deepcopy(indexer.progress))

    original_reader = module._read_text_file

    def read_file(path, errors="ignore"):
        if case.get("read_failure") and path.relative_to(tmp_path).as_posix() == "brain/engine.py":
            raise OSError("read rejected")
        return original_reader(path, errors)

    monkeypatch.setattr(module, "_read_text_file", read_file)
    monkeypatch.setattr(indexer, "_failure", failure)
    monkeypatch.setattr(indexer, "_publish_progress", publish)
    error = None
    try:
        await indexer._post_process_graph("fixture", tmp_path, scanned)
    except asyncio.CancelledError:
        error = "CancelledError"

    assert {"calls": normalized_calls(graph.calls, tmp_path), "progress": indexer.progress,
            "failures": failures, "published": published, "error": error} == case["expected"]
