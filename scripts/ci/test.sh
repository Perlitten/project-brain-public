#!/usr/bin/env bash
set -euo pipefail
python scripts/ci/wait_services.py

# Install dependencies (hash-locked)


# Lint with ruff
ruff check .

# Verify legal inventory against pinned dependencies
python scripts/check_third_party_notices.py

# Type check ratchet (mypy, non-increasing baseline)
bash scripts/mypy_gate.sh

# Migration check (fresh init + idempotent re-apply)
python scripts/check_migrations.py

# Run tests (with coverage)
pytest -q --cov=brain --cov=apps --cov-report=term-missing --cov-report=xml:coverage.xml

# Team authority PostgreSQL race, native MCP and restore checks
PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE USER fixture WITH CREATEDB PASSWORD 'fixture_local_only'" \
  -c "CREATE DATABASE brain_team_test OWNER fixture"
TEST_TEAM_POSTGRES_URL="postgresql+asyncpg://fixture:fixture_local_only@${POSTGRES_HOST}:$POSTGRES_PORT/brain_team_test" \
  pytest tests/test_team_foundations.py tests/test_team_transport.py tests/test_team_restore.py \
  tests/test_team_invitations.py tests/test_team_restore_fence.py tests/test_team_execution.py \
  tests/test_team_lock_order.py tests/test_team_execution_llm_artifacts.py tests/test_memory_cas_concurrency.py -q
# Keep the untrusted vector extension out of the restricted-role restore fixture's dump.
# Only the following memory-lock regressions require it.
PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d brain_team_test -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector"
TEST_TEAM_POSTGRES_URL="postgresql+asyncpg://fixture:fixture_local_only@${POSTGRES_HOST}:$POSTGRES_PORT/brain_team_test" \
  pytest tests/test_team_locking_postgres.py -q


# Build wheel + clean-install smoke
python -m pip install build
python -m build --wheel --sdist --outdir dist/
python scripts/check_third_party_notices.py dist/*.whl dist/*.tar.gz
SMOKE_VENV=$(mktemp -d)
trap 'rm -rf "$SMOKE_VENV"' EXIT
python -m venv "$SMOKE_VENV"
"$SMOKE_VENV"/bin/pip install --require-hashes -r requirements.lock
"$SMOKE_VENV"/bin/pip install --no-deps dist/*.whl
"$SMOKE_VENV"/bin/brain --help >/dev/null
echo "wheel installs and 'brain --help' entrypoint works"

