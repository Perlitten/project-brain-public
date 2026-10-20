#!/usr/bin/env bash
set -euo pipefail

# Install dependencies (hash-locked)


# Build the execution test images
docker build -f tests/docker/execution.Dockerfile --target environment -t brain-exec-test-env .
docker build -f tests/docker/execution.Dockerfile --target egress-proxy -t brain-exec-test-proxy .


# Real-docker execution tests
pytest tests/test_team_execution_container.py -m real_docker -v -p no:cacheprovider
