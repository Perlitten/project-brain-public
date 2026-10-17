# Runner image for scripts/team_e2e_check.py (opt-in team-mode end-to-end check).
#
# Built by scripts/team_e2e_docker.sh from a staging directory that holds
#   main/    the working tree under test
#   legacy/  `git archive c13d056` (the release production ran before team mode)
# Not a production image: it carries the PostgreSQL client tools the backup and
# restore scripts shell out to, pinned to the server's major version like
# production (pg_dump runs inside the pg15 container there).
FROM python:3.12-slim

ARG PG_CLIENT_MAJOR=15

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-client-${PG_CLIENT_MAJOR}" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work/main
COPY main/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

COPY legacy /work/legacy
COPY main /work/main

ENV PYTHONDONTWRITEBYTECODE=1 \
    TEAM_E2E_LEGACY_TREE=/work/legacy \
    TEAM_E2E_WORK_DIR=/work/out

ENTRYPOINT ["python", "scripts/team_e2e_check.py"]
