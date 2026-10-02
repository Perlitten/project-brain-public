"""Lazy PyLate BF16 adapter used only inside the GPU service."""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import numpy as np


class PyLateEncoder:
    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str,
        dimension: int = 128,
        batch_size: int = 16,
        query_max_tokens: int = 32,
        document_max_tokens: int = 512,
        device: str = "cuda",
    ):
        self.model_name = model_name
        self.model_revision = model_revision
        self.dimension = dimension
        self.batch_size = batch_size
        self.query_max_tokens = query_max_tokens
        self.document_max_tokens = document_max_tokens
        self.device = device
        self._model: Any = None
        self._load_lock = asyncio.Lock()

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> Any:
        try:
            import torch
            from pylate import models
        except ImportError as exc:
            raise RuntimeError("GPU service requires the optional pylate and torch packages") from exc
        model = models.ColBERT(
            model_name_or_path=self.model_name,
            revision=self.model_revision,
            device=self.device,
            trust_remote_code=True,
            query_length=self.query_max_tokens,
            document_length=self.document_max_tokens,
            model_kwargs={"torch_dtype": torch.bfloat16},
        )
        model.tokenizer.pad_token = model.tokenizer.eos_token
        return model

    async def _encode(self, texts: Sequence[str], *, is_query: bool) -> list[np.ndarray]:
        model = await self._ensure_model()

        def run() -> list[np.ndarray]:
            values = model.encode(
                list(texts),
                is_query=is_query,
                batch_size=self.batch_size,
                convert_to_tensor=False,
                show_progress_bar=False,
            )
            matrices = [np.asarray(value, dtype=np.float32) for value in values]
            for matrix in matrices:
                if matrix.ndim != 2 or matrix.shape[1] != self.dimension:
                    raise RuntimeError("PyLate returned an unexpected token matrix shape")
            return matrices

        return await asyncio.to_thread(run)

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]:
        return await self._encode(texts, is_query=True)

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return await self._encode(texts, is_query=False)

    async def ready(self) -> None:
        await self._ensure_model()
