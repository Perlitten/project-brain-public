"""Late-interaction service adapter for a private llama.cpp embedding sidecar."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import numpy as np

from brain.late_interaction.provider import LfmColbertProvider


class LlamaCppEncoder:
    """Adapt ``LfmColbertProvider`` to the service ``LateInteractionEncoder`` contract.

    The provider owns the exact LFM tokenization, padding, punctuation filtering,
    shape validation and L2 normalization contract.  This adapter only adds
    ordered, bounded concurrency for service-side document batches.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        model_revision: str,
        dimension: int = 128,
        query_max_tokens: int = 32,
        document_max_tokens: int = 512,
        timeout_s: float = 60.0,
        max_concurrency: int = 1,
        expected_model_alias: str = "",
        expected_model_ftype: str = "",
        provider: LfmColbertProvider | None = None,
    ):
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self.model_name = model_name
        self.model_revision = model_revision
        self.dimension = dimension
        self.expected_model_alias = expected_model_alias
        self.expected_model_ftype = expected_model_ftype
        self._provider = provider or LfmColbertProvider(
            base_url=base_url,
            model=model_name,
            model_revision=model_revision,
            dimension=dimension,
            timeout_s=timeout_s,
            query_max_tokens=query_max_tokens,
            document_max_tokens=document_max_tokens,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._ready_lock = asyncio.Lock()
        self._warmed_up = False

    async def _encode_one(self, text: str, *, is_query: bool) -> np.ndarray:
        async with self._semaphore:
            encoded = await self._provider.embed(text, is_query=is_query)
        return np.asarray(encoded.vectors, dtype=np.float32)

    async def _encode(
        self,
        texts: Sequence[str],
        *,
        is_query: bool,
    ) -> list[np.ndarray]:
        if any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("late-interaction texts must be non-empty strings")
        if not texts:
            return []
        return list(
            await asyncio.gather(
                *(self._encode_one(text, is_query=is_query) for text in texts)
            )
        )

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]:
        return await self._encode(texts, is_query=True)

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return await self._encode(texts, is_query=False)

    async def ready(self) -> None:
        """Verify the sidecar and warm both model execution paths exactly once."""
        health = await self._provider.health()
        if health.get("status") != "healthy":
            raise RuntimeError("llama.cpp late-interaction sidecar is unhealthy")
        props = await self._provider.props()
        actual_alias = str(props.get("model_alias") or props.get("model_path") or "")
        if self.expected_model_alias and actual_alias != self.expected_model_alias:
            raise RuntimeError("llama.cpp late-interaction model alias mismatch")
        actual_ftype = str(props.get("model_ftype") or "")
        if (
            self.expected_model_ftype
            and actual_ftype.upper() != self.expected_model_ftype.upper()
        ):
            raise RuntimeError("llama.cpp late-interaction model type mismatch")
        if self._warmed_up:
            return
        async with self._ready_lock:
            if self._warmed_up:
                return
            await self._encode_one("Project Brain readiness query", is_query=True)
            await self._encode_one("Project Brain readiness document", is_query=False)
            self._warmed_up = True

    async def aclose(self) -> None:
        await self._provider.aclose()
