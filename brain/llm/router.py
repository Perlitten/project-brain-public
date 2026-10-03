from enum import Enum
from functools import lru_cache
from typing import Literal, Optional

from brain.config.settings import settings
from brain.llm.factory import create_llm_provider, create_summarizer_provider
from brain.llm.presets import (
    PRESETS,
    is_openai_compatible,
    normalize_provider_name,
    resolve_embedding_endpoint,
    resolve_llm_endpoint,
    resolve_task_model,
)
from brain.llm.providers.base import EmbeddingProvider, LLMProvider, SummarizerProvider


class TaskKind(str, Enum):
    CLASSIFICATION = "classification"
    SUMMARIZATION = "summarization"
    SYNTHESIS = "synthesis"
    INSIGHT = "insight"


# Per-task overrides; when unset the model comes from the active preset
# (see brain.llm.presets.resolve_task_model).
_TASK_MODEL_ATTR = {
    TaskKind.CLASSIFICATION: "LLM_TASK_CLASSIFICATION_MODEL",
    TaskKind.SUMMARIZATION: "LLM_TASK_SUMMARIZATION_MODEL",
    TaskKind.SYNTHESIS: "LLM_TASK_SYNTHESIS_MODEL",
    TaskKind.INSIGHT: "LLM_TASK_INSIGHT_MODEL",
}


class ModelRouter:
    """Routes LLM, embedding, and summarizer calls by task kind and configured provider."""

    def __init__(self) -> None:
        self._provider_name = normalize_provider_name(settings.DEFAULT_LLM_PROVIDER)
        self._embedding_name = normalize_provider_name(settings.DEFAULT_EMBEDDING_PROVIDER)
        self._llm_cache: dict[tuple[str, str], LLMProvider] = {}
        self._summarizer: Optional[SummarizerProvider] = None
        self._embedding: Optional[EmbeddingProvider] = None

    def task_model(self, task: TaskKind) -> str:
        return resolve_task_model(TaskKind(task).value, self._provider_name)

    def _create_llm(self, model: str) -> LLMProvider:
        return create_llm_provider(self._provider_name, model=model or None)

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
            model = self.task_model(TaskKind.SUMMARIZATION)
            self._summarizer = create_summarizer_provider(self._provider_name, model=model or None)
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
    def _endpoint_status(name: str, kind: str) -> str:
        """configured / missing / not_required for the active provider (no secrets)."""
        if not is_openai_compatible(name):
            key_attr = {"anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY"}.get(name)
            if key_attr is None:
                return "not_required"
            return "configured" if getattr(settings, key_attr, None) else "missing"
        endpoint = (
            resolve_embedding_endpoint(name) if kind == "embedding" else resolve_llm_endpoint(name)
        )
        if endpoint.api_key:
            return "configured"
        return "missing" if endpoint.requires_key else "not_required"

    @staticmethod
    def api_key_status() -> dict[str, str]:
        """Return configured/missing status for each provider API key (no secret values).

        ``active_llm`` / ``active_embedding`` describe the key of the providers
        actually selected (``not_required`` for mock and keyless local endpoints).
        """

        def _status(key: Optional[str]) -> str:
            return "configured" if key else "missing"

        status = {
            "openai": _status(settings.OPENAI_API_KEY),
            "anthropic": _status(settings.ANTHROPIC_API_KEY),
            "google": _status(settings.GOOGLE_API_KEY),
            "nvidia": _status(settings.NVIDIA_API_KEY),
        }
        for preset in PRESETS.values():
            if preset.key_env and preset.name not in status:
                status[preset.name] = _status(getattr(settings, preset.key_env, None))
        status["active_llm"] = ModelRouter._endpoint_status(
            normalize_provider_name(settings.DEFAULT_LLM_PROVIDER), "llm"
        )
        status["active_embedding"] = ModelRouter._endpoint_status(
            normalize_provider_name(settings.DEFAULT_EMBEDDING_PROVIDER), "embedding"
        )
        return status

    @staticmethod
    def _endpoint_summary(name: str, kind: str, model: Optional[str]) -> dict:
        """Preset + base-URL host + model of an endpoint — never the key or full URL."""
        if not is_openai_compatible(name):
            return {"preset": None, "base_url_host": None, "model": model}
        endpoint = (
            resolve_embedding_endpoint(name) if kind == "embedding" else resolve_llm_endpoint(name)
        )
        return {
            "preset": endpoint.provider,
            "base_url_host": endpoint.base_url_host,
            "model": model or endpoint.model,
        }

    def dashboard_summary(self) -> dict:
        emb = self.embedding("query")
        embedding_model = getattr(emb, "model", None)
        return {
            "llm_provider": self._provider_name,
            "embedding_provider": self._embedding_name,
            "embedding_model": embedding_model or "unknown",
            "embedding_dimension": getattr(emb, "dimension", None),
            "llm_endpoint": self._endpoint_summary(
                self._provider_name, "llm", self.task_model(TaskKind.SYNTHESIS) or None
            ),
            "embedding_endpoint": self._endpoint_summary(self._embedding_name, "embedding", embedding_model),
            "routing_table": self.routing_table(),
            "api_keys": self.api_key_status(),
        }


@lru_cache(maxsize=1)
def get_model_router() -> ModelRouter:
    return ModelRouter()
