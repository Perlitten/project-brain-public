from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import pytest
from fastapi.testclient import TestClient

from brain.late_interaction.gpu_service import ServiceConfig, create_app
from brain.late_interaction.llama_cpp_encoder import LlamaCppEncoder
from brain.late_interaction.provider import EncodedText


GGUF_REVISION = "bc240003aba07253e261a8aaf0d2c9683318a967"
ROOT = Path(__file__).resolve().parents[1]


class FakeLlamaProvider:
    model = "LiquidAI/LFM2.5-ColBERT-350M-GGUF"
    model_revision = GGUF_REVISION
    dimension = 128

    def __init__(self, *, healthy: bool = True):
        self.healthy = healthy
        self.health_calls = 0
        self.embed_calls: list[tuple[str, bool]] = []
        self.closed = False

    async def health(self) -> dict[str, str]:
        self.health_calls += 1
        return {"status": "healthy" if self.healthy else "unhealthy"}

    async def props(self) -> dict[str, str]:
        return {
            "model_alias": "/models/LFM2.5-ColBERT-350M-BF16.gguf",
            "model_ftype": "BF16",
        }

    async def embed(self, text: str, *, is_query: bool) -> EncodedText:
        self.embed_calls.append((text, is_query))
        value = float(len(self.embed_calls))
        return EncodedText(
            vectors=np.full((2, 128), value, dtype=np.float32),
            token_count=2,
            truncated=False,
        )

    async def aclose(self) -> None:
        self.closed = True


class FactoryEncoder:
    def __init__(self, model_revision: str, dimension: int):
        self.model_revision = model_revision
        self.dimension = dimension

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [np.ones((1, self.dimension), dtype=np.float32) for _ in texts]

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [np.ones((1, self.dimension), dtype=np.float32) for _ in texts]


class FailingEncoder(FactoryEncoder):
    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        raise RuntimeError("sidecar unavailable")


@pytest.mark.asyncio
async def test_llama_cpp_encoder_adapts_provider_and_preserves_batch_order():
    provider = FakeLlamaProvider()
    encoder = LlamaCppEncoder(
        base_url="http://llama:8080",
        model_name=provider.model,
        model_revision=provider.model_revision,
        dimension=provider.dimension,
        max_concurrency=2,
        provider=provider,
    )

    queries = await encoder.encode_queries(["first", "second"])
    documents = await encoder.encode_documents(["third"])
    await encoder.aclose()

    assert provider.embed_calls == [
        ("first", True),
        ("second", True),
        ("third", False),
    ]
    assert [float(matrix[0, 0]) for matrix in queries + documents] == [1.0, 2.0, 3.0]
    assert all(matrix.dtype == np.float32 for matrix in queries + documents)
    assert provider.closed is True


@pytest.mark.asyncio
async def test_llama_cpp_encoder_ready_warms_both_paths_once():
    provider = FakeLlamaProvider()
    encoder = LlamaCppEncoder(
        base_url="http://llama:8080",
        model_name=provider.model,
        model_revision=provider.model_revision,
        expected_model_alias="/models/LFM2.5-ColBERT-350M-BF16.gguf",
        expected_model_ftype="BF16",
        provider=provider,
    )

    await encoder.ready()
    await encoder.ready()

    assert provider.health_calls == 2
    assert provider.embed_calls == [
        ("Project Brain readiness query", True),
        ("Project Brain readiness document", False),
    ]


@pytest.mark.asyncio
async def test_llama_cpp_encoder_rejects_unhealthy_sidecar_and_empty_text():
    provider = FakeLlamaProvider(healthy=False)
    encoder = LlamaCppEncoder(
        base_url="http://llama:8080",
        model_name=provider.model,
        model_revision=provider.model_revision,
        provider=provider,
    )

    with pytest.raises(RuntimeError, match="unhealthy"):
        await encoder.ready()
    with pytest.raises(ValueError, match="non-empty"):
        await encoder.encode_documents([""])
    assert await encoder.encode_queries([]) == []


