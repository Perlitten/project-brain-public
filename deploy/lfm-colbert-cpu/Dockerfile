FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENBLAS_NUM_THREADS=2 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

WORKDIR /srv/project-brain

COPY deploy/lfm-colbert-cpu/requirements.lock ./requirements.lock
COPY brain ./brain
COPY apps ./apps
COPY scripts/validate_lfm_release_gate.py ./scripts/validate_lfm_release_gate.py

RUN python -m pip install --no-cache-dir --no-deps --requirement requirements.lock \
    && python -m pip check \
    && groupadd --gid 10001 lfm \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin lfm \
    && mkdir -p /var/lib/lfm \
    && chown -R lfm:lfm /var/lib/lfm /srv/project-brain

USER 10001:10001

EXPOSE 8090

CMD ["uvicorn", "brain.late_interaction.gpu_service:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--no-access-log"]
