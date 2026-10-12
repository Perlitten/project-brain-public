#!/usr/bin/env bash
# Project Brain — start the API, the background worker and the web dashboard
# in one terminal. Run ./scripts/bootstrap.sh once first.
#
#   ./scripts/start.sh            # api :8000, worker, web :3100
#   ./scripts/start.sh --no-web   # api + worker only
#
# Ctrl+C stops all three. The datastores (postgres, redis, neo4j) keep running
# in Docker; `docker compose stop` stops them.
set -euo pipefail

WEB=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-web) WEB=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

cd "$(dirname "$0")/.."
[[ -x .venv/bin/python && -f .env ]] || { echo "run ./scripts/bootstrap.sh first" >&2; exit 1; }
if [[ "$WEB" == 1 ]] && ! command -v npm >/dev/null; then
  echo "npm not found — skipping the web dashboard (install Node.js 22, or pass --no-web)" >&2
  WEB=0
fi

docker compose up -d postgres redis neo4j >/dev/null

PIDS=()
cleanup() { trap - EXIT INT TERM; kill "${PIDS[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

.venv/bin/uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 &
PIDS+=($!)
.venv/bin/python -m brain.workers.worker &
PIDS+=($!)

if [[ "$WEB" == 1 ]]; then
  [[ -d apps/web/node_modules ]] || (cd apps/web && npm ci --no-audit --no-fund)
  API_KEY=$(grep '^PROJECT_BRAIN_API_KEY=' .env | cut -d= -f2-)
  (cd apps/web && BRAIN_API_URL=http://127.0.0.1:8000 BRAIN_API_KEY="$API_KEY" exec npm run dev) &
  PIDS+=($!)
fi

echo
echo "API     http://127.0.0.1:8000/health"
echo "Worker  running (indexing and other background jobs, plus the scheduler)"
if [[ "$WEB" == 1 ]]; then echo "Web     http://127.0.0.1:3100"; fi
echo "Ctrl+C stops all of them."
# Stop everything as soon as any one of them exits.
wait -n "${PIDS[@]}"
