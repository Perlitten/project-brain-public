# --- Builder stage ---
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY brain ./brain
COPY apps ./apps
COPY rules ./rules
COPY n8n ./n8n
COPY eval ./eval
COPY scripts/write_source_manifest.py ./scripts/write_source_manifest.py
COPY scripts/validate_lfm_release_gate.py ./scripts/validate_lfm_release_gate.py
COPY Dockerfile docker-compose.prod.yml /release-inputs/
COPY deploy/server_up.sh /release-inputs/deploy/
COPY scripts/write_source_manifest.py scripts/validate_lfm_release_gate.py /release-inputs/scripts/

ARG BRAIN_BUILD_SHA=unknown
ARG BRAIN_BUILD_TIME=unknown
ARG BRAIN_SOURCE_DIGEST=unknown
ARG BRAIN_DEPLOY_DIGEST=unknown
ARG BRAIN_REQUIRE_RELEASE_IDENTITY=false

RUN if [ "${BRAIN_REQUIRE_RELEASE_IDENTITY}" = "true" ]; then \
        python scripts/write_source_manifest.py /build \
            --source-revision "${BRAIN_BUILD_SHA}" \
            --expected-content-digest "${BRAIN_SOURCE_DIGEST}" \
            --require-release-identity && \
        python scripts/write_source_manifest.py /release-inputs \
            --source-revision "${BRAIN_BUILD_SHA}" \
            --expected-content-digest "${BRAIN_DEPLOY_DIGEST}" \
            --require-release-identity; \
    else \
        python scripts/write_source_manifest.py /build \
            --source-revision "${BRAIN_BUILD_SHA}"; \
    fi

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# --- Runtime stage ---
FROM python:3.11-slim

WORKDIR /app

# Create non-root user with a PINNED uid/gid. `useradd -r` hands out whatever
# system id is free at build time; the id leaks into bind-mount ownership on
# the host, so an unpinned id makes every rebuild a potential permission
# incident (brain-api crash-looped on /app/reports exactly this way,
# 2026-07-30). 999 matches what production already has on disk.
RUN groupadd -r -g 999 brain && useradd -r -u 999 -g brain -s /bin/false brain

# The indexer and freshness checks shell out to git (commit hash, clean-tree
# verification). Without it every index run failed with "No such file or
# directory: 'git'" (2026-10).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY brain ./brain
COPY apps ./apps
COPY rules ./rules
COPY n8n ./n8n
COPY eval ./eval
COPY scripts/write_source_manifest.py ./scripts/write_source_manifest.py
COPY scripts/validate_lfm_release_gate.py ./scripts/validate_lfm_release_gate.py

# Copy release manifests from builder
COPY --from=builder /build/.brain-source-manifest.json ./.brain-source-manifest.json

ARG BRAIN_BUILD_SHA=unknown
ARG BRAIN_BUILD_TIME=unknown
ARG BRAIN_SOURCE_DIGEST=unknown
ARG BRAIN_DEPLOY_DIGEST=unknown

ENV BRAIN_BUILD_SHA=${BRAIN_BUILD_SHA} \
    BRAIN_BUILD_TIME=${BRAIN_BUILD_TIME} \
    BRAIN_SOURCE_DIGEST=${BRAIN_SOURCE_DIGEST} \
    BRAIN_DEPLOY_DIGEST=${BRAIN_DEPLOY_DIGEST}

LABEL org.opencontainers.image.revision=${BRAIN_BUILD_SHA} \
      org.opencontainers.image.created=${BRAIN_BUILD_TIME} \
      io.project-brain.source-digest=${BRAIN_SOURCE_DIGEST} \
      io.project-brain.deploy-digest=${BRAIN_DEPLOY_DIGEST}

# Writable directories for runtime artifacts
RUN mkdir -p /app/reports /app/context_packs /app/logs && \
    chown -R brain:brain /app

USER brain

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
