#!/usr/bin/env bash
# Ratchet gate: mypy must not get WORSE than the recorded baseline.
#
# The codebase carries pre-existing type debt (see baseline number in
# scripts/mypy_baseline.txt — count of `error:` lines from the same mypy
# invocation). Zeroing it is tracked as its own backlog item; until then this
# gate makes the debt monotonically non-increasing: every new error fails CI.
# To lower the baseline, fix errors and regenerate with:
#   python -m mypy --explicit-package-bases brain apps scripts | grep -c "error:"
set -euo pipefail

BASELINE="$(tr -d '[:space:]' < scripts/mypy_baseline.txt)"
gate_output="$(mktemp "${TMPDIR:-/tmp}/brain-mypy-gate.XXXXXX")"
trap 'rm -f "$gate_output"' EXIT
status=0
"${MYPY_PYTHON:-python}" -m mypy --explicit-package-bases brain apps scripts > "$gate_output" 2>&1 || status=$?
cat "$gate_output"
COUNT="$(grep -c 'error:' "$gate_output" || true)"
if [ "$status" -ne 0 ] && { [ "$status" -ne 1 ] || [ "$COUNT" -eq 0 ]; }; then
    echo "mypy failed to complete (exit $status); this is not a passing type gate"
    exit 1
fi

echo "mypy errors: ${COUNT} (baseline ${BASELINE})"
if [ "${COUNT}" -gt "${BASELINE}" ]; then
    echo "mypy regression: new type errors were introduced — fix them or re-baseline deliberately"
    exit 1
fi
