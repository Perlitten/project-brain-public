#!/usr/bin/env bash
# PostgreSQL restore; the configured target must be explicitly confirmed.
set -euo pipefail
cd "$(dirname "$0")/.."
python="${BRAIN_PYTHON:-.venv/bin/python}"
if [[ ! -x "$python" ]]; then
  echo "Restore requires an installed Brain virtual environment (or BRAIN_PYTHON)." >&2
  exit 1
fi
exec "$python" -m scripts.restore_database "$@"
