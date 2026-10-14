#!/usr/bin/env bash
# Project Brain — one-command local bootstrap.
#
#   ./scripts/bootstrap.sh [--repo /path/to/your/repo]
#
# --repo sets TARGET_REPO_PATH, the repository Brain reads. Without it Brain
# uses its own source as a sample repository.
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
#   ./scripts/start.sh        # api + worker + web dashboard
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
if [[ -n "$REPO" ]]; then
  [[ -d "$REPO" ]] || { echo "--repo: $REPO is not a directory" >&2; exit 1; }
  REPO="$(cd "$REPO" && pwd)"
fi

step() { echo; echo "==> $1"; }

# 1. prerequisites
step "Checking prerequisites"
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
PYV=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  python $PYV"
python3 -c 'import sys; raise SystemExit(not ((3, 12) <= sys.version_info[:2] < (3, 13)))' || {
  echo "Python 3.12 is required by the reviewed dependency locks; found $PYV." >&2
  exit 1
}
command -v docker >/dev/null || { echo "docker not found — install Docker Engine first" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin not found" >&2; exit 1; }
echo "  $(docker compose version --short)"

# 2. venv + install
step "Setting up virtualenv"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -c 'import sys; raise SystemExit(not ((3, 12) <= sys.version_info[:2] < (3, 13)))' || {
  echo "Existing .venv is not Python 3.12; migrate it before retrying (no environment removed)." >&2
  exit 1
}
.venv/bin/pip install -q --require-hashes -r requirements-dev.lock
.venv/bin/pip install -q --no-deps -e .
echo "  package installed"

# 3. .env
step "Configuring .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  # Replace the published placeholders with fresh random secrets.
  .venv/bin/python - <<'EOF'
import pathlib, secrets
env = pathlib.Path(".env")
text = env.read_text(encoding="utf-8")
text = text.replace(
    "PROJECT_BRAIN_API_KEY=change-me-in-production",
    f"PROJECT_BRAIN_API_KEY={secrets.token_hex(32)}",
)
text = text.replace(
    "PROJECT_BRAIN_WEBHOOK_TOKEN=change-me-to-a-random-64-character-token",
    f"PROJECT_BRAIN_WEBHOOK_TOKEN={secrets.token_hex(32)}",
)
env.write_text(text, encoding="utf-8")
EOF
  echo "  created .env from .env.example (mock providers, random API key)"
else
  echo "  .env already exists, leaving it alone"
fi
if [[ -n "$REPO" ]]; then
  .venv/bin/python - "$REPO" <<'EOF'
import pathlib, re, sys
env = pathlib.Path(".env")
text = env.read_text(encoding="utf-8")
line = f"TARGET_REPO_PATH={sys.argv[1]}"
text, n = re.subn(r"(?m)^TARGET_REPO_PATH=.*$", lambda _: line, text)
env.write_text(text if n else text.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
EOF
  echo "  TARGET_REPO_PATH=$REPO"
else
  echo "  no --repo given: Brain will read its own source as a sample repository."
  echo "  Re-run with --repo /path/to/your/repo to point it at yours."
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

INDEX_REPO="${REPO:-$ROOT}"
echo
echo "Done. Next:"
echo "  1. .venv/bin/brain index --repo $INDEX_REPO"
echo "  2. ./scripts/start.sh"
echo "       starts all three in this terminal (Ctrl+C stops them):"
echo "         API     .venv/bin/uvicorn apps.api.main:app --port 8000"
echo "         Worker  .venv/bin/python -m brain.workers.worker   (indexing and other background jobs)"
echo "         Web     cd apps/web && npm run dev                 (http://127.0.0.1:3100, needs Node.js 22)"
echo "  3. Open http://127.0.0.1:3100/setup"
