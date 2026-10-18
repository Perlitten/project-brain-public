# Test images for the real-docker execution tests (pytest -m real_docker; CI job container-real-docker).
# Build from the repository root:
#   docker build -f tests/docker/execution.Dockerfile --target environment -t brain-exec-test-env .
#   docker build -f tests/docker/execution.Dockerfile --target egress-proxy -t brain-exec-test-proxy .
# Not a production image: operators build their own execution environments, and production runs the
# egress proxy from the brain-api image (BRAIN_EXECUTION_EGRESS_PROXY_IMAGE).

# An execution environment: Python plus git (the container backend reads the working copy's head,
# changes and patch with git inside the environment image).
FROM python:3.12-slim AS environment
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# The egress proxy sidecar: brain.execution.egress and its import closure are standard library only.
FROM python:3.12-slim AS egress-proxy
WORKDIR /opt/brain
COPY brain/__init__.py brain/_version.py ./brain/
COPY brain/execution ./brain/execution
ENV PYTHONPATH=/opt/brain
