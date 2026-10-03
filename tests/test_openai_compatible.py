"""Generic OpenAI-compatible provider layer: presets, precedence, wire format,
legacy NVIDIA parity, batching, dimension safety, doctor/provider_check.

No network: HTTP goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from typer.testing import CliRunner

import apps.cli.commands.doctor as doctor_module
from apps.cli.main import cli
from brain.config.settings import settings
from brain.llm import get_embedding_provider, get_llm_provider, get_summarizer_provider
from brain.llm.presets import (
    PRESETS,
    is_local_base_url,
    join_url,
    llm_provider_ready,
    normalize_provider_name,
    resolve_embedding_endpoint,
    resolve_llm_endpoint,
    resolve_summarizer_endpoint,
    resolve_task_model,
)
from brain.llm.providers.nvidia_provider import (
    NvidiaEmbeddingProvider,
    NvidiaLLMProvider,
    NvidiaSummarizerProvider,
)
from brain.llm.providers.openai_compatible import (
    EmbeddingDimensionMismatchError,
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleLLMProvider,
    OpenAICompatibleSummarizerProvider,
)
from brain.llm.providers.openai_provider import OpenAIEmbeddingProvider, OpenAILLMProvider

_REAL_ASYNC_CLIENT = httpx.AsyncClient

_UNIVERSAL = (
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "SUMMARIZER_MODEL",
    "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL",
    "OPENROUTER_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY",
)


def _cfg(**overrides):
    base = {name: None for name in _UNIVERSAL}
    base.update(
        DEFAULT_LLM_PROVIDER="mock",
        DEFAULT_EMBEDDING_PROVIDER="mock",
        EMBEDDING_DIMENSION=0,
        EMBEDDING_MAX_INPUT_CHARS=0,
        EMBEDDING_BATCH_SIZE=0,
        EMBEDDING_SEND_INPUT_TYPE=None,
        NVIDIA_LLM_MODEL="meta/llama-3.1-70b-instruct",
        NVIDIA_EMBEDDING_MODEL="nvidia/nv-embedcode-7b-v1",
        NVIDIA_SUMMARIZER_MODEL="meta/llama-3.1-8b-instruct",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def clean_settings(monkeypatch):
    """Global settings with every universal/preset knob unset."""
    for name in _UNIVERSAL:
        monkeypatch.setattr(settings, name, None)
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 0)
    monkeypatch.setattr(settings, "EMBEDDING_MAX_INPUT_CHARS", 0)
    monkeypatch.setattr(settings, "EMBEDDING_BATCH_SIZE", 0)
    monkeypatch.setattr(settings, "EMBEDDING_SEND_INPUT_TYPE", None)
    monkeypatch.setattr(settings, "NVIDIA_LLM_MODEL", "meta/llama-3.1-70b-instruct")
    monkeypatch.setattr(settings, "NVIDIA_EMBEDDING_MODEL", "nvidia/nv-embedcode-7b-v1")
    monkeypatch.setattr(settings, "NVIDIA_SUMMARIZER_MODEL", "meta/llama-3.1-8b-instruct")
    monkeypatch.setattr(settings, "DEFAULT_LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "mock")
    return settings


class _Recorder:
    """MockTransport handler recording requests; replies with chat or embeddings."""

    def __init__(self, width: int = 3, status_sequence=None):
        self.requests: list[httpx.Request] = []
        self.width = width
        self.status_sequence = list(status_sequence or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_sequence:
            return httpx.Response(self.status_sequence.pop(0), headers={"Retry-After": "0"})
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            data = [
                {"index": i, "embedding": [float(i)] * self.width}
                for i in range(len(body["input"]))
            ]
            data.reverse()  # out of order on purpose: the client must sort
            return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    def payloads(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def transport(monkeypatch):
    def install(recorder: _Recorder) -> _Recorder:
        mock = httpx.MockTransport(recorder)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _REAL_ASYNC_CLIENT(transport=mock))
        monkeypatch.setattr("brain.llm.providers.openai_compatible.asyncio.sleep", AsyncMock())
        return recorder

    return install


# --------------------------------------------------------------------------- presets


def test_registry_has_required_presets():
    for name in ("openai", "nvidia", "openrouter", "groq", "together", "deepseek", "ollama",
                 "mistral", "lmstudio", "openai_compatible"):
        assert name in PRESETS
    # The NVIDIA endpoint lives in the registry only.
    assert PRESETS["nvidia"].base_url == "https://integrate.api.nvidia.com/v1"


def test_aliases_resolve_to_openai_compatible():
    assert normalize_provider_name("custom") == "openai_compatible"
    assert normalize_provider_name("OpenAI-Compatible") == "openai_compatible"
    assert normalize_provider_name(" NVIDIA ") == "nvidia"


def test_preset_resolution_uses_provider_specific_key():
    cfg = _cfg(DEFAULT_LLM_PROVIDER="groq", GROQ_API_KEY="gsk_test")
    endpoint = resolve_llm_endpoint("groq", cfg=cfg)
    assert endpoint.base_url == "https://api.groq.com/openai/v1"
    assert endpoint.api_key == "gsk_test"
    assert endpoint.key_source == "GROQ_API_KEY"
    assert endpoint.model == "llama-3.3-70b-versatile"
    assert resolve_summarizer_endpoint("groq", cfg=cfg).model == "llama-3.1-8b-instant"


def test_universal_settings_override_preset_for_active_provider_only():
    cfg = _cfg(
        DEFAULT_LLM_PROVIDER="openrouter",
        LLM_BASE_URL="https://proxy.example.com/v1/",
        LLM_API_KEY="universal-key",
        LLM_MODEL="anthropic/claude-3.5-sonnet",
        SUMMARIZER_MODEL="openai/gpt-4o-mini",
        OPENROUTER_API_KEY="preset-key",
    )
    endpoint = resolve_llm_endpoint("openrouter", cfg=cfg)
    assert endpoint.base_url == "https://proxy.example.com/v1/"
    assert endpoint.api_key == "universal-key"
    assert endpoint.model == "anthropic/claude-3.5-sonnet"
    assert resolve_summarizer_endpoint("openrouter", cfg=cfg).model == "openai/gpt-4o-mini"
    # An explicit model argument beats LLM_MODEL (per-task routing).
    assert resolve_llm_endpoint("openrouter", model="x/y", cfg=cfg).model == "x/y"
    # Not the active provider: universal settings do not leak into other presets.
    other = resolve_llm_endpoint("groq", cfg=cfg)
    assert other.base_url == "https://api.groq.com/openai/v1"
    assert other.api_key is None
    assert other.model == "llama-3.3-70b-versatile"


def test_blank_strings_are_unset():
    cfg = _cfg(DEFAULT_LLM_PROVIDER="groq", LLM_BASE_URL="  ", LLM_API_KEY="", LLM_MODEL="",
               GROQ_API_KEY="gsk_x")
    endpoint = resolve_llm_endpoint("groq", cfg=cfg)
    assert endpoint.base_url == "https://api.groq.com/openai/v1"
    assert endpoint.api_key == "gsk_x"
    assert endpoint.model == "llama-3.3-70b-versatile"


def test_embedding_endpoint_falls_back_to_llm_endpoint_for_same_provider():
    cfg = _cfg(
        DEFAULT_LLM_PROVIDER="custom",
        DEFAULT_EMBEDDING_PROVIDER="openai-compatible",
        LLM_BASE_URL="https://llm.example.com/v1",
        LLM_API_KEY="shared-key",
        EMBEDDING_MODEL="bge-m3",
        EMBEDDING_DIMENSION=1024,
    )
    endpoint = resolve_embedding_endpoint("openai_compatible", cfg=cfg)
    assert endpoint.base_url == "https://llm.example.com/v1"
    assert endpoint.api_key == "shared-key"
    assert endpoint.model == "bge-m3"
    assert endpoint.dimension == 1024
    # Dedicated EMBEDDING_* settings win.
    cfg.EMBEDDING_BASE_URL = "http://embedder:8080/v1"
    cfg.EMBEDDING_API_KEY = "emb-key"
    endpoint = resolve_embedding_endpoint("openai_compatible", cfg=cfg)
    assert endpoint.base_url == "http://embedder:8080/v1"
    assert endpoint.api_key == "emb-key"


def test_embedding_does_not_borrow_llm_key_of_a_different_provider():
    cfg = _cfg(DEFAULT_LLM_PROVIDER="groq", DEFAULT_EMBEDDING_PROVIDER="openai", LLM_API_KEY="gsk_key")
    endpoint = resolve_embedding_endpoint("openai", cfg=cfg)
    assert endpoint.api_key is None
    assert endpoint.key_source == "OPENAI_API_KEY"


def test_preset_dimension_only_applies_to_preset_default_model():
    cfg = _cfg(DEFAULT_EMBEDDING_PROVIDER="openai")
    assert resolve_embedding_endpoint("openai", cfg=cfg).dimension == 1536
    cfg.EMBEDDING_MODEL = "text-embedding-3-large"
    assert resolve_embedding_endpoint("openai", cfg=cfg).dimension is None
    cfg.EMBEDDING_DIMENSION = 3072
    assert resolve_embedding_endpoint("openai", cfg=cfg).dimension == 3072


def test_local_base_urls_do_not_require_a_key():
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://ollama:11434/v1",
                "http://10.0.0.5:8000/v1", "http://gpu.internal/v1", "http://[::1]:1234/v1"):
        assert is_local_base_url(url), url
    for url in ("https://api.openai.com/v1", "https://8.8.8.8/v1", None, ""):
        assert not is_local_base_url(url), url
    cfg = _cfg(DEFAULT_LLM_PROVIDER="openai", LLM_BASE_URL="http://localhost:8000/v1")
    assert resolve_llm_endpoint("openai", cfg=cfg).requires_key is False
    assert resolve_llm_endpoint("ollama", cfg=_cfg()).requires_key is False
    assert resolve_llm_endpoint("groq", cfg=_cfg()).requires_key is True


def test_join_url_requires_version_path_and_is_slash_safe():
    assert join_url("https://api.groq.com/openai/v1", "chat/completions") == \
        "https://api.groq.com/openai/v1/chat/completions"
    assert join_url("http://localhost:11434/v1/", "/embeddings") == "http://localhost:11434/v1/embeddings"
    provider = OpenAICompatibleLLMProvider(base_url="https://x.example/api/v1/", model="m")
    assert provider.api_url == "https://x.example/api/v1/chat/completions"


def test_task_model_resolution_follows_active_preset():
    unset = dict(LLM_TASK_CLASSIFICATION_MODEL=None, LLM_TASK_SUMMARIZATION_MODEL=None,
                 LLM_TASK_SYNTHESIS_MODEL=None, LLM_TASK_INSIGHT_MODEL=None)
    cfg = _cfg(DEFAULT_LLM_PROVIDER="groq", **unset)
    assert resolve_task_model("synthesis", cfg=cfg) == "llama-3.3-70b-versatile"
    assert resolve_task_model("classification", cfg=cfg) == "llama-3.1-8b-instant"
    cfg.LLM_MODEL = "big"
    cfg.SUMMARIZER_MODEL = "small"
    assert resolve_task_model("synthesis", cfg=cfg) == "big"
    assert resolve_task_model("insight", cfg=cfg) == "small"
    cfg.LLM_TASK_INSIGHT_MODEL = "explicit"
    assert resolve_task_model("insight", cfg=cfg) == "explicit"
    # Preset-less endpoint: only LLM_MODEL is known, every task uses it.
    custom = _cfg(DEFAULT_LLM_PROVIDER="custom", LLM_MODEL="qwen2.5", **unset)
    assert resolve_task_model("classification", cfg=custom) == "qwen2.5"
    assert resolve_task_model("synthesis", cfg=custom) == "qwen2.5"


def test_llm_provider_ready_helper():
    assert llm_provider_ready(_cfg(DEFAULT_LLM_PROVIDER="mock"))[0] is True
    assert llm_provider_ready(_cfg(DEFAULT_LLM_PROVIDER="groq"))[0] is False
    assert llm_provider_ready(_cfg(DEFAULT_LLM_PROVIDER="groq", GROQ_API_KEY="k"))[0] is True
    assert llm_provider_ready(_cfg(DEFAULT_LLM_PROVIDER="ollama"))[0] is True
    ready, detail = llm_provider_ready(_cfg(DEFAULT_LLM_PROVIDER="custom", LLM_MODEL="m"))
    assert ready is False and "base URL" in detail
    assert llm_provider_ready(
        _cfg(DEFAULT_LLM_PROVIDER="custom", LLM_BASE_URL="https://h.example/v1", LLM_MODEL="m")
    )[0] is True


# ------------------------------------------------------------------------- factory


def test_factory_returns_generic_or_legacy_classes(clean_settings, monkeypatch):
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk_x")
    assert type(get_llm_provider("groq")) is OpenAICompatibleLLMProvider
    assert type(get_llm_provider("custom")) is OpenAICompatibleLLMProvider
    assert type(get_summarizer_provider("ollama")) is OpenAICompatibleSummarizerProvider
    assert type(get_embedding_provider("ollama")) is OpenAICompatibleEmbeddingProvider
    assert isinstance(get_llm_provider("nvidia"), NvidiaLLMProvider)
    assert isinstance(get_llm_provider("openai"), OpenAILLMProvider)
    assert isinstance(get_embedding_provider("NVIDIA"), NvidiaEmbeddingProvider)
    assert isinstance(get_summarizer_provider("nvidia"), NvidiaSummarizerProvider)
    with pytest.raises(ValueError, match="Unknown LLM provider: acme"):
        get_llm_provider("acme")
    with pytest.raises(ValueError, match="Unknown Embedding provider: acme"):
        get_embedding_provider("acme")
    with pytest.raises(ValueError, match="Unknown Summarizer provider: acme"):
        get_summarizer_provider("acme")


def test_nvidia_legacy_parity(clean_settings):
    llm = NvidiaLLMProvider()
    assert llm.api_url == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert llm.model == "meta/llama-3.1-70b-instruct"
    assert NvidiaSummarizerProvider().llm.model == "meta/llama-3.1-8b-instruct"
    emb = NvidiaEmbeddingProvider()
    assert emb.api_url == "https://integrate.api.nvidia.com/v1/embeddings"
    assert emb.model == "nvidia/nv-embedcode-7b-v1"
    assert emb.provider == "nvidia"
    assert emb.dimension == 4096
    assert emb.MAX_INPUT_CHARS == 2048
    assert emb.MAX_BATCH_SIZE == 8
    assert emb.input_type == "query"
    assert NvidiaEmbeddingProvider.MAX_INPUT_CHARS == 2048
    assert NvidiaEmbeddingProvider.MAX_BATCH_SIZE == 8


def test_nvidia_legacy_model_settings_still_apply(clean_settings, monkeypatch):
    monkeypatch.setattr(settings, "NVIDIA_LLM_MODEL", "z-ai/glm-5.2")
    monkeypatch.setattr(settings, "NVIDIA_SUMMARIZER_MODEL", "meta/llama-3.3-70b-instruct")
    assert NvidiaLLMProvider().model == "z-ai/glm-5.2"
    assert NvidiaSummarizerProvider().llm.model == "meta/llama-3.3-70b-instruct"
    assert NvidiaLLMProvider(model="explicit").model == "explicit"


def test_openai_wrapper_keeps_old_defaults(clean_settings):
    assert OpenAILLMProvider().api_url == "https://api.openai.com/v1/chat/completions"
    assert OpenAILLMProvider().model == "gpt-4o"
    emb = OpenAIEmbeddingProvider()
    assert (emb.model, emb.dimension, emb.provider) == ("text-embedding-3-small", 1536, "openai")


# ---------------------------------------------------------------------- wire format


@pytest.mark.asyncio
async def test_no_authorization_header_without_key(clean_settings, transport):
    recorder = transport(_Recorder())
    provider = OpenAICompatibleLLMProvider(base_url="http://localhost:11434/v1", model="llama3.1")
    assert await provider.generate("hi") == "ok"
    request = recorder.requests[0]
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in request.headers
    assert recorder.payloads()[0]["model"] == "llama3.1"


@pytest.mark.asyncio
async def test_bearer_header_when_key_present(clean_settings, transport, monkeypatch):
    recorder = transport(_Recorder())
    monkeypatch.setattr(settings, "DEFAULT_LLM_PROVIDER", "groq")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk_secret")
    await get_llm_provider().generate("hi")
    assert recorder.requests[0].headers["authorization"] == "Bearer gsk_secret"
    assert str(recorder.requests[0].url) == "https://api.groq.com/openai/v1/chat/completions"


@pytest.mark.asyncio
async def test_missing_key_for_remote_endpoint_is_a_clear_error(clean_settings):
    provider = get_llm_provider("groq")
    with pytest.raises(ValueError, match="Groq API key is not configured. Set GROQ_API_KEY"):
        await provider.generate("hi")
    custom = OpenAICompatibleLLMProvider(base_url=None, model="m")
    with pytest.raises(ValueError, match="base URL is not configured"):
        await custom.generate("hi")


@pytest.mark.asyncio
async def test_nvidia_embedding_wire_parity(clean_settings, transport, monkeypatch):
    recorder = transport(_Recorder(width=4096))
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "nvapi-test")
    provider = NvidiaEmbeddingProvider()
    vectors = await provider.embed_batch(["x" * 5000, "short"])
    assert [v[0] for v in vectors] == [0.0, 1.0]  # sorted by index
    payload = recorder.payloads()[0]
    assert payload["model"] == "nvidia/nv-embedcode-7b-v1"
    assert payload["input_type"] == "query"
    assert len(payload["input"][0]) == 2048  # capped
    assert str(recorder.requests[0].url) == "https://integrate.api.nvidia.com/v1/embeddings"
    # A caller-supplied input_type wins.
    await provider.embed_batch(["a"], input_type="passage")
    assert recorder.payloads()[1]["input_type"] == "passage"


@pytest.mark.asyncio
async def test_generic_embedding_strips_input_type_and_chunks_batches(clean_settings, transport):
    recorder = transport(_Recorder(width=3))
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://localhost:11434/v1", model="nomic-embed-text", dimension=3, max_batch_size=8,
        extra_payload={"encoding_format": "float"},
    )
    vectors = await provider.embed_batch([f"t{i}" for i in range(20)], input_type="passage")
    assert len(vectors) == 20
    assert [len(p["input"]) for p in recorder.payloads()] == [8, 8, 4]
    assert all("input_type" not in p for p in recorder.payloads())
    assert all(p["encoding_format"] == "float" for p in recorder.payloads())
    # Order is preserved across chunks.
    assert [v[0] for v in vectors[:9]] == [0, 1, 2, 3, 4, 5, 6, 7, 0]


@pytest.mark.asyncio
async def test_embedding_batch_size_and_char_cap_settings(clean_settings, transport, monkeypatch):
    recorder = transport(_Recorder(width=768))
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "EMBEDDING_BATCH_SIZE", 2)
    monkeypatch.setattr(settings, "EMBEDDING_MAX_INPUT_CHARS", 10)
    provider = get_embedding_provider()
    assert provider.MAX_BATCH_SIZE == 2 and provider.MAX_INPUT_CHARS == 10
    await provider.embed_batch(["a" * 50, "b", "c"])
    assert [len(p["input"]) for p in recorder.payloads()] == [2, 1]
    assert recorder.payloads()[0]["input"][0] == "a" * 10


@pytest.mark.asyncio
async def test_embedding_retries_honour_retry_after(clean_settings, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("brain.llm.providers.openai_compatible.asyncio.sleep", sleep)
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]}),
    ])
    mock = httpx.MockTransport(lambda request: next(responses))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _REAL_ASYNC_CLIENT(transport=mock))
    provider = OpenAICompatibleEmbeddingProvider(base_url="http://localhost/v1", model="m", dimension=2)
    assert await provider.embed_batch(["a"]) == [[1.0, 2.0]]
    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_embedding_index_mismatch_raises(clean_settings, monkeypatch):
    mock = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _REAL_ASYNC_CLIENT(transport=mock))
    provider = OpenAICompatibleEmbeddingProvider(base_url="http://localhost/v1", model="m", dimension=1,
                                                 label="Ollama")
    with pytest.raises(ValueError, match="Ollama embedding response cardinality/index mismatch"):
        await provider.embed_batch(["a", "b"])


# ------------------------------------------------------------------ dimension safety


@pytest.mark.asyncio
async def test_dimension_mismatch_fails_loudly(clean_settings, transport):
    transport(_Recorder(width=768))
    provider = OpenAICompatibleEmbeddingProvider(base_url="http://localhost/v1", model="m", dimension=4096)
    with pytest.raises(EmbeddingDimensionMismatchError, match="returned 768-dim vectors .* 4096"):
        await provider.embed_batch(["a"])


@pytest.mark.asyncio
async def test_unknown_dimension_refuses_to_embed(clean_settings):
    provider = OpenAICompatibleEmbeddingProvider(base_url="http://localhost/v1", model="custom-embed")
    with pytest.raises(ValueError, match="Set EMBEDDING_DIMENSION"):
        await provider.embed_batch(["a"])


def test_strict_dimension_resolution(clean_settings, monkeypatch):
    from brain.embeddings.constants import EmbeddingDimensionUnknownError, resolve_embedding_dimension

    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "nvidia")
    assert resolve_embedding_dimension(strict=True) == 4096
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "custom")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "bge-m3")
    with pytest.raises(EmbeddingDimensionUnknownError, match="EMBEDDING_DIMENSION"):
        resolve_embedding_dimension(strict=True)
    assert resolve_embedding_dimension() == 1536  # import-time fallback only
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 1024)
    assert resolve_embedding_dimension(strict=True) == 1024
    assert resolve_embedding_dimension("mock") == 1024


# --------------------------------------------------------------------- doctor / check

runner = CliRunner()


def _doctor(tmp_path, **overrides):
    cfg = _cfg(
        ENVIRONMENT="local",
        TARGET_REPO_PATH=str(tmp_path),
        CONTEXT_PACK_OUTPUT_DIR=str(tmp_path / "context_packs"),
        REPORT_OUTPUT_DIR=str(tmp_path / "reports"),
        PROJECT_BRAIN_API_KEY="test-key",
        ANTHROPIC_API_KEY=None,
        GOOGLE_API_KEY=None,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    healthy = {s: {"status": "healthy"} for s in ("postgres", "redis", "neo4j")}
    with patch.object(doctor_module, "settings", cfg), patch.object(
        doctor_module, "check_health", AsyncMock(return_value=healthy)
    ):
        return runner.invoke(cli, ["doctor"])


def test_doctor_keyless_local_endpoint_passes(tmp_path):
    result = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="ollama", DEFAULT_EMBEDDING_PROVIDER="ollama")
    assert result.exit_code == 0, result.output
    assert "localhost:11434" in result.output
    assert "no API key" in result.output


def test_doctor_openai_compatible_requires_base_url_and_model(tmp_path):
    result = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="custom")
    assert result.exit_code == 1
    assert "needs LLM_BASE_URL" in result.output
    assert "set LLM_MODEL" in result.output
    ok = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="custom",
                 LLM_BASE_URL="http://vllm:8000/v1", LLM_MODEL="qwen2.5")
    assert ok.exit_code == 0, ok.output


def test_doctor_remote_preset_requires_its_key(tmp_path):
    result = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="groq")
    assert result.exit_code == 1
    assert "GROQ_API_KEY is not set" in result.output
    ok = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="groq", LLM_API_KEY="gsk_whatever")
    assert ok.exit_code == 0, ok.output
    assert "gsk_whatever" not in ok.output  # never prints secrets


def test_doctor_nvapi_prefix_only_for_nvidia(tmp_path):
    bad = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="nvidia", NVIDIA_API_KEY="sk-wrong")
    assert bad.exit_code == 1
    assert "expected nvapi-" in bad.output
    good = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="nvidia", DEFAULT_EMBEDDING_PROVIDER="nvidia",
                   NVIDIA_API_KEY="nvapi-ok")
    assert good.exit_code == 0, good.output
    assert "4096 dims" in good.output
    other = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="no-prefix")
    assert other.exit_code == 0, other.output


def test_doctor_flags_embedding_dimension_problems(tmp_path):
    unknown = _doctor(tmp_path, DEFAULT_EMBEDDING_PROVIDER="custom",
                      EMBEDDING_BASE_URL="http://tei:8080/v1", EMBEDDING_MODEL="bge-m3")
    assert unknown.exit_code == 1
    assert "embedding dimension unknown" in unknown.output
    conflict = _doctor(tmp_path, DEFAULT_EMBEDDING_PROVIDER="nvidia", NVIDIA_API_KEY="nvapi-ok",
                       EMBEDDING_DIMENSION=1536)
    assert conflict.exit_code == 1
    assert "EMBEDDING_DIMENSION=1536 conflicts" in conflict.output


def test_doctor_unknown_provider_lists_presets(tmp_path):
    result = _doctor(tmp_path, DEFAULT_LLM_PROVIDER="acme")
    assert result.exit_code == 1
    assert "unknown LLM provider 'acme'" in result.output
    assert "openai_compatible" in result.output and "groq" in result.output


def test_provider_check_states(clean_settings, tmp_path, monkeypatch):
    from brain.onboarding import provider_check, setup_state

    monkeypatch.setattr(setup_state, "load_state", lambda: {})
    state = provider_check.provider_state("ollama", "llm")
    assert state["state"] == "configured"
    assert "keyless" in state["detail"]
    missing = provider_check.provider_state("groq", "llm")
    assert missing == {"provider": "groq", "state": "missing", "detail": "GROQ_API_KEY is not set"}
    assert provider_check.provider_state("custom", "llm")["detail"] == "LLM_BASE_URL is not set"
    assert provider_check.provider_state("acme", "llm")["detail"] == "unsupported llm provider"
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "nvapi-secret")
    configured = provider_check.provider_state("nvidia", "embedding")
    assert configured["state"] == "configured"
    assert "NVIDIA_API_KEY" in configured["detail"]
    assert "nvapi-secret" not in json.dumps(configured)


def test_router_status_reports_preset_and_host_without_secrets(clean_settings, monkeypatch):
    from brain.llm.router import ModelRouter

    monkeypatch.setattr(settings, "DEFAULT_LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or-secret")
    summary = ModelRouter().dashboard_summary()
    assert summary["llm_endpoint"]["preset"] == "openrouter"
    assert summary["llm_endpoint"]["base_url_host"] == "openrouter.ai"
    assert summary["embedding_endpoint"] == {
        "preset": "ollama", "base_url_host": "localhost:11434", "model": "nomic-embed-text",
    }
    assert summary["embedding_dimension"] == 768
    assert summary["api_keys"]["active_llm"] == "configured"
    assert summary["api_keys"]["active_embedding"] == "not_required"
    assert summary["api_keys"]["openrouter"] == "configured"
    assert "nvidia" in summary["api_keys"]
    assert "sk-or-secret" not in json.dumps(summary)
