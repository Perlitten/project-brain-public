# Historical research and release records

These records preserve design context and reproducibility. They are not the
recommended installation path or evidence of general coding improvements.
Start with [Getting started](../GETTING_STARTED.md) for the current product.

| Retained material | Why it remains |
|---|---|
| [Historical evaluation catalog](../../eval/historical/README.md) | Frozen pre-web fixtures and explicitly synthetic ranking experiments |
| [Version-specific script catalog](../../scripts/HISTORICAL.md) | Older end-to-end replay entry points and their retained contracts |
| [v0.9.0 release records](../release/0.9.0/) | Original installation, support and rollback evidence for that tag |
| [v0.5 engineering change laboratory](../v0.5.0-engineering-change-laboratory.md) | Earlier design of still-maintained API/CLI behavior |
| [Design records](../design/) | Design rationale; implementation and current configuration may have evolved |
| [Architecture decisions](../adr/) | Decisions with explicit status, including superseded proposals |

[Utility pilot](../../benchmarks/utility_pilot/README.md) remains a maintained
protocol/framework for measuring downstream usefulness. Its fixtures and
baseline results are retained; they do not establish a coding advantage.
Synthetic ranking proxies in the historical catalog likewise are not real
model-provider or agent coding benchmarks.

Do not remove old evidence merely because it is not a runtime dependency.
Before moving or retiring a workflow, check current imports, tests, CI,
packaging, CLI/API compatibility and historical reproducibility.

Return to [current documentation](../README.md).
