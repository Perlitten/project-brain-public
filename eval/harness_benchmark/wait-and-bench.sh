#!/bin/bash
# Wait for Brain indexing to complete, then run the full harness benchmark.
# Usage: ./wait-and-bench.sh
set -uo pipefail

REPO="$HOME/workspace/projects/brain"
API_URL="http://127.0.0.1:8000"
API_KEY=$(grep "^PROJECT_BRAIN_API_KEY=" "$REPO/.env" | cut -d= -f2)
OUT="$REPO/eval/harness_benchmark/results"

echo "[wait-and-bench] waiting for indexing to complete..."
for i in $(seq 1 120); do
  STATUS=$(curl -s -m 15 -H "X-API-Key: $API_KEY" "$API_URL/api/status/indexing" 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['recent_runs'][0]['status'])" 2>/dev/null)
  echo "  poll $i: $STATUS"
  if [ "$STATUS" = "completed" ]; then
    echo "[wait-and-bench] index completed, running benchmark..."
    mkdir -p "$OUT"
    STAMP=$(date +%Y%m%d_%H%M%S)
    /home/hatch/workspace/brain-env.sh "$REPO/.venv/bin/python" "$REPO/eval/harness_benchmark/run.py" \
      --output "$OUT/benchmark_$STAMP.json" 2>&1 | tail -20
    echo "[wait-and-bench] done: $OUT/benchmark_$STAMP.json"
    exit 0
  fi
  if [ "$STATUS" = "failed" ]; then
    echo "[wait-and-bench] indexing FAILED, aborting"
    exit 1
  fi
  sleep 60
done
echo "[wait-and-bench] timed out waiting for indexing"
exit 1
