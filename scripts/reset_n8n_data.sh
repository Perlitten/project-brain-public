#!/usr/bin/env bash
# Reset n8n SQLite volume (removes all imported workflows and credentials).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Stopping n8n container..."
docker compose stop n8n

echo "Removing n8n container..."
docker compose rm -f n8n

volume="$(docker volume ls --format '{{.Name}}' | grep n8n_data | head -n1 || true)"
if [[ -n "${volume}" ]]; then
  echo "Removing volume ${volume}..."
  docker volume rm "${volume}"
else
  echo "No n8n_data volume found (already clean)."
fi

echo "Starting n8n..."
docker compose up -d n8n

cat <<'EOF'

Done. Re-import Project Brain workflows from n8n/workflows/:
  - git-merge-reindex.json
  - nightly-health.json
  - weekly-benchmark.json
  - nightly-proactive-insights.json
EOF
