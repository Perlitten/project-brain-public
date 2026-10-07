"""Generic client for any OpenAI-compatible REST API.

One implementation serves OpenAI, NVIDIA NIM, OpenRouter, Groq, Together,
DeepSeek, Mistral, Ollama, LM Studio, vLLM… — anything exposing
``POST {base_url}/chat/completions`` and ``POST {base_url}/embeddings``.
Provider defaults live in :mod:`brain.llm.presets`; the ``Nvidia*`` /
``OpenAI*`` classes are thin preset wrappers around these.

``base_url`` must include the API version path (``https://api.groq.com/openai/v1``).
The ``Authorization`` header is sent only when a key is configured, so local
keyless servers (Ollama, LM Studio, vLLM) work without a dummy key.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from brain.llm.presets import (
    ResolvedEndpoint,
    join_url,
    resolve_embedding_endpoint,
    resolve_llm_endpoint,
    resolve_summarizer_endpoint,
)
from brain.llm.providers.base import EmbeddingProvider, LLMProvider, SummarizerProvider
from brain.config.settings import settings

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
DEFAULT_EMBEDDING_BATCH_SIZE = 64


class EmbeddingDimensionMismatchError(ValueError):
    """The endpoint returned vectors whose width differs from the configured
    embedding dimension (``EMBEDDING_DIMENSION`` / preset default). Storing them
    would corrupt or break the pgvector column, so indexing stops here."""


def _auth_headers(api_key: Optional[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _missing_key_message(label: str, key_source: Optional[str]) -> str:
    hint = f" Set {key_source}." if key_source else ""
    return f"{label} API key is not configured.{hint}"


class OpenAICompatibleLLMProvider(LLMProvider):
    """Chat-completions client for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: Optional[str],
        model: Optional[str],
        api_key: Optional[str] = None,
        provider: str = "openai_compatible",
        label: str = "OpenAI-compatible",
        require_key: bool = False,
        key_source: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or "").strip() or None
        self.api_key = api_key or None
        self.model = model
        self.provider = provider
        self.label = label
        self.require_key = require_key
        self.key_source = key_source
        self.timeout = timeout
        self.api_url = join_url(self.base_url, "chat/completions") if self.base_url else ""

    @classmethod
    def from_endpoint(cls, endpoint: ResolvedEndpoint, **overrides: Any) -> "OpenAICompatibleLLMProvider":
        params: Dict[str, Any] = dict(
            base_url=endpoint.base_url,
            model=endpoint.model,
            api_key=endpoint.api_key,
            provider=endpoint.provider,
            label=endpoint.label,
            require_key=endpoint.requires_key,
            key_source=endpoint.key_source,
        )
        params.update(overrides)
        return cls(**params)

    @classmethod
    def from_preset(cls, name: str, *, model: Optional[str] = None, cfg: Any = None) -> "OpenAICompatibleLLMProvider":
        return cls.from_endpoint(resolve_llm_endpoint(name, model=model, cfg=cfg))

    def _check_config(self) -> str:
        if not self.api_url:
            raise ValueError(
                f"{self.label} base URL is not configured. Set LLM_BASE_URL "
                "(including the version path, e.g. https://api.example.com/v1)."
            )
        if not self.model:
            raise ValueError(f"{self.label} model is not configured. Set LLM_MODEL.")
        if self.require_key and not self.api_key:
            raise ValueError(_missing_key_message(self.label, self.key_source))
        return self.api_url

    async def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        api_url = self._check_config()

        from brain.llm.observability import audit_and_bound_llm_input

        prompt, system_instruction, audit = audit_and_bound_llm_input(
            prompt,
            system_instruction,
            lineage_verified=kwargs.pop("lineage_verified", True),
            cache_hit=kwargs.pop("cache_hit", False),
        )

        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        payload = {"model": self.model, "messages": messages, **kwargs}
        headers = _auth_headers(self.api_key)
        retries = max(1, settings.LLM_MAX_RETRIES)
        delay = max(0.1, settings.LLM_RETRY_BASE_DELAY_S)
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(api_url, headers=headers, json=payload, timeout=self.timeout)
                    response.raise_for_status()
                    res_json = response.json()
                    choices = res_json.get("choices") or []
                    if not choices:
                        # e.g. a content-filtered/moderation response — surface a
                        # clear error instead of an opaque KeyError/IndexError.
                        raise ValueError(f"LLM response contained no choices: {str(res_json)[:200]}")
                    return choices[0]["message"]["content"]
            except httpx.HTTPStatusError as e:
                if attempt == retries - 1:
                    raise e
                if e.response.status_code in _RETRYABLE_STATUS:
                    wait_time = delay * (2 ** attempt)
                    logger.warning(
                        f"API request failed with status {e.response.status_code}. "
                        f"Retrying in {wait_time:.1f}s... (Attempt {attempt+1}/{retries})"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    raise e
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                wait_time = delay * (2 ** attempt)
                logger.warning(f"API request failed: {e}. Retrying in {wait_time:.1f}s... (Attempt {attempt+1}/{retries})")
                await asyncio.sleep(wait_time)

        raise RuntimeError(f"{self.label} request failed after all retries")


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """``/embeddings`` client with input capping, batch chunking, 429/5xx
    retries honouring ``Retry-After``, response index validation and a hard
    vector-width check against the configured dimension."""

    MAX_INPUT_CHARS: int = 0  # 0 = no client-side cap
    MAX_BATCH_SIZE: int = DEFAULT_EMBEDDING_BATCH_SIZE

    def __init__(
        self,
        *,
        base_url: Optional[str],
        model: Optional[str],
        api_key: Optional[str] = None,
        dimension: Optional[int] = None,
        max_input_chars: Optional[int] = None,
        max_batch_size: Optional[int] = None,
        input_type: Optional[str] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
        provider: str = "openai_compatible",
        label: str = "OpenAI-compatible",
        require_key: bool = False,
        key_source: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or "").strip() or None
        self.api_key = api_key or None
        self.model = model
        self.provider = provider
        self.label = label
        self.require_key = require_key
        self.key_source = key_source
        self.timeout = timeout
        self.dimension = dimension
        self.input_type = input_type
        self.extra_payload = dict(extra_payload or {})
        # Instance attributes shadow the class defaults so the indexer
        # (MAX_INPUT_CHARS / MAX_BATCH_SIZE lookups) sees the resolved values.
        if max_input_chars:
            self.MAX_INPUT_CHARS = int(max_input_chars)
        if max_batch_size:
            self.MAX_BATCH_SIZE = int(max_batch_size)
        self.api_url = join_url(self.base_url, "embeddings") if self.base_url else ""

    @classmethod
    def from_endpoint(cls, endpoint: ResolvedEndpoint, **overrides: Any) -> "OpenAICompatibleEmbeddingProvider":
        params: Dict[str, Any] = dict(
            base_url=endpoint.base_url,
            model=endpoint.model,
            api_key=endpoint.api_key,
            dimension=endpoint.dimension,
            max_input_chars=endpoint.max_input_chars,
            max_batch_size=endpoint.max_batch_size,
            input_type=endpoint.input_type,
            provider=endpoint.provider,
            label=endpoint.label,
            require_key=endpoint.requires_key,
            key_source=endpoint.key_source,
        )
        params.update(overrides)
        return cls(**params)

    @classmethod
    def from_preset(cls, name: str, *, cfg: Any = None) -> "OpenAICompatibleEmbeddingProvider":
        return cls.from_endpoint(resolve_embedding_endpoint(name, cfg=cfg))

    @property
    def max_input_chars(self) -> int:
        return int(self.MAX_INPUT_CHARS or 0)

    def _check_config(self) -> str:
        if not self.api_url:
            raise ValueError(
                f"{self.label} embedding base URL is not configured. Set EMBEDDING_BASE_URL "
                "or LLM_BASE_URL (including the version path)."
            )
        if not self.model:
            raise ValueError(f"{self.label} embedding model is not configured. Set EMBEDDING_MODEL.")
        if not self.dimension:
            raise ValueError(
                f"{self.label} embedding dimension is unknown for model '{self.model}'. "
                "Set EMBEDDING_DIMENSION to the model's output width."
            )
        if self.require_key and not self.api_key:
            raise ValueError(_missing_key_message(self.label, self.key_source))
        return self.api_url

    async def embed(self, text: str, **kwargs) -> List[float]:
        res = await self.embed_batch([text], **kwargs)
        return res[0]

    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        api_url = self._check_config()
        if not texts:
            return []

        payload_kwargs = {**self.extra_payload, **kwargs}
        if self.input_type:
            payload_kwargs.setdefault("input_type", self.input_type)
        else:
            # Most OpenAI-compatible servers reject unknown fields like input_type.
            payload_kwargs.pop("input_type", None)

        cap = self.max_input_chars
        if cap > 0:
            n_truncated = sum(1 for t in texts if len(t) > cap)
            if n_truncated:
                logger.warning(
                    f"{type(self).__name__}: truncated {n_truncated}/{len(texts)} "
                    f"input(s) to {cap} chars before embedding."
                )
            texts = [t[:cap] if len(t) > cap else t for t in texts]

        batch_size = max(1, int(self.MAX_BATCH_SIZE or DEFAULT_EMBEDDING_BATCH_SIZE))
        headers = _auth_headers(self.api_key)
        vectors: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            chunk = texts[start:start + batch_size]
            payload = {"model": self.model, "input": chunk, **payload_kwargs}
            vectors.extend(await self._post(api_url, headers, payload, len(chunk)))
        return vectors

    async def _post(
        self, api_url: str, headers: Dict[str, str], payload: Dict[str, Any], expected: int
    ) -> List[List[float]]:
        max_retries = max(1, settings.LLM_MAX_RETRIES)
        base_delay = max(0.1, settings.LLM_RETRY_BASE_DELAY_S)
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(api_url, headers=headers, json=payload, timeout=self.timeout)
                    response.raise_for_status()
                    data = sorted(response.json()["data"], key=lambda item: item["index"])
                    if [item["index"] for item in data] != list(range(expected)):
                        raise ValueError(f"{self.label} embedding response cardinality/index mismatch")
                    vectors = [item["embedding"] for item in data]
                    self._check_dimensions(vectors)
                    return vectors
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in _RETRYABLE_STATUS:
                    raise
                if attempt == max_retries - 1:
                    raise
                delay = base_delay * float(2 ** attempt)
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        delay = max(delay, min(60.0, float(exc.response.headers.get("Retry-After", "0"))))
                    except ValueError:
                        pass
                logger.warning("{} embedding retry {}/{} after {}", self.label, attempt + 1, max_retries, type(exc).__name__)
                await asyncio.sleep(delay)
        raise RuntimeError(f"{self.label} embedding retries exhausted")

    def _check_dimensions(self, vectors: List[List[float]]) -> None:
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbeddingDimensionMismatchError(
                    f"{self.label} embedding model '{self.model}' returned {len(vector)}-dim vectors "
                    f"but the configured dimension is {self.dimension}. Set EMBEDDING_DIMENSION="
                    f"{len(vector)} (this changes the pgvector column and requires a full re-index) "
                    "or switch back to the model the index was built with."
                )


class OpenAICompatibleSummarizerProvider(SummarizerProvider):
    """Summarizer on top of :class:`OpenAICompatibleLLMProvider`."""

    def __init__(self, llm: OpenAICompatibleLLMProvider):
        self.llm = llm
        self.api_key = llm.api_key

    @classmethod
    def from_endpoint(cls, endpoint: ResolvedEndpoint) -> "OpenAICompatibleSummarizerProvider":
        return cls(OpenAICompatibleLLMProvider.from_endpoint(endpoint))

    @classmethod
    def from_preset(cls, name: str, *, model: Optional[str] = None, cfg: Any = None) -> "OpenAICompatibleSummarizerProvider":
        return cls.from_endpoint(resolve_summarizer_endpoint(name, model=model, cfg=cfg))

    async def summarize(self, text: str, max_length: Optional[int] = None, **kwargs) -> str:
        prompt = f"Please summarize the following content:\n\n{text}"
        if max_length:
            prompt += f"\n\nKeep the summary within approximately {max_length} characters."
        system_instruction = "You are a precise code and project documentation summarizer."
        return await self.llm.generate(prompt, system_instruction=system_instruction, **kwargs)


__all__ = [
    "EmbeddingDimensionMismatchError",
    "OpenAICompatibleEmbeddingProvider",
    "OpenAICompatibleLLMProvider",
    "OpenAICompatibleSummarizerProvider",
]
