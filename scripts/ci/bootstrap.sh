#!/usr/bin/env bash
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends git curl ca-certificates build-essential libpq-dev postgresql-client
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