@pytest.mark.asyncio
async def test_llama_cpp_encoder_rejects_wrong_runtime_model_identity():
    provider = FakeLlamaProvider()
    encoder = LlamaCppEncoder(
        base_url="http://llama:8080",
        model_name=provider.model,
        model_revision=provider.model_revision,
        expected_model_alias="/models/not-the-pinned-model.gguf",
        expected_model_ftype="BF16",
        provider=provider,
    )

    with pytest.raises(RuntimeError, match="model alias mismatch"):
        await encoder.ready()


def test_service_factory_defaults_to_pylate(monkeypatch, tmp_path):
    created: dict[str, object] = {}

    def fake_pylate(**kwargs):
        created.update(kwargs)
        return FactoryEncoder(
            model_revision=str(kwargs["model_revision"]),
            dimension=int(kwargs["dimension"]),
        )

    monkeypatch.setattr(
        "brain.late_interaction.pylate_encoder.PyLateEncoder",
        fake_pylate,
    )
    config = ServiceConfig(
        database_path=str(tmp_path / "late.sqlite3"),
        token="test-token",
        environment="test",
    )

    app = create_app(config)

    assert config.encoder_backend == "pylate"
    assert app.state.encoder.model_revision == config.model_revision
    assert created["model_name"] == config.model_name
    assert "base_url" not in created


def test_service_factory_selects_llama_cpp_from_config(monkeypatch, tmp_path):
    created: dict[str, object] = {}

    def fake_llama(**kwargs):
        created.update(kwargs)
        return FactoryEncoder(
            model_revision=str(kwargs["model_revision"]),
            dimension=int(kwargs["dimension"]),
        )

    monkeypatch.setattr(
        "brain.late_interaction.llama_cpp_encoder.LlamaCppEncoder",
        fake_llama,
    )
    config = ServiceConfig(
        database_path=str(tmp_path / "late.sqlite3"),
        token="test-token",
        environment="test",
        encoder_backend="llama_cpp",
        llama_cpp_url="http://llama:8080",
        model_name="LiquidAI/LFM2.5-ColBERT-350M-GGUF",
        model_revision=GGUF_REVISION,
        llama_cpp_max_concurrency=2,
        llama_cpp_expected_model_alias="/models/LFM2.5-ColBERT-350M-BF16.gguf",
        llama_cpp_expected_model_ftype="BF16",
    )

    app = create_app(config)

    assert app.state.encoder.model_revision == GGUF_REVISION
    assert created["base_url"] == "http://llama:8080"
    assert created["max_concurrency"] == 2
    assert created["model_name"] == "LiquidAI/LFM2.5-ColBERT-350M-GGUF"
    assert created["expected_model_alias"] == "/models/LFM2.5-ColBERT-350M-BF16.gguf"


