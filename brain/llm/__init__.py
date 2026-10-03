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
from brain.llm.providers.openai_compatible import (
    OpenAICompatibleLLMProvider,
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleSummarizerProvider
)
from brain.llm.factory import (
    create_llm_provider,
    create_embedding_provider,
    create_summarizer_provider
)
from brain.llm.router import ModelRouter, TaskKind, get_model_router

def get_llm_provider(provider_name: Optional[str] = None) -> LLMProvider:
    """Returns the configured LLMProvider.

    Args:
        provider_name: Optional override for the provider name: 'mock', 'anthropic',
            'google', or any OpenAI-compatible preset ('openai', 'nvidia',
            'openrouter', 'groq', 'together', 'deepseek', 'mistral', 'ollama',
            'lmstudio', 'openai_compatible' / 'custom').

    Returns:
        An instance of LLMProvider.
    """
    return create_llm_provider(provider_name or settings.DEFAULT_LLM_PROVIDER)


def get_embedding_provider(provider_name: Optional[str] = None) -> EmbeddingProvider:
    """Returns the configured EmbeddingProvider (see :func:`get_llm_provider` for names)."""
    return create_embedding_provider(provider_name or settings.DEFAULT_EMBEDDING_PROVIDER)


def get_summarizer_provider(provider_name: Optional[str] = None) -> SummarizerProvider:
    """Returns the configured SummarizerProvider (see :func:`get_llm_provider` for names)."""
    return create_summarizer_provider(provider_name or settings.DEFAULT_LLM_PROVIDER)


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
    "MockLLMProvider",
    "MockEmbeddingProvider",
    "MockSummarizerProvider",
    "OpenAILLMProvider",
    "OpenAIEmbeddingProvider",
    "OpenAISummarizerProvider",
    "AnthropicLLMProvider",
    "AnthropicEmbeddingProvider",
    "AnthropicSummarizerProvider",
    "GoogleLLMProvider",
    "GoogleEmbeddingProvider",
    "GoogleSummarizerProvider",
    "NvidiaLLMProvider",
    "NvidiaEmbeddingProvider",
    "NvidiaSummarizerProvider",
    "OpenAICompatibleLLMProvider",
    "OpenAICompatibleEmbeddingProvider",
    "OpenAICompatibleSummarizerProvider",
    "create_llm_provider",
    "create_embedding_provider",
    "create_summarizer_provider",
]

