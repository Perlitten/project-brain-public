#!/usr/bin/env bash
# Project Brain — one-command local bootstrap.
#
#   ./scripts/bootstrap.sh [--repo /path/to/your/repo]
#
# Does everything INSTALL.md describes, in order:
#   1. checks prerequisites (python3, docker)
#   2. creates .venv and installs the package
#   3. creates .env from .env.example (if missing)
#   4. starts postgres+redis+neo4j via docker compose
#   5. runs `brain doctor` preflight
#
# Afterwards:
#   .venv/bin/brain index --repo /path/to/your/repo
#   .venv/bin/uvicorn apps.api.main:app --port 8000
set -euo pipefail

REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

cd "$(dirname "$0")/.."
ROOT="$PWD"

step() { echo; echo "==> $1"; }

# 1. prerequisites
step "Checking prerequisites"
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
PYV=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  python $PYV"
command -v docker >/dev/null || { echo "docker not found — install Docker Engine first" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin not found" >&2; exit 1; }
echo "  $(docker compose version --short)"

# 2. venv + install
step "Setting up virtualenv"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"
echo "  package installed"

# 3. .env
step "Configuring .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "  created .env from .env.example (mock providers by default)"
  echo "  WARNING: change PROJECT_BRAIN_API_KEY before exposing port 8000"
else
  echo "  .env already exists, leaving it alone"
fi

# 4. services
step "Starting services (postgres, redis, neo4j)"
docker compose up -d postgres redis neo4j
echo "  waiting for healthy services..."
for i in $(seq 1 24); do
  UNHEALTHY=$(docker compose ps --format json 2>/dev/null \
    | python3 -c "import json,sys; print(sum(1 for l in sys.stdin if (json.loads(l).get('Health') or 'healthy') != 'healthy'))" 2>/dev/null || echo 1)
  if [[ "$UNHEALTHY" == "0" ]]; then break; fi
  sleep 5
done
docker compose ps --format "table {{.Name}}\t{{.Status}}"

# 5. preflight
step "Running preflight (brain doctor)"
.venv/bin/brain doctor || {
  echo "doctor reported FAILs — fix the lines above, then re-run ./scripts/bootstrap.sh"
  exit 1
}

echo
echo "Done. Next:"
echo "  .venv/bin/brain index --repo ${REPO:-/path/to/your/repo}"
echo "  .venv/bin/uvicorn apps.api.main:app --port 8000"
echo
echo "Then: curl -H \"X-API-Key: \$(grep PROJECT_BRAIN_API_KEY .env | cut -d= -f2)\" \\"
echo "  -H 'Content-Type: application/json' \\"
echo "  -d '{\"repo_path\": \"${REPO:-/path/to/your/repo}\", \"task_description\": \"add oauth login\", \"persist\": true}' \\"
echo "  http://127.0.0.1:8000/context"
