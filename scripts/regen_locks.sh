#!/usr/bin/env bash
# Regenerate the hashed dependency lockfiles from pyproject.toml.
# Requires pip-tools in the active environment: pip install pip-tools
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"
"$PYTHON" -m piptools compile --generate-hashes --no-strip-extras -o requirements.lock pyproject.toml
"$PYTHON" -m piptools compile --generate-hashes --no-strip-extras --extra dev -o requirements-dev.lock pyproject.toml

echo "wrote requirements.lock and requirements-dev.lock"
