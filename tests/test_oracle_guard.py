"""Solution Leakage Guard Test for Oracle Evidence Packs.

Verifies that Oracle evidence packs contain ZERO implementation code, patches,
exact replacement strings, or gold diffs.
"""

from benchmarks.utility_pilot.oracle.oracle_packs import ORACLE_PACKS


def test_oracle_packs_no_solution_leakage():
    prohibited_keywords = [
        "diff --git",
        "def truncate_utf8",
        "encoded[-1] & 0xC0",
        "encoded.decode",
        "MAX_EXPORTS_PER_MINUTE =",
        "+class",
        "+def",
    ]

    for task_id, pack in ORACLE_PACKS.items():
        pack_str = str(pack.to_dict()).lower()
        for keyword in prohibited_keywords:
            assert keyword.lower() not in pack_str, f"Solution keyword '{keyword}' leaked in Oracle pack for {task_id}"
