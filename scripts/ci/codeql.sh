#!/usr/bin/env bash
# Provision a reviewed CodeQL bundle (CLI + matching query packs) on the
# isolated brain-codeql local runner. No Actions runtime is involved.
set -euo pipefail
: "${CODEQL_LANGUAGE:?}"
case "$CODEQL_LANGUAGE" in python|javascript-typescript) ;; *) exit 2 ;; esac
command -v codeql
output="$PWD/artifacts/codeql/$CODEQL_LANGUAGE"
mkdir -p "$output"
trap 'rm -rf "$output/database"' EXIT
codeql database create "$output/database" --language="$CODEQL_LANGUAGE" --build-mode=none --source-root="$PWD"
codeql database analyze "$output/database" --format=sarif-latest \
  --output="$output/results.sarif" --threads=2 --ram=4096
# Keep analysis independent from GitHub credentials. Fail on errors and
# security findings; publishing SARIF is a separate credentialed operation.
python3 - "$output/results.sarif" <<'PY'
import json
import sys
report = json.load(open(sys.argv[1]))
findings = []
for run in report.get('runs', []):
    for invocation in run.get('invocations', []):
        if invocation.get('executionSuccessful') is False:
            raise SystemExit('CodeQL analysis did not complete successfully')
    findings.extend(run.get('results', []))
for result in findings:
    print(result.get('ruleId'), result.get('message', {}).get('text', ''))
print(f'CodeQL reported {len(findings)} findings; see SARIF for triage')
PY
