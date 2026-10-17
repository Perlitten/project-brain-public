#!/usr/bin/env bash
# Run the team-mode end-to-end check (scripts/team_e2e_check.py) in throwaway containers.
#
#   scripts/team_e2e_docker.sh [--keep-image] [--pg-client MAJOR]
#
# Builds a runner image from the current working tree plus `git archive c13d056`,
# starts PostgreSQL 15 (pgvector) and Redis under compose project brain-e2e,
# runs the check, copies the report to artifacts/team-e2e/ and removes the stack
# and its volumes. Exit status is the check's. Needs docker with compose v2 and
# git; no Python, psql or pg_dump on the host. See docs/runbooks/team-e2e-check.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEGACY_COMMIT="${TEAM_E2E_LEGACY_COMMIT:-c13d056}"
KEEP_IMAGE=0
export TEAM_E2E_PG_CLIENT_MAJOR="${TEAM_E2E_PG_CLIENT_MAJOR:-15}"
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-image) KEEP_IMAGE=1 ;;
    --pg-client) shift; TEAM_E2E_PG_CLIENT_MAJOR="$1" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# Docker for Windows (Git Bash) needs native paths; elsewhere this is the identity.
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

COMPOSE=(docker compose -f "$(host_path "$ROOT/deploy/team-e2e/docker-compose.yml")" -p brain-e2e)
STAGING="$(mktemp -d)"
export TEAM_E2E_STAGING="$(host_path "$STAGING")"
export TEAM_E2E_PG_PASSWORD="e2e$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
OUT="$ROOT/artifacts/team-e2e"

cleanup() {
  docker rm -f brain-e2e-runner >/dev/null 2>&1 || true
  "${COMPOSE[@]}" --profile runner down -v --remove-orphans >/dev/null 2>&1 || true
  if [ "$KEEP_IMAGE" -eq 0 ]; then docker image rm brain-e2e-runner:local >/dev/null 2>&1 || true; fi
  rm -rf "$STAGING"
}
trap cleanup EXIT

echo "staging main tree and ${LEGACY_COMMIT} ..."
mkdir -p "$STAGING/main" "$STAGING/legacy" "$OUT"
# Tracked and untracked-but-not-ignored files: .env, .mcp.json and other ignored secrets never leave the host.
(cd "$ROOT" && git ls-files -co --exclude-standard -z | tar --null -T - -cf -) | tar -x -C "$STAGING/main"
git -C "$ROOT" archive "$LEGACY_COMMIT" | tar -x -C "$STAGING/legacy"

"${COMPOSE[@]}" up -d --wait pg redis
"${COMPOSE[@]}" --profile runner build runner
set +e
"${COMPOSE[@]}" --profile runner run --name brain-e2e-runner runner
STATUS=$?
set -e
rm -rf "$OUT/latest"
MSYS_NO_PATHCONV=1 docker cp brain-e2e-runner:/work/out "$(host_path "$OUT/latest")" >/dev/null 2>&1 || echo "no report to copy" >&2
echo "report: $OUT/latest/report.json (exit $STATUS)"
exit "$STATUS"
