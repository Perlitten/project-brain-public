# Historical release tooling

These scripts reproduce version-specific release demonstrations. They are
retained at their original paths to preserve imports, recorded generation
commands, and historical evidence. They are **not** the current installation
or release procedure, and their original assumptions may require the matching
source revision and environment.

| Version | Preserved scripts | Reason to retain |
| --- | --- | --- |
| 0.3.0 | `e2e_v030_release_demo.py`, `v030_contract_smoke.py`, `generate_v030_release_manifest.py`, `generate_v030_drift_reports.py`, `generate_v030_change_guard_reports.py` | Architecture/change-guard demonstrations and evidence generation; the manifest imports the demo and smoke scripts. |
| 0.4.0 | `e2e_v040_release_demo.py`, `generate_v040_graph_migration_report.py`, `v040_release_candidate_builder.py` | Repository-scoped graph migration and release evidence; preserve graph compatibility examples. |
| 0.5.0 | `v050_engineering_lab_verification.py`, `v050_feature_inventory.py`, `v050_release_candidate.py` | Laboratory, capability boundary and release evidence; the release candidate imports the verification helper. |

Current CI does not run these version-specific entry points. Their Python code
remains covered by repository-wide Ruff and mypy gates. Current version-named
tests such as `tests/test_v050_api_contracts.py` and
`tests/test_v050_cli_contracts.py` protect APIs and CLI commands that still
exist; they are active regression coverage and must not be removed based on
their filenames.

For present-day local quality checks use [`verify_release.sh`](verify_release.sh).
Publication uses [the release workflow](../.github/workflows/release.yml).
Use the [current getting-started documentation](../README.md) for installation.