def test_service_maps_sidecar_failure_to_retryable_503(tmp_path):
    config = ServiceConfig(
        database_path=str(tmp_path / "late.sqlite3"),
        token="test-token",
        environment="test",
        model_revision=GGUF_REVISION,
        max_documents=1,
    )
    app = create_app(
        config,
        encoder=FailingEncoder(GGUF_REVISION, 128),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/index/upsert",
            headers={"X-Late-Interaction-Token": "test-token"},
            json={
                "repository_id": 7,
                "model_revision": GGUF_REVISION,
                "documents": [
                    {
                        "chunk_id": 10,
                        "path": "alpha.py",
                        "content_hash": "a" * 64,
                        "text": "alpha",
                    }
                ],
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "late-interaction encoder unavailable"


def test_service_config_selects_llama_cpp_from_environment(monkeypatch):
    monkeypatch.setenv("LATE_INTERACTION_SERVICE_ENCODER_BACKEND", "llama_cpp")
    monkeypatch.setenv(
        "LATE_INTERACTION_SERVICE_LLAMA_CPP_URL",
        "http://llama:8080/",
    )
    monkeypatch.setenv("LATE_INTERACTION_SERVICE_LLAMA_CPP_TIMEOUT_S", "45")
    monkeypatch.setenv(
        "LATE_INTERACTION_SERVICE_LLAMA_CPP_MAX_CONCURRENCY",
        "2",
    )
    monkeypatch.setenv(
        "LATE_INTERACTION_SERVICE_LLAMA_CPP_EXPECTED_MODEL_ALIAS",
        "/models/LFM2.5-ColBERT-350M-BF16.gguf",
    )
    monkeypatch.setenv(
        "LATE_INTERACTION_SERVICE_LLAMA_CPP_EXPECTED_MODEL_FTYPE",
        "BF16",
    )

    config = ServiceConfig.from_env()

    assert config.encoder_backend == "llama_cpp"
    assert config.llama_cpp_url == "http://llama:8080"
    assert config.llama_cpp_timeout_s == 45.0
    assert config.llama_cpp_max_concurrency == 2
    assert config.llama_cpp_expected_model_ftype == "BF16"


@pytest.mark.parametrize(
    ("backend", "url", "message"),
    [
        ("unknown", "http://llama:8080", "ENCODER_BACKEND"),
        ("llama_cpp", "not-a-url", "LLAMA_CPP_URL"),
        ("llama_cpp", "http://user:secret@llama:8080", "credentials"),
    ],
)
def test_service_config_rejects_unsafe_encoder_configuration(
    backend,
    url,
    message,
):
    config = ServiceConfig(
        token="test-token",
        environment="test",
        encoder_backend=backend,
        llama_cpp_url=url,
    )

    with pytest.raises(RuntimeError, match=message):
        config.validate_startup()


def test_cpu_compose_is_bf16_private_authenticated_and_default_off():
    compose = (
        ROOT / "deploy" / "lfm-colbert-cpu" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    dockerfile = (
        ROOT / "deploy" / "lfm-colbert-cpu" / "Dockerfile"
    ).read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "LFM2.5-ColBERT-350M-BF16.gguf" in compose
    assert "c21d5cacc004cbc7746dbeeaee496c74b01f0f7bfdef1e1a57570d1744ef871b" in compose
    assert "LATE_INTERACTION_SERVICE_ENCODER_BACKEND: llama_cpp" in compose
    assert 'LATE_INTERACTION_SERVICE_LLAMA_CPP_MAX_CONCURRENCY: "1"' in compose
    assert 'LATE_INTERACTION_SERVICE_MAX_DOCUMENTS: "1"' in compose
    assert "LATE_INTERACTION_SERVICE_LLAMA_CPP_EXPECTED_MODEL_ALIAS:" in compose
    assert "LATE_INTERACTION_SERVICE_TOKEN: ${LATE_INTERACTION_SERVICE_TOKEN:?" in compose
    assert "LFM_LICENSE_ACKNOWLEDGED: ${LFM_LICENSE_ACKNOWLEDGED:?" in compose
    assert "${LFM_CPU_SERVICE_BIND_ADDRESS:-127.0.0.1}" in compose
    assert "${LFM_CPU_SERVICE_PORT:-8094}:8090" in compose
    assert "internal: true" in compose
    assert "name: brain_default" in compose
    assert "requirements.lock" in dockerfile
    assert (
        "COPY scripts/validate_lfm_release_gate.py "
        "./scripts/validate_lfm_release_gate.py"
    ) in dockerfile
    assert "!deploy/lfm-colbert-cpu/requirements.lock" in dockerignore
    assert "io.project-brain.cutover: \"false\"" in compose
    assert "LATE_INTERACTION_ENABLED" not in compose
    assert "LATE_INTERACTION_SHADOW_ENABLED" not in compose
    assert "LATE_INTERACTION_RERANK_ENABLED" not in compose
    assert "@sha256:" in dockerfile
