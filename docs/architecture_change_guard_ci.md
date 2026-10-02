# Architectural Change Guard — CI & PR Integration Guide

`Architectural Change Guard` is a diff-aware policy enforcement layer for Project Brain (v0.3.0 Milestone).
It evaluates Git change-sets against architectural rules, baseline states, policies, and waivers before pull requests are merged.

## Local CLI Usage

Run change guard evaluation locally:

```bash
# Text format output
python3 -m brain.insights.drift_cli check --repo . --base main --candidate HEAD --format text

# JSON output for CI artifacts
python3 -m brain.insights.drift_cli check --repo . --base main --candidate HEAD --format json --output change-guard-report.json

# SARIF 2.1.0 output for GitHub Security Code Scanning
python3 -m brain.insights.drift_cli check --repo . --base main --candidate HEAD --format sarif --output change-guard.sarif
```

## GitHub Actions Workflow Example

Add `.github/workflows/architecture-guard.yml`:

```yaml
name: Architectural Change Guard

on:
  pull_request:
    branches: [ main, master ]

jobs:
  architecture-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install Dependencies
        run: |
          pip install -e .

      - name: Run Architectural Change Guard
        run: |
          python3 -m brain.insights.drift_cli check \
            --repo . \
            --base "${{ github.event.pull_request.base.sha }}" \
            --candidate "${{ github.sha }}" \
            --format sarif \
            --output architecture-drift.sarif

      - name: Upload SARIF report
        uses: github/codeql-action/upload-sarif@v2
        if: always()
        with:
          sarif_file: architecture-drift.sarif
```

## Exit Codes Reference
* `0`: Success (Policy passed or enforcement mode is `report` / `warn`).
* `2`: Architecture policy violation (Unwaived critical violation or threshold exceeded).
* `3`: Invalid configuration (Malformed policy YAML or waivers file).
* `4`: Repository / revision error (Invalid Git revision or path error).
* `5`: Internal scan failure.
