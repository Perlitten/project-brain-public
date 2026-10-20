#!/usr/bin/env bash
# Dedicated disposable local-backend runner; Python 3.12, Docker + Compose,
# git and PostgreSQL 15 clients must be provisioned on the runner.
set -euo pipefail
python3.12 -m venv .ci-venv
.ci-venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.ci-venv/bin/python -m pip install --no-deps -e .
docker info >/dev/null
docker compose version
command -v pg_dump
command -v psql
export PATH="$PWD/.ci-venv/bin:$PATH"
