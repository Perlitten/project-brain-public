#!/usr/bin/env bash
set -euo pipefail
python scripts/ci/wait_services.py
trap 'test ! -f /tmp/brain-api.pid || kill $(cat /tmp/brain-api.pid) || true' EXIT

# Install dependencies (hash-locked)


# Run migrations
python scripts/check_migrations.py

# Start API
nohup python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 \
  > /tmp/brain-api.log 2>&1 &
echo $! > /tmp/brain-api.pid
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "API is up"
    break
  fi
  sleep 5
done
curl -sf http://127.0.0.1:8000/health || (cat /tmp/brain-api.log && exit 1)


# Index repository
# POST /index runs the index synchronously and returns its result.
RESULT=$(curl -sf -X POST http://127.0.0.1:8000/index \
  -H "X-API-Key: $PROJECT_BRAIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repo_path": ".", "clean": true}')
echo "${RESULT:0:500}"
if ! echo "$RESULT" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('status') == 'success' else 1)"; then
  echo "indexing failed"; tail -50 /tmp/brain-api.log; exit 1
fi
RUN=$(curl -sf -H "X-API-Key: $PROJECT_BRAIN_API_KEY" http://127.0.0.1:8000/api/status/indexing \
  | python3 -c "import json,sys; r=json.load(sys.stdin)['recent_runs'][0]; print(r['status'], r['progress'].get('files', {}))" \
  || echo "unknown")
echo "latest indexing run: $RUN"
case "$RUN" in failed*) echo "indexing run failed"; exit 1 ;; esac


# Run harness benchmark
set -o pipefail
mkdir -p eval/harness_benchmark/results
python eval/harness_benchmark/run.py \
  --api-url http://127.0.0.1:8000 \
  --repo "$PWD" \
  --skip-review \
  --output eval/harness_benchmark/results/benchmark_ci.json 2>&1 | tail -30


# Regression gate
python eval/harness_benchmark/ci_gate.py \
  --results eval/harness_benchmark/results/benchmark_ci.json \
  --baseline eval/harness_benchmark/baseline.json

