from enum import Enum
from functools import lru_cache
from typing import Literal, Optional

from brain.config.settings import settings
from brain.llm.providers.base import EmbeddingProvider, LLMProvider, SummarizerProvider


class TaskKind(str, Enum):
    CLASSIFICATION = "classification"
    SUMMARIZATION = "summarization"
    SYNTHESIS = "synthesis"
    INSIGHT = "insight"


_TASK_MODEL_ATTR = {
    TaskKind.CLASSIFICATION: "LLM_TASK_CLASSIFICATION_MODEL",
    TaskKind.SUMMARIZATION: "LLM_TASK_SUMMARIZATION_MODEL",
    TaskKind.SYNTHESIS: "LLM_TASK_SYNTHESIS_MODEL",
    TaskKind.INSIGHT: "LLM_TASK_INSIGHT_MODEL",
}


class ModelRouter:
    """Routes LLM, embedding, and summarizer calls by task kind and configured provider."""

    def __init__(self) -> None:
        self._provider_name = settings.DEFAULT_LLM_PROVIDER.lower()
        self._embedding_name = settings.DEFAULT_EMBEDDING_PROVIDER.lower()
        self._llm_cache: dict[tuple[str, str], LLMProvider] = {}
        self._summarizer: Optional[SummarizerProvider] = None
        self._embedding: Optional[EmbeddingProvider] = None

    def task_model(self, task: TaskKind) -> str:
        return getattr(settings, _TASK_MODEL_ATTR[task])

    def _create_llm(self, model: str) -> LLMProvider:
        from brain.llm import get_llm_provider

        name = self._provider_name
        if name == "mock":
            return get_llm_provider("mock")
        if name == "nvidia":
            from brain.llm.providers.nvidia_provider import NvidiaLLMProvider

            return NvidiaLLMProvider(model=model)
        if name == "openai":
            from brain.llm.providers.openai_provider import OpenAILLMProvider

            return OpenAILLMProvider(model=model)
        if name == "anthropic":
            from brain.llm.providers.anthropic_provider import AnthropicLLMProvider

            return AnthropicLLMProvider(model=model)
        if name == "google":
            from brain.llm.providers.google_provider import GoogleLLMProvider

            return GoogleLLMProvider(model=model)
        return get_llm_provider(name)

    def llm(self, task: TaskKind) -> LLMProvider:
        model = self.task_model(task)
        if self._provider_name == "mock":
            key = ("mock", "__singleton__")
        else:
            key = (self._provider_name, model)
        if key not in self._llm_cache:
            self._llm_cache[key] = self._create_llm(model)
        return self._llm_cache[key]

    def embedding(self, input_type: Literal["query", "passage"]) -> EmbeddingProvider:
        del input_type  # callers pass input_type to embed(); provider is shared
        if self._embedding is None:
            from brain.llm import get_embedding_provider

            self._embedding = get_embedding_provider()
        return self._embedding

    def summarizer(self) -> SummarizerProvider:
        if self._summarizer is None:
            name = self._provider_name
            model = self.task_model(TaskKind.SUMMARIZATION)
            if name == "mock":
                from brain.llm import get_summarizer_provider

                self._summarizer = get_summarizer_provider("mock")
            elif name == "nvidia":
                from brain.llm.providers.nvidia_provider import NvidiaSummarizerProvider

                self._summarizer = NvidiaSummarizerProvider(model=model)
            elif name == "openai":
                from brain.llm.providers.openai_provider import OpenAISummarizerProvider

                self._summarizer = OpenAISummarizerProvider(model=model)
            elif name == "anthropic":
                from brain.llm.providers.anthropic_provider import AnthropicSummarizerProvider

                self._summarizer = AnthropicSummarizerProvider(model=model)
            elif name == "google":
                from brain.llm.providers.google_provider import GoogleSummarizerProvider

                self._summarizer = GoogleSummarizerProvider(model=model)
            else:
                from brain.llm import get_summarizer_provider

                self._summarizer = get_summarizer_provider(name)
        return self._summarizer

    def routing_table(self) -> list[dict[str, str]]:
        return [
            {"task_kind": task.value, "model": self.task_model(task)}
            for task in TaskKind
        ]

    def embedding_info(self) -> dict[str, str]:
        provider = self.embedding("query")
        return {
            "provider": self._embedding_name,
            "model": getattr(provider, "model", "unknown"),
        }

    @staticmethod
    def api_key_status() -> dict[str, str]:
        """Return configured/missing status for each provider API key (no secret values)."""

        def _status(key: Optional[str]) -> str:
            return "configured" if key else "missing"

        return {
            "openai": _status(settings.OPENAI_API_KEY),
            "anthropic": _status(settings.ANTHROPIC_API_KEY),
            "google": _status(settings.GOOGLE_API_KEY),
            "nvidia": _status(settings.NVIDIA_API_KEY),
        }

    def dashboard_summary(self) -> dict:
        emb = self.embedding("query")
        return {
            "llm_provider": self._provider_name,
            "embedding_provider": self._embedding_name,
            "embedding_model": getattr(emb, "model", settings.NVIDIA_EMBEDDING_MODEL),
            "routing_table": self.routing_table(),
            "api_keys": self.api_key_status(),
        }


@lru_cache(maxsize=1)
def get_model_router() -> ModelRouter:
    return ModelRouter()
