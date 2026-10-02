"""Static Benchmark Leak Guard Test for Project Brain v0.5.2.

Verifies zero benchmark task IDs, gold file lists, or task prompts are hard-coded in product code.
"""

from pathlib import Path


def test_no_benchmark_gold_leakage_in_product_code():
    repo_root = Path(__file__).resolve().parent.parent
    brain_dir = repo_root / "brain"

    prohibited_strings = [
        "task_01_bug_localization",
        "task_02_multifile_change",
        "task_03_arch_boundary",
        "task_04_cross_repo_contract",
        "task_05_regression_remediation",
        "Cyrillic Token Slicing Boundary Fix",
        "Coordinated Export Rate Limiter Feature",
    ]

    for py_file in brain_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        for ps in prohibited_strings:
            assert ps not in content, f"Benchmark leak detected: '{ps}' found in product file {py_file}"
