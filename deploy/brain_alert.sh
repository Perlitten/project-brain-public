#!/usr/bin/env bash
# Send a Project Brain alert to Telegram.
#
# Credentials are read from Project Brain's deployment .env at call time.
# The token is never echoed or placed in the request URL.
#
# Usage: brain_alert.sh "<title>" "<body>"
#        echo "<body>" | brain_alert.sh "<title>"
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BRAIN_ALERT_ENV_FILE:-$(cd "$SCRIPT_DIR/.." && pwd)/.env}"
TITLE="${1:-Brain alert}"
BODY="${2:-$(cat 2>/dev/null || true)}"

[ -f "$ENV_FILE" ] || {
  echo "brain_alert: no env file at $ENV_FILE" >&2
  exit 2
}

env_value() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null |
    head -1 |
    cut -d= -f2- |
    sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" |
    tr -d '\r'
}

TOKEN="$(env_value TELEGRAM_ALERT_BOT_TOKEN)"
CHAT="$(env_value TELEGRAM_ALERT_CHAT_ID | tr -d ' ')"
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || {
  echo "brain_alert: TELEGRAM_ALERT_BOT_TOKEN or TELEGRAM_ALERT_CHAT_ID missing in $ENV_FILE" >&2
  exit 2
}

TEXT="$(printf 'Project Brain: %s\n%s' "$TITLE" "$BODY")"

# Deduplication guard: compute hash of TEXT and suppress if sent within 6 hours (21600s)
HASH="$(python3 -c "import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode('utf-8')).hexdigest()[:16])" "$TEXT" 2>/dev/null || echo "nohash")"
DEDUP_FILE="/tmp/.brain_alert_dedup_${HASH}"
NOW=$(date +%s 2>/dev/null || echo 0)

if [ -f "$DEDUP_FILE" ]; then
  LAST=$(cat "$DEDUP_FILE" 2>/dev/null || echo 0)
  DIFF=$((NOW - LAST))
  if [ "$DIFF" -gt 0 ] && [ "$DIFF" -lt 21600 ]; then
    echo "brain_alert: suppressing duplicate alert (sent ${DIFF}s ago)"
    exit 0
  fi
fi

OUT_FILE="$(mktemp)"
trap 'rm -f "$OUT_FILE"' EXIT

code="$(curl -sS -o "$OUT_FILE" -w '%{http_code}' --max-time 25 \
  "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" \
  --data-urlencode "text=${TEXT}" \
  --data-urlencode "disable_web_page_preview=true")"

if [ "$code" = "200" ]; then
  echo "$NOW" > "$DEDUP_FILE" 2>/dev/null || true
elif [ "$code" != "200" ]; then
  # Do not echo the Telegram response: some error payloads can expose secrets.
  echo "brain_alert: send failed with HTTP $code" >&2
  exit 1
fi
