#!/usr/bin/env bash
# Daily deterministic Project Brain watchdog -> Telegram.
# Silence means healthy. This is a fallback beside the LLM diagnosis workflow.
#
# Checks:
#   1. strict API readiness
#   2. every repository's freshness
#   3. worker error volume during the last 24 hours
#
# Install: 30 8 * * * bash /path/to/project-brain/deploy/brain_watchdog.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ALERT="$REPO_DIR/deploy/brain_alert.sh"
API="${BRAIN_API_URL:-http://127.0.0.1:8010}"
KEY="$(
  grep -E '^PROJECT_BRAIN_API_KEY=' "$REPO_DIR/.env" 2>/dev/null |
    head -1 |
    cut -d= -f2- |
    sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" |
    tr -d '\r'
)"
PROBLEMS=""

add() {
  PROBLEMS="${PROBLEMS}$1
"
}

# 1. Strict readiness ---------------------------------------------------------
ready_file="$(mktemp)"
ready_code="$(curl -sS --max-time 20 -o "$ready_file" -w '%{http_code}' "$API/ready" 2>/dev/null || true)"
readiness="$(cat "$ready_file" 2>/dev/null || true)"
rm -f "$ready_file"
if [ "$ready_code" != "200" ] || [[ "$readiness" != *'"status":"ready"'* ]]; then
  if [ -z "$readiness" ]; then
    add "- API does not answer on /ready"
  else
    add "- API is not ready (HTTP ${ready_code:-none}): $(printf '%s' "$readiness" | head -c 220)"
  fi
fi

# 2. Repository freshness -----------------------------------------------------
if [ -n "$KEY" ]; then
  repos_file="$(mktemp)"
  http="$(curl -sS --max-time 30 -o "$repos_file" -w '%{http_code}' \
    "$API/repositories" -H "X-API-Key: $KEY" 2>/dev/null || true)"
  if [ "$http" != "200" ]; then
    add "- Failed to fetch repositories: HTTP ${http:-none}"
  else
    parse_err="$(mktemp)"
    bad="$(python3 - "$repos_file" 2>"$parse_err" <<'PY'
import json
import sys

rows = json.load(open(sys.argv[1], encoding="utf-8")).get("repositories", [])
for row in rows:
    freshness = row.get("freshness") or {}
    status = freshness.get("status") or "unknown"
    path = row.get('path') or ''
    if status in {"source_missing", "stale", "unknown", "behind"}:
        age = freshness.get("age_seconds")
        age_text = f"{int(age) // 86400}d" if isinstance(age, (int, float)) else "?"
        print(f"- {path}: {status} (age {age_text})")
    elif status == "unverifiable" and (path == "/app" or path == "."):
        age = freshness.get("age_seconds")
        age_text = f"{int(age) // 86400}d" if isinstance(age, (int, float)) else "?"
        print(f"- {path}: {status} (age {age_text})")
PY
)"
    if [ -s "$parse_err" ]; then
      add "- Freshness parser failed: $(head -c 200 "$parse_err" | tr '\n' ' ')"
    elif [ -n "$bad" ]; then
      add "$bad"
    fi
    rm -f "$parse_err"
  fi
  rm -f "$repos_file"
else
  add "- PROJECT_BRAIN_API_KEY is unavailable; repository checks were skipped"
fi

# 3. Worker errors ------------------------------------------------------------
errors="$(docker logs brain-worker --since 24h 2>&1 |
  grep -cE 'Traceback|CRITICAL|Failed to generate embedding' || true)"
if [ "${errors:-0}" -gt 20 ]; then
  add "- Worker emitted $errors error markers in the last 24 hours"
fi

if [ -n "$PROBLEMS" ]; then
  printf '%s' "$PROBLEMS" | bash "$ALERT" "Deterministic watchdog requires attention"
fi
