"""Single provider resolver shared by ``brain.llm.get_*_provider`` and the
:class:`~brain.llm.router.ModelRouter`.

* ``mock`` / ``anthropic`` / ``google`` — native adapters, unchanged.
* ``openai`` / ``nvidia`` — preset wrapper classes (names kept for compatibility).
* every other preset (``openrouter``, ``groq``, ``ollama``…) and
  ``openai_compatible`` (aliases ``custom``, ``openai-compatible``) — the
  generic OpenAI-compatible client configured from :mod:`brain.llm.presets`.
"""

from __future__ import annotations

from typing import Optional

from brain.llm.presets import (
    normalize_provider_name,
    resolve_embedding_endpoint,
    resolve_llm_endpoint,
    resolve_summarizer_endpoint,
)
from brain.llm.providers.base import EmbeddingProvider, LLMProvider, SummarizerProvider


def _native_llm(name: str, model: Optional[str]) -> Optional[LLMProvider]:
    if name == "mock":
        from brain.llm.providers.mock_provider import MockLLMProvider

        return MockLLMProvider()
    if name == "anthropic":
        from brain.llm.providers.anthropic_provider import AnthropicLLMProvider

        return AnthropicLLMProvider(model=model) if model else AnthropicLLMProvider()
    if name == "google":
        from brain.llm.providers.google_provider import GoogleLLMProvider

        return GoogleLLMProvider(model=model) if model else GoogleLLMProvider()
    return None


def create_llm_provider(provider_name: str, model: Optional[str] = None) -> LLMProvider:
    name = normalize_provider_name(provider_name)
    native = _native_llm(name, model)
    if native is not None:
        return native
    if name == "nvidia":
        from brain.llm.providers.nvidia_provider import NvidiaLLMProvider

        return NvidiaLLMProvider(model=model)
    if name == "openai":
        from brain.llm.providers.openai_provider import OpenAILLMProvider

        return OpenAILLMProvider(model=model)
    try:
        endpoint = resolve_llm_endpoint(name, model=model)
    except ValueError:
        raise ValueError(f"Unknown LLM provider: {name}") from None
    from brain.llm.providers.openai_compatible import OpenAICompatibleLLMProvider

    return OpenAICompatibleLLMProvider.from_endpoint(endpoint)


def create_embedding_provider(provider_name: str) -> EmbeddingProvider:
    name = normalize_provider_name(provider_name)
    if name == "mock":
        from brain.llm.providers.mock_provider import MockEmbeddingProvider

        return MockEmbeddingProvider()
    if name == "anthropic":
        from brain.llm.providers.anthropic_provider import AnthropicEmbeddingProvider

        return AnthropicEmbeddingProvider()
    if name == "google":
        from brain.llm.providers.google_provider import GoogleEmbeddingProvider

        return GoogleEmbeddingProvider()
    if name == "nvidia":
        from brain.llm.providers.nvidia_provider import NvidiaEmbeddingProvider

        return NvidiaEmbeddingProvider()
    if name == "openai":
        from brain.llm.providers.openai_provider import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider()
    try:
        endpoint = resolve_embedding_endpoint(name)
    except ValueError:
        raise ValueError(f"Unknown Embedding provider: {name}") from None
    from brain.llm.providers.openai_compatible import OpenAICompatibleEmbeddingProvider

    return OpenAICompatibleEmbeddingProvider.from_endpoint(endpoint)


def create_summarizer_provider(provider_name: str, model: Optional[str] = None) -> SummarizerProvider:
    name = normalize_provider_name(provider_name)
    if name == "mock":
        from brain.llm.providers.mock_provider import MockSummarizerProvider

        return MockSummarizerProvider()
    if name == "anthropic":
        from brain.llm.providers.anthropic_provider import AnthropicSummarizerProvider

        return AnthropicSummarizerProvider(model=model) if model else AnthropicSummarizerProvider()
    if name == "google":
        from brain.llm.providers.google_provider import GoogleSummarizerProvider

        return GoogleSummarizerProvider(model=model) if model else GoogleSummarizerProvider()
    if name == "nvidia":
        from brain.llm.providers.nvidia_provider import NvidiaSummarizerProvider

        return NvidiaSummarizerProvider(model=model)
    if name == "openai":
        from brain.llm.providers.openai_provider import OpenAISummarizerProvider

        return OpenAISummarizerProvider(model=model)
    try:
        endpoint = resolve_summarizer_endpoint(name, model=model)
    except ValueError:
        raise ValueError(f"Unknown Summarizer provider: {name}") from None
    from brain.llm.providers.openai_compatible import OpenAICompatibleSummarizerProvider

    return OpenAICompatibleSummarizerProvider.from_endpoint(endpoint)
