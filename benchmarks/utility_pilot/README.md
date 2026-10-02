# Project Brain v0.5.0 Utility Benchmark Pilot

## Overview
This package implements an empirical product evaluation framework for **Project Brain v0.5.0-rc1**.
The objective is to measure whether Project Brain's Knowledge Graph, Context Packs, Impact Engine, and Remediation services provide measurable utility to autonomous coding agents across real-world engineering tasks.

## Methodology & Experimental Controls
- **Frozen System Under Test**: Project Brain `v0.5.0-rc1` (commit `4c4d973` / `1bf3e0d`).
- **Paired Experimental Design**: 5 representative tasks executed in two modes:
  1. `Control`: Brain capabilities disabled (standard file inspection / search / tests).
  2. `Treatment`: Project Brain v0.5 capabilities available.
- **Randomization**: Deterministic seed (`seed=42`) used to randomize whether Control or Treatment executes first for each task.
- **Gold Standard Isolation**: Gold manifests and hidden acceptance tests stored isolated outside of agent workspaces.
- **Clean Workspace Environment**: Each run operates inside an isolated, disposable Git worktree created from the exact target commit, cleaned up after execution.

## Pilot Tasks
1. `task_01_bug_localization`: Cyrillic Token Slicing Boundary Fix
2. `task_02_multifile_change`: Coordinated Export Rate Limiter Feature
3. `task_03_arch_boundary`: Enforce Search Service Architecture Boundary (`DRIFT-001`)
4. `task_04_cross_repo_contract`: Portfolio API Contract Synchronization
5. `task_05_regression_remediation`: Remediation Strategy Cyclic Dependency Decoupling

## Evaluator Harness
- **Functional Evaluator**: Runs mandatory pytest suites.
- **Patch Evaluator**: Checks diff hygiene, secret leaks, and prohibited code modifications.
- **Architecture Evaluator**: Runs `ArchitecturalDriftAnalyzer` (`DRIFT-001` through `DRIFT-005`).
- **Impact Evaluator**: Computes precision and recall of files read vs required gold files.

## Reproduction
Run full benchmark execution:
```bash
py -3 -m benchmarks.utility_pilot.run_pilot
```

Run benchmark unit tests:
```bash
py -3 -m pytest tests/test_utility_benchmark_pilot.py -v
```
