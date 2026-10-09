from types import SimpleNamespace

from brain.context.runtime_context_builder import _referenced_constant_ranges


def test_referenced_top_level_constant_is_selected_but_local_assignment_is_not():
    chunks = [
        SimpleNamespace(start_line=1, end_line=3, content="FAILURES = {'error', 'timeout'}\nLOCAL = 1\n"),
        SimpleNamespace(start_line=78, end_line=92, content="def metrics(items):\n    INNER = 2\n    return sum(row[1] in FAILURES for row in items) + INNER\n"),
    ]
    assert _referenced_constant_ranges(chunks, {(78, 92)}) == {(1, 1)}


def test_huge_offset_is_bounded_and_syntax_errors_fail_closed():
    huge = SimpleNamespace(start_line=100_000, end_line=100_001, content="FAILURES = []")
    broken = SimpleNamespace(start_line=1, end_line=2, content="FAILURES = [\n")
    assert _referenced_constant_ranges([huge], {(100_000, 100_001)}) == set()
    assert _referenced_constant_ranges([broken], {(1, 2)}) == set()
