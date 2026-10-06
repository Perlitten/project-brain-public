"""Embedding dimension constants without database imports."""

from __future__ import annotations

from typing import Optional

from brain.config.settings import settings

_PROVIDER_DIMENSIONS: dict[str, int] = {
    "mock": 1536,
    "openai": 1536,
    "anthropic": 1536,
    "google": 768,
    "nvidia": 4096,
}


class EmbeddingDimensionUnknownError(ValueError):
    """The active embedding model's output width cannot be determined."""


def resolve_provider_name(provider_name: Optional[str] = None) -> str:
    from brain.llm.presets import normalize_provider_name

    return normalize_provider_name(provider_name or settings.DEFAULT_EMBEDDING_PROVIDER)


def resolve_embedding_dimension(provider_name: Optional[str] = None, *, strict: bool = False) -> int:
    """Configured embedding width.

    ``EMBEDDING_DIMENSION`` (> 0) wins; native providers use the fixed map;
    OpenAI-compatible presets use their default model's width. A model the
    preset does not know (custom endpoint, non-default ``EMBEDDING_MODEL``) has
    no implicit width: ``strict=True`` raises (used by migrations so a
    misconfiguration can never rebuild the pgvector column), otherwise the
    historical 1536 fallback is returned for import-time model declarations.
    """
    if settings.EMBEDDING_DIMENSION > 0:
        return settings.EMBEDDING_DIMENSION
    name = resolve_provider_name(provider_name)
    if name in _PROVIDER_DIMENSIONS and name not in ("openai", "nvidia"):
        return _PROVIDER_DIMENSIONS[name]

    from brain.llm.presets import get_preset, resolve_embedding_endpoint

    if get_preset(name) is not None:
        endpoint = resolve_embedding_endpoint(name)
        if endpoint.dimension:
            return endpoint.dimension
        if strict:
            raise EmbeddingDimensionUnknownError(
                f"Embedding dimension is unknown for provider '{name}' model "
                f"'{endpoint.model or '<unset>'}'. Set EMBEDDING_DIMENSION to the model's "
                "output width (changing it rebuilds the vector column; re-index afterwards)."
            )
        # Historical default: kept for backwards compatibility with pre-2.0 indexes.
        return 1536
    if strict:
        raise EmbeddingDimensionUnknownError(f"Unknown embedding provider '{name}'.")
    return 1536


EMBEDDING_DIMENSION = resolve_embedding_dimension()


class VectorSearchStatus:
    OK = "OK"
    DEGRADED = "DEGRADED"
    DIMENSION_MISMATCH = "DIMENSION_MISMATCH"
    PGVECTOR_UNAVAILABLE = "PGVECTOR_UNAVAILABLE"
