"""Offline token-feature hashing; identifier search, not learned semantics."""
import hashlib
import re
from collections import Counter

import numpy as np

from brain.analyzers.identifier_words import split_identifier_words
from brain.llm.providers.base import EmbeddingProvider


class LocalEmbeddingProvider(EmbeddingProvider):
    dimension = 384
    model = "local-token-hash-v1"
    MAX_BATCH_SIZE = 64

    async def embed(self, text: str, **kwargs) -> list[float]:
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text[:100_000])
        features = []
        for token in tokens:
            features.append(token.lower())
            features.extend(part.lower() for part in split_identifier_words(token))
        vector = np.zeros(self.dimension, dtype=np.float32)
        for token, count in Counter(features).items():
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            vector[index] += (1 if digest[4] & 1 else -1) * (1 + np.log(count))
        norm = np.linalg.norm(vector)
        if norm:
            vector /= norm
        return vector.tolist()

    async def embed_batch(self, texts: list[str], **kwargs) -> list[list[float]]:
        return [await self.embed(text, **kwargs) for text in texts]
