from typing import Optional
from brain.config.settings import settings
from brain.llm.providers.base import LLMProvider, EmbeddingProvider, SummarizerProvider
from brain.llm.providers.mock_provider import (
    MockLLMProvider,
    MockEmbeddingProvider,
    MockSummarizerProvider
)
from brain.llm.providers.openai_provider import (
    OpenAILLMProvider,
    OpenAIEmbeddingProvider,
    OpenAISummarizerProvider
)
from brain.llm.providers.anthropic_provider import (
    AnthropicLLMProvider,
    AnthropicEmbeddingProvider,
    AnthropicSummarizerProvider
)
from brain.llm.providers.google_provider import (
    GoogleLLMProvider,
    GoogleEmbeddingProvider,
    GoogleSummarizerProvider
)
from brain.llm.providers.nvidia_provider import (
    NvidiaLLMProvider,
    NvidiaEmbeddingProvider,
    NvidiaSummarizerProvider
)
from brain.llm.router import ModelRouter, TaskKind, get_model_router

def get_llm_provider(provider_name: Optional[str] = None) -> LLMProvider:
    """Returns the configured LLMProvider.

    Args:
        provider_name: Optional override for the provider name (e.g. 'openai', 'anthropic', 'google', 'nvidia', 'mock').

    Returns:
        An instance of LLMProvider.
    """
    name = (provider_name or settings.DEFAULT_LLM_PROVIDER).lower()
    if name == "mock":
        return MockLLMProvider()
    elif name == "openai":
        return OpenAILLMProvider()
    elif name == "anthropic":
        return AnthropicLLMProvider()
    elif name == "google":
        return GoogleLLMProvider()
    elif name == "nvidia":
        return NvidiaLLMProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {name}")


def get_embedding_provider(provider_name: Optional[str] = None) -> EmbeddingProvider:
    """Returns the configured EmbeddingProvider.

    Args:
        provider_name: Optional override for the provider name (e.g. 'openai', 'google', 'nvidia', 'mock').

    Returns:
        An instance of EmbeddingProvider.
    """
    name = (provider_name or settings.DEFAULT_EMBEDDING_PROVIDER).lower()
    if name == "mock":
        return MockEmbeddingProvider()
    elif name == "openai":
        return OpenAIEmbeddingProvider()
    elif name == "anthropic":
        return AnthropicEmbeddingProvider()
    elif name == "google":
        return GoogleEmbeddingProvider()
    elif name == "nvidia":
        return NvidiaEmbeddingProvider()
    else:
        raise ValueError(f"Unknown Embedding provider: {name}")


def get_summarizer_provider(provider_name: Optional[str] = None) -> SummarizerProvider:
    """Returns the configured SummarizerProvider.

    Args:
        provider_name: Optional override for the provider name (e.g. 'openai', 'anthropic', 'google', 'nvidia', 'mock').

    Returns:
        An instance of SummarizerProvider.
    """
    name = (provider_name or settings.DEFAULT_LLM_PROVIDER).lower()
    if name == "mock":
        return MockSummarizerProvider()
    elif name == "openai":
        return OpenAISummarizerProvider()
    elif name == "anthropic":
        return AnthropicSummarizerProvider()
    elif name == "google":
        return GoogleSummarizerProvider()
    elif name == "nvidia":
        return NvidiaSummarizerProvider()
    else:
        raise ValueError(f"Unknown Summarizer provider: {name}")


__all__ = [
    "get_llm_provider",
    "get_embedding_provider",
    "get_summarizer_provider",
    "get_model_router",
    "ModelRouter",
    "TaskKind",
    "LLMProvider",
    "EmbeddingProvider",
    "SummarizerProvider",
]

