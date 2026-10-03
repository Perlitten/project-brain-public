"""OpenAI API providers — thin wrappers over the generic OpenAI-compatible client.

Defaults come from the ``openai`` preset (:mod:`brain.llm.presets`); the class
names and ``(api_key=None, model=None)`` signatures are kept for backward
compatibility. An explicit ``api_key`` (even ``""``) overrides the configured
key; ``None`` means "use settings".
"""

from typing import Any, Dict, Optional

from brain.llm.presets import (
    ResolvedEndpoint,
    resolve_embedding_endpoint,
    resolve_llm_endpoint,
    resolve_summarizer_endpoint,
)
from brain.llm.providers.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleLLMProvider,
    OpenAICompatibleSummarizerProvider,
)


def _key_override(api_key: Optional[str]) -> Dict[str, Any]:
    return {} if api_key is None else {"api_key": api_key}


def _llm_params(endpoint: ResolvedEndpoint, api_key: Optional[str]) -> Dict[str, Any]:
    params: Dict[str, Any] = dict(
        base_url=endpoint.base_url,
        model=endpoint.model,
        api_key=endpoint.api_key,
        provider=endpoint.provider,
        label=endpoint.label,
        require_key=endpoint.requires_key,
        key_source=endpoint.key_source,
    )
    params.update(_key_override(api_key))
    return params


class OpenAILLMProvider(OpenAICompatibleLLMProvider):
    """OpenAI chat-completions provider (``openai`` preset)."""

    PRESET = "openai"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        endpoint = resolve_llm_endpoint(self.PRESET, model=model)
        super().__init__(**_llm_params(endpoint, api_key))


class OpenAIEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    """OpenAI embeddings provider (``openai`` preset)."""

    PRESET = "openai"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        endpoint = resolve_embedding_endpoint(self.PRESET)
        params: Dict[str, Any] = dict(
            base_url=endpoint.base_url,
            model=model or endpoint.model,
            api_key=endpoint.api_key,
            dimension=endpoint.dimension,
            max_input_chars=endpoint.max_input_chars,
            max_batch_size=endpoint.max_batch_size,
            input_type=endpoint.input_type,
            provider=endpoint.provider,
            label=endpoint.label,
            require_key=endpoint.requires_key,
            key_source=endpoint.key_source,
        )
        params.update(_key_override(api_key))
        super().__init__(**params)


class OpenAISummarizerProvider(OpenAICompatibleSummarizerProvider):
    """OpenAI summarizer (``openai`` preset, ``gpt-4o-mini`` by default)."""

    PRESET = "openai"
    LLM_CLASS = OpenAILLMProvider

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        endpoint = resolve_summarizer_endpoint(self.PRESET, model=model)
        llm = self.LLM_CLASS(api_key=api_key, model=endpoint.model)
        super().__init__(llm)
