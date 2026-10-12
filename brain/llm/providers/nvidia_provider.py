"""NVIDIA NIM providers — thin wrappers over the generic OpenAI-compatible client.

All endpoint defaults (base URL, models, 4096-dim ``nv-embedcode``, 2048-char
input cap, batch size 8, ``input_type="query"``) live in the ``nvidia`` preset
in :mod:`brain.llm.presets`. Legacy settings (``NVIDIA_API_KEY``,
``NVIDIA_LLM_MODEL``, ``NVIDIA_EMBEDDING_MODEL``, ``NVIDIA_SUMMARIZER_MODEL``)
keep working unchanged.
"""

import re
from typing import List

import asyncio  # noqa: F401  (tests patch nvidia_provider.asyncio.sleep)
import httpx  # noqa: F401  (tests patch nvidia_provider.httpx.AsyncClient)

from brain.llm.presets import PRESETS
from brain.llm.providers.openai_compatible import DEFAULT_EMBEDDING_BATCH_SIZE
from brain.llm.providers.openai_provider import (
    OpenAIEmbeddingProvider,
    OpenAILLMProvider,
    OpenAISummarizerProvider,
)

_NVIDIA = PRESETS["nvidia"]

# NIM parses any "data:image/...;base64," substring in an embedding input as an
# image and rejects the whole request with 400 ("image inputs require VLM
# serving"). Source code legitimately contains such literals (tests, CSS,
# fixtures), so break the scheme before sending; the text stays readable.
_IMAGE_DATA_URL = re.compile(r"data:(\s*)image/", re.IGNORECASE)


def _neutralize_image_data_urls(text: str) -> str:
    return _IMAGE_DATA_URL.sub("data: image/", text) if "image/" in text.lower() else text


class NvidiaLLMProvider(OpenAILLMProvider):
    """NVIDIA NIM chat-completions provider (``nvidia`` preset)."""

    PRESET = "nvidia"


class NvidiaEmbeddingProvider(OpenAIEmbeddingProvider):
    """NVIDIA NIM embeddings provider (``nvidia`` preset)."""

    PRESET = "nvidia"
    # nv-embedcode has a per-request TOKEN limit (~1k); see the preset comment.
    # (Re-index after changing this so stored vectors are consistent.)
    MAX_INPUT_CHARS = _NVIDIA.embedding_max_input_chars or 0
    MAX_BATCH_SIZE = _NVIDIA.embedding_max_batch_size or DEFAULT_EMBEDDING_BATCH_SIZE

    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        return await super().embed_batch([_neutralize_image_data_urls(t) for t in texts], **kwargs)


class NvidiaSummarizerProvider(OpenAISummarizerProvider):
    """NVIDIA NIM summarizer (``nvidia`` preset)."""

    PRESET = "nvidia"
    LLM_CLASS = NvidiaLLMProvider


__all__ = ["NvidiaEmbeddingProvider", "NvidiaLLMProvider", "NvidiaSummarizerProvider"]
