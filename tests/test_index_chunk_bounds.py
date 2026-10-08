"""Payload bounds must preserve source, coordinates and ordinary windows."""
import pytest

from brain.indexers.file_indexer import MAX_CHUNK_CHARS, chunk_file_content


@pytest.mark.parametrize("unit", ["x", "Ж", "😀"])
def test_single_long_line_is_preserved_without_oversized_chunks(unit):
    content = unit * (MAX_CHUNK_CHARS * 2 + 37)
    chunks = chunk_file_content(content, [])
    assert "".join(chunk["content"] for chunk in chunks) == content
    assert len(chunks) == 3
    assert all(len(chunk["content"]) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert [(c["start_line"], c["end_line"]) for c in chunks] == [(1, 1)] * 3
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2]


def test_long_symbol_segment_preserves_line_mapping_and_all_content():
    lines = ["def large():", "a" * 40_000, "b" * 40_000, "return 1"]
    text = "\n".join(lines)
    chunks = chunk_file_content(text, [{"start_line": 1, "end_line": 4}])
    assert "".join(c["content"] for c in chunks) == text
    assert all(len(c["content"]) <= MAX_CHUNK_CHARS for c in chunks)
    assert [(c["start_line"], c["end_line"]) for c in chunks] == [(1, 2), (3, 4)]


def test_ordinary_overlapping_windows_keep_their_exact_boundaries():
    lines = [f"line {i}" for i in range(1, 7)]
    chunks = chunk_file_content("\n".join(lines), [], max_chunk_lines=4, chunk_overlap_lines=1)
    assert chunks == [
        {"chunk_index": 0, "content": "\n".join(lines[:4]), "start_line": 1, "end_line": 4},
        {"chunk_index": 1, "content": "\n".join(lines[3:]), "start_line": 4, "end_line": 6},
    ]


def test_ordinary_window_preserves_trailing_blank_source_line():
    chunks = chunk_file_content("first\n\n", [])
    assert chunks == [{"chunk_index": 0, "content": "first\n", "start_line": 1, "end_line": 2}]
