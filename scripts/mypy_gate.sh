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
python -m mypy --explicit-package-bases brain apps scripts > /tmp/mypy_gate.out 2>&1 || true
cat /tmp/mypy_gate.out
COUNT="$(grep -c 'error:' /tmp/mypy_gate.out || true)"

echo "mypy errors: ${COUNT} (baseline ${BASELINE})"
if [ "${COUNT}" -gt "${BASELINE}" ]; then
    echo "mypy regression: new type errors were introduced — fix them or re-baseline deliberately"
    exit 1
fi
