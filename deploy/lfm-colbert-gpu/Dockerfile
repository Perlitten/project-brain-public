FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime@sha256:417bd75df6365104c283ea4c1651fb3530d9eb5a4c2fafa51943cff2a94e6385

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /srv/project-brain

COPY pyproject.toml README.md ./
COPY brain ./brain
COPY apps ./apps

RUN python -m pip install --no-cache-dir ".[late-interaction-gpu]" \
    && groupadd --gid 10001 lfm \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin lfm \
    && mkdir -p /var/lib/lfm /models/huggingface \
    && chown -R lfm:lfm /var/lib/lfm /models/huggingface /srv/project-brain

USER 10001:10001

EXPOSE 8090

CMD ["uvicorn", "brain.late_interaction.gpu_service:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--no-access-log"]
