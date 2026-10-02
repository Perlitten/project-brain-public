#!/usr/bin/env bash
# Nightly retrieval-quality alarm.
#
# Staleness and silent index corruption do not announce themselves; a drop in
# retrieval quality does. This runs the golden set against the live API and
# shouts when the score falls below the previous run, so the first sign of a
# broken index is a number, not a user noticing bad answers weeks later.
#
# Install: 15 3 * * * bash /path/to/project-brain/deploy/golden_eval_nightly.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
STATE_DIR="${STATE_DIR:-$REPO_DIR/reports/eval-state}"
LOG="$STATE_DIR/golden-eval.log"
LATEST="$STATE_DIR/latest.json"
PREVIOUS="$STATE_DIR/previous.json"
TOKEN_ECONOMY_LATEST="$STATE_DIR/token-economy-latest.json"
# Below this, treat the run as a failure rather than a dip.
FLOOR="${GOLDEN_EVAL_FLOOR:-70}"

mkdir -p "$STATE_DIR"
stamp() { date -u +%FT%TZ; }

[ -f "$LATEST" ] && cp -f "$LATEST" "$PREVIOUS"

out="$(docker exec -e BRAIN_API_URL=http://127.0.0.1:8000 brain-api \
  sh -lc 'export BRAIN_API_KEY=$(grep -oP "^PROJECT_BRAIN_API_KEY=\K.*" /app/.env 2>/dev/null || printf "%s" "$PROJECT_BRAIN_API_KEY"); \
          python eval/run_golden_eval.py --limit 5 --json /tmp/golden.json >/dev/null 2>&1; \
          cat /tmp/golden.json' 2>/dev/null)"

if [ -z "$out" ]; then
  echo "$(stamp) FAIL: eval produced no output (container down, eval/ not mounted, or API key unreadable)" >> "$LOG"
  exit 1
fi
printf '%s' "$out" > "$LATEST"

# Keep the transport/call-count baseline alongside the retrieval-quality
# alarm. This is measurement only: it cannot change the serving code.
docker exec -e BRAIN_API_URL=http://127.0.0.1:8000 brain-api \
  sh -lc 'export BRAIN_API_KEY=$(grep -oP "^PROJECT_BRAIN_API_KEY=\\K.*" /app/.env 2>/dev/null || printf "%s" "$PROJECT_BRAIN_API_KEY"); \
          python eval/token_economy_benchmark.py --output /tmp/token-economy.json >/dev/null 2>&1; \
          cat /tmp/token-economy.json' > "$TOKEN_ECONOMY_LATEST" 2>/dev/null || true

read -r now_any now_lex <<EOF
$(printf '%s' "$out" | python3 -c '
import json,sys
d=json.load(sys.stdin)["summary"]
print(d.get("hit@3_any_pct",0), d.get("hit@3_pct",0))
' 2>/dev/null || echo "0 0")
EOF

prev_any=0
[ -f "$PREVIOUS" ] && prev_any="$(python3 -c '
import json,sys
try: print(json.load(open(sys.argv[1]))["summary"].get("hit@3_any_pct",0))
except Exception: print(0)
' "$PREVIOUS" 2>/dev/null || echo 0)"

verdict="ok"
# A drop of more than 5 points is a signal, not noise, on a fixed question set.
drop="$(python3 -c "print(1 if float('$prev_any') - float('$now_any') > 5 else 0)" 2>/dev/null || echo 0)"
below="$(python3 -c "print(1 if float('$now_any') < float('$FLOOR') else 0)" 2>/dev/null || echo 0)"
[ "$drop" = "1" ] && verdict="REGRESSION"
[ "$below" = "1" ] && verdict="BELOW_FLOOR"

echo "$(stamp) hit@3_any=$now_any hit@3_lexical=$now_lex prev_any=$prev_any verdict=$verdict" >> "$LOG"
if [ "$verdict" != "ok" ]; then
  echo "$(stamp) !! retrieval quality $verdict — inspect $LATEST" >> "$LOG"
  ALERT="${REPO_DIR}/deploy/brain_alert.sh"
  [ -x "$ALERT" ] || ALERT=""
  [ -n "$ALERT" ] && printf '• Hit@3 (все каналы): %s%%  ← было %s%%\n• Hit@3 (лексический): %s%%\n• Подробности: %s' \
      "$now_any" "$prev_any" "$now_lex" "$LATEST" \
    | bash "$ALERT" "Качество поиска: $verdict"
fi
