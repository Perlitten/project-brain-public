#!/usr/bin/env bash
# verify_release.sh — reproducible local verification for a Brain release
# candidate. Mirrors .github/workflows/ci.yml; GitHub Actions is disabled
# repo-wide, so THIS is the authoritative gate set. Every gate prints
# PASS/FAIL/SKIP; service-dependent gates SKIP when the required service is
# unreachable instead of pretending to pass.
#
# Usage:
#   bash scripts/verify_release.sh              # all gates; services auto-detected
#   VERIFY_SKIP_SLOW=1 bash scripts/verify_release.sh   # skip pytest + wheel build
#
# Prerequisites: .venv with pip install -e ".[dev]" (or set PYTHON to a
# suitable interpreter). Services for the full run:
#   docker compose up -d postgres redis neo4j
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1 — $2"; }

check() { # name, command
  echo "--- $1"
  if eval "$2"; then ok "$1"; else bad "$1"; fi
}

service_up() { # host port
  "$PY" - "$1" "$2" <<'EOF' >/dev/null 2>&1
import socket, sys
sys.exit(0 if socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2) else 1)
EOF
}

if [ ! -x "$PY" ]; then
  echo "FAIL: interpreter $PY missing — run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
  exit 1
fi

check "ruff"          ".venv/bin/ruff check ."
check "mypy ratchet"  "bash scripts/mypy_gate.sh"
check "design-system guard" "$PY scripts/design_system_guard.py --root ."
check "deploy identity scan" "bash deploy/doctor.sh stale-scan"

if service_up localhost "${POSTGRES_PORT:-5433}"; then
  check "migrations (fresh + idempotent)" "$PY scripts/check_migrations.py"
else
  skip "migrations (fresh + idempotent)" "postgres not reachable on :${POSTGRES_PORT:-5433} (docker compose up -d postgres)"
fi

if [ "${VERIFY_SKIP_SLOW:-0}" = "1" ]; then
  skip "pytest" "VERIFY_SKIP_SLOW=1"
  skip "wheel clean-install" "VERIFY_SKIP_SLOW=1"
else
  check "pytest" "OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 .venv/bin/pytest tests/ -x -q"
  echo "--- wheel build + clean install"
  rm -rf /tmp/brain-verify-dist /tmp/brain-verify-venv
  if "$PY" -m build --wheel --outdir /tmp/brain-verify-dist >/dev/null 2>&1 \
     && python3 -m venv /tmp/brain-verify-venv \
     && /tmp/brain-verify-venv/bin/pip install -q /tmp/brain-verify-dist/*.whl \
     && /tmp/brain-verify-venv/bin/brain --help >/dev/null 2>&1 \
     && /tmp/brain-verify-venv/bin/python -c "import apps.api.main" 2>/dev/null; then
    ok "wheel clean-install"
  else
    bad "wheel clean-install"
  fi
fi

echo "======================================"
echo "gates: $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -eq 0 ]
