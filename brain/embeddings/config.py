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
    from brain.llm import get_embedding_provider

    name = resolve_provider_name(provider_name)
    provider = get_embedding_provider(name)
    model = getattr(provider, "model", name)
    dimension = getattr(provider, "dimension", None) or resolve_embedding_dimension(name)
    return EmbeddingConfig(provider=name, model=model, dimension=dimension)


def content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
