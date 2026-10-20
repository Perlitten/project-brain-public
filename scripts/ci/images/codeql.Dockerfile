FROM node:22.23.3-bookworm@sha256:0e5f906573693feaa1e21057ebdcfdb5bd5021f050b2dc7c9deceb629c7da2a8 AS node_runtime
FROM python:3.12-bookworm@sha256:5560e9ab8709f459489e5b8aa696eda8a07ef821e14bb122be62d91234bfa98b
RUN apt-get update -qq && apt-get install -y --no-install-recommends ca-certificates curl git zstd \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fL --retry 3 -o /tmp/codeql.tar.zst https://github.com/github/codeql-action/releases/download/codeql-bundle-v2.27.3/codeql-bundle-linux64.tar.zst \
    && echo '5e7607e1b721728955e31f5402d460ad227cbd2ffed0d4cc610116755796150e  /tmp/codeql.tar.zst' | sha256sum -c - \
    && mkdir -p /opt/codeql \
    && tar --use-compress-program=unzstd -xf /tmp/codeql.tar.zst -C /opt/codeql \
    && rm /tmp/codeql.tar.zst
ENV PATH="/opt/codeql/codeql:${PATH}"
RUN codeql version --format=json
LABEL org.opencontainers.image.description="Pinned CodeQL bundle for unprivileged public Project Brain CI" \
      ci.codeql.version="2.27.3" \
      ci.codeql.bundle.sha256="5e7607e1b721728955e31f5402d460ad227cbd2ffed0d4cc610116755796150e"

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
RUN node --version && codeql version --format=terse
