"""Configured embedding provider metadata and dimension resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from brain.embeddings.constants import resolve_embedding_dimension, resolve_provider_name


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    dimension: int

    def as_metadata(self) -> dict[str, str | int]:
        return {
            "provider": self.provider,
            "model": self.model,
            "dimension": self.dimension,
        }


def get_embedding_config(provider_name: Optional[str] = None) -> EmbeddingConfig:
    from brain.llm.call_budget import provider_calls_forbidden
    if provider_calls_forbidden():
        # Resolve configured provenance without constructing a client/model.
        name = resolve_provider_name(provider_name)
        if name == "mock":
            return EmbeddingConfig(name, "mock-deterministic", resolve_embedding_dimension(name, strict=True))
        from brain.llm.presets import is_openai_compatible, resolve_embedding_endpoint
        if not is_openai_compatible(name):
            raise ValueError("No client-free embedding metadata resolver for this provider")
        endpoint = resolve_embedding_endpoint(name)
        if not endpoint.model:
            raise ValueError("Embedding model is not configured")
        return EmbeddingConfig(name, endpoint.model, resolve_embedding_dimension(name, strict=True))
    from brain.llm import get_embedding_provider

    name = resolve_provider_name(provider_name)
    provider = get_embedding_provider(name)
    model = getattr(provider, "model", name)
    dimension = getattr(provider, "dimension", None) or resolve_embedding_dimension(name, strict=True)
    return EmbeddingConfig(provider=name, model=model, dimension=dimension)


def content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
