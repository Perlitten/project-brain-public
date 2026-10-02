"""Retrieval v6 P3 — pluggable card embedding provider.

Cards are embedded through a dedicated provider abstraction so a future P3.5 can
A/B a different embedding model for cards WITHOUT touching chunk embeddings or the
retrieval code. In P3 the default provider inherits the configured embedding
provider (nv-embedcode); only the *representation* (card_text) is the new variable,
not the embedding model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from brain.config.settings import settings
from brain.embeddings.config import EmbeddingConfig, get_embedding_config
from brain.llm import get_embedding_provider


@dataclass(frozen=True)
class CardEmbeddingProvider:
    """Thin pluggable wrapper around an embedding provider for file cards."""

    config: EmbeddingConfig

    @property
    def provider(self) -> str:
        return self.config.provider

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def dimension(self) -> int:
        return self.config.dimension

    async def embed_card(self, card_text: str) -> List[float]:
        provider = get_embedding_provider(self.config.provider)
        vector = await provider.embed(card_text, input_type="passage")
        return vector or []

    async def embed_query(self, query: str) -> List[float]:
        provider = get_embedding_provider(self.config.provider)
        vector = await provider.embed(query, input_type="query")
        return vector or []


def get_card_embedding_provider(provider_name: Optional[str] = None) -> CardEmbeddingProvider:
    """Resolve the card embedding provider.

    Order: explicit arg → ``RETRIEVAL_CARD_EMBEDDING_PROVIDER`` → default embedding
    provider. P3 leaves the setting unset so cards use nv-embedcode.
    """
    name = provider_name or settings.RETRIEVAL_CARD_EMBEDDING_PROVIDER or None
    return CardEmbeddingProvider(config=get_embedding_config(name))
