"""Complete artifact fixtures captured from v0.9.2's original builder."""

import json
from pathlib import Path

import pytest

from brain.context.rendering import PackDocument, render_document, render_evidence
from brain.database.models import Decision, File, Learning, Rule, Symbol


CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "context_render_contract.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_complete_markdown_matches_original_builder(case):
    data = case["input"]
    files = [(File(**item["record"]), item["trace"]) for item in data["files"]]
    symbols = [(Symbol(**item["record"]), item["trace"]) for item in data["symbols"]]
    evidence = render_evidence(files, symbols, data["repo_posix"])
    document = PackDocument(
        task_description=data["task_description"], task_type=data["task_type"],
        features=data["features"], budget=data["budget"], evidence=evidence,
        graph_md=data["graph_md"], rules=[Rule(**row) for row in data["rules"]],
        decisions=[Decision(**row) for row in data["decisions"]],
        learnings=[Learning(**row) for row in data["learnings"]],
        risks=data["risks"], checklist=data["checklist"], plan_content=data["plan_content"],
        critic_status=data["critic_res"]["status"], critic_warnings=data["critic_res"]["warnings"],
    )

    assert render_document(document) == case["expected"]


def test_malformed_file_trace_preserves_formatting_error():
    with pytest.raises(TypeError):
        render_evidence([(File(path="source.py"), {"vector_similarity": None})], [], "/synthetic/repo")


def test_missing_symbol_trace_preserves_key_error():
    with pytest.raises(KeyError, match="final_score"):
        render_evidence([], [(Symbol(name="entry", kind="function"), {})], "/synthetic/repo")
