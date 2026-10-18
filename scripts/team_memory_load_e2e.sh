#!/usr/bin/env bash
# One-command run of the multi-user team memory load e2e (docs/runbooks/team-memory-load-e2e.md).
#
# Starts a throwaway PostgreSQL 16 (pgvector) on 127.0.0.1:5444 and Redis on 127.0.0.1:6390 with a
# generated password, runs scripts/team_memory_load_e2e.py with the given arguments, and removes both
# containers afterwards. The exit status is the run's.
#
#   scripts/team_memory_load_e2e.sh --users 12 --ops 50
#   DOCKER="wsl -d Ubuntu-24.04 docker" PYTHON=.venv/Scripts/python scripts/team_memory_load_e2e.sh --users 36
set -euo pipefail

DOCKER="${DOCKER:-docker}"
PYTHON="${PYTHON:-python}"
PG_NAME="${TEAM_MEMORY_E2E_PG_CONTAINER:-brain-memload-pg}"
REDIS_NAME="${TEAM_MEMORY_E2E_REDIS_CONTAINER:-brain-memload-redis}"
PG_PORT="${TEAM_MEMORY_E2E_PG_PORT:-5444}"
REDIS_PORT="${TEAM_MEMORY_E2E_REDIS_PORT:-6390}"
PASSWORD="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')"

cleanup() {
    $DOCKER rm -f "$PG_NAME" "$REDIS_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup
$DOCKER run -d --name "$PG_NAME" -e POSTGRES_PASSWORD="$PASSWORD" -p "127.0.0.1:${PG_PORT}:5432" \
    pgvector/pgvector:pg16 -c max_connections=300 >/dev/null
$DOCKER run -d --name "$REDIS_NAME" -p "127.0.0.1:${REDIS_PORT}:6379" redis:7-alpine >/dev/null

for _ in $(seq 1 60); do
    if $DOCKER exec "$PG_NAME" pg_isready -U postgres -q 2>/dev/null; then
        break
    fi
    sleep 1
done

status=0
"$PYTHON" scripts/team_memory_load_e2e.py \
    --database-url "postgresql://postgres:${PASSWORD}@127.0.0.1:${PG_PORT}/postgres" \
    --redis-url "redis://127.0.0.1:${REDIS_PORT}/0" \
    --docker-cmd "$DOCKER" --pg-container "$PG_NAME" "$@" || status=$?
exit "$status"
