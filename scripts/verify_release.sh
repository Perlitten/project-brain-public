#!/usr/bin/env bash
# Local command gates from ci.yml, with explicit incomplete-run status.
# Install hash-locked dev requirements, this package (--no-deps -e .), and
# build in a Python 3.12 venv first. Start postgres/redis/neo4j via Compose.
# Usage: bash scripts/verify_release.sh
# PYTHON may name a different prepared interpreter. VERIFY_SKIP_SLOW=1 skips
# pytest, Next.js build and wheel checks; skips exit 2 unless
# VERIFY_ALLOW_SKIPS=1 is explicitly set. No Actions run is implied.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "FAIL: interpreter $PY missing; prepare the dev venv first"
  exit 1
fi
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
export PATH="$(dirname "$PY"):$PATH"
export MYPY_PYTHON="$PY"
PASS=0; FAIL=0; SKIP=0
verify_tmp="$(mktemp -d "${TMPDIR:-/tmp}/brain-release-verify.XXXXXX")" || exit 1
cleanup() {
  rm -rf "$verify_tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1 — $2"; }
check() {
  local name="$1"
  shift
  echo "--- $name"
  if "$@"; then ok "$name"; else bad "$name"; fi
}

service_up() {
  "$PY" - "$1" <<'PY'
import socket, sys
from urllib.parse import urlsplit
from brain.config.settings import settings
targets = [(settings.POSTGRES_HOST, settings.POSTGRES_PORT)]
if sys.argv[1] == "all":
    neo4j = urlsplit(settings.NEO4J_URI)
    targets += [(settings.REDIS_HOST, settings.REDIS_PORT),
                (neo4j.hostname or "localhost", neo4j.port or 7687)]
for host, port in targets:
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            pass
    except OSError:
        sys.exit(3)
PY
}

mypy_gate() {
  "$PY" -m mypy --version && bash scripts/mypy_gate.sh
}

web_checks() {
  (cd apps/web && npm ci && npm test && npm run typecheck && npm run build)
}

wheel_smoke() {
  "$PY" -m build --wheel --outdir "$verify_tmp/dist" &&
  "$PY" -m venv "$verify_tmp/venv" &&
  "$verify_tmp/venv/bin/python" -m pip install --require-hashes -r requirements.lock &&
  "$verify_tmp/venv/bin/python" -m pip install --no-deps "$verify_tmp"/dist/*.whl &&
  (cd "$verify_tmp" && "$verify_tmp/venv/bin/brain" --help >/dev/null &&
   "$verify_tmp/venv/bin/python" -c 'import apps.api.main')
}

check "ruff" "$PY" -m ruff check brain apps tests
check "mypy ratchet" mypy_gate
check "deploy identity scan" bash deploy/doctor.sh stale-scan

service_up postgres
service_status=$?
if [ "$service_status" -eq 0 ]; then
  check "migrations (fresh + idempotent)" "$PY" scripts/check_migrations.py
elif [ "$service_status" -eq 3 ]; then
  skip "migrations (fresh + idempotent)" "configured postgres is unreachable"
else
  bad "migrations preflight (settings/interpreter)"
fi

if [ "${VERIFY_SKIP_SLOW:-0}" = "1" ]; then
  skip "pytest" "VERIFY_SKIP_SLOW=1"
  skip "Next.js tests, typecheck and build" "VERIFY_SKIP_SLOW=1"
  skip "wheel clean-install" "VERIFY_SKIP_SLOW=1"
else
  service_up all
  service_status=$?
  if [ "$service_status" -eq 0 ]; then
    check "pytest" env OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 "$PY" -m pytest -q
  elif [ "$service_status" -eq 3 ]; then
    skip "pytest" "configured datastores are unreachable"
  else
    bad "pytest preflight (settings/interpreter)"
  fi
  check "wheel clean-install" wheel_smoke
  check "Next.js tests, typecheck and build" web_checks
fi

echo "gates: $PASS passed, $FAIL failed, $SKIP skipped"
if [ "$FAIL" -ne 0 ]; then exit 1; fi
if [ "$SKIP" -ne 0 ]; then
  echo "INCOMPLETE: release verification has skipped gates"
  if [ "${VERIFY_ALLOW_SKIPS:-0}" != "1" ]; then exit 2; fi
fi
