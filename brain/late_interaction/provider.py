"""LFM2.5-ColBERT client for a loopback llama.cpp sidecar."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from brain.config.settings import settings
from brain.late_interaction.metrics import increment, observe


_SKIPLIST_WORDS = (
    "!",
    '"',
    "#",
    "$",
    "%",
    "&",
    "'",
    "(",
    ")",
    "*",
    "+",
    ",",
    "-",
    ".",
    "/",
    ":",
    ";",
    "<",
    "=",
    ">",
    "?",
    "@",
    "[",
    "\\",
    "]",
    "^",
    "_",
    "`",
    "{",
    "|",
    "}",
    "~",
)
_SPECIAL_TOKEN_CACHE: dict[tuple[str, str, str], tuple[int, frozenset[int]]] = {}
_SHARED_PROVIDER: LfmColbertProvider | None = None


class LateInteractionProviderError(RuntimeError):
    """The optional sidecar did not satisfy its shape/runtime contract."""


@dataclass(frozen=True)
class EncodedText:
    vectors: np.ndarray
    token_count: int
    truncated: bool


class LfmColbertProvider:
    """Official GGUF runtime contract: tokenize -> per-token embedding -> L2 norm."""

    provider = "llama.cpp"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        model_revision: str | None = None,
        dimension: int | None = None,
        timeout_s: float | None = None,
        query_max_tokens: int | None = None,
        document_max_tokens: int | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = (base_url or settings.LATE_INTERACTION_PROVIDER_URL).rstrip("/")
        self.model = model or settings.LATE_INTERACTION_MODEL
        self.model_revision = model_revision or settings.LATE_INTERACTION_MODEL_REVISION
        self.dimension = dimension if dimension is not None else settings.LATE_INTERACTION_DIMENSION
        self.timeout_s = timeout_s if timeout_s is not None else settings.LATE_INTERACTION_TIMEOUT_S
        self.query_max_tokens = (
            query_max_tokens if query_max_tokens is not None else settings.LATE_INTERACTION_QUERY_MAX_TOKENS
        )
        self.document_max_tokens = (
            document_max_tokens
            if document_max_tokens is not None
            else settings.LATE_INTERACTION_DOCUMENT_MAX_TOKENS
        )
        self._client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._special_lock = asyncio.Lock()
        self._pad_token_id: int | None = None
        self._skip_token_ids: frozenset[int] | None = None

    @property
    def _cache_key(self) -> tuple[str, str, str]:
        return self.base_url, self.model, self.model_revision

    async def _active_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._owned_client is not None:
            return self._owned_client
        async with self._client_lock:
            if self._owned_client is None:
                self._owned_client = httpx.AsyncClient(
                    timeout=self.timeout_s,
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                )
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        started = time.perf_counter()
        increment("provider_requests")
        try:
            client = await self._active_client()
            response = await client.request(method, f"{self.base_url}{path}", **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            increment("provider_failures")
            error = str(exc).strip() or type(exc).__name__
            raise LateInteractionProviderError(f"late-interaction sidecar {path} failed: {error}") from exc
        finally:
            observe("provider_latency_ms_total", (time.perf_counter() - started) * 1000)

    @staticmethod
    def _tokens_from_response(payload: Any) -> list[int]:
        if not isinstance(payload, dict) or not isinstance(payload.get("tokens"), list):
            raise LateInteractionProviderError("tokenize response does not contain a tokens list")
        tokens: list[int] = []
        for value in payload["tokens"]:
            token = value.get("id") if isinstance(value, dict) else value
            if not isinstance(token, int):
                raise LateInteractionProviderError("tokenize response contains a non-integer token")
            tokens.append(token)
        return tokens

    async def _tokenize(self, text: str, *, add_special: bool) -> list[int]:
        payload = await self._request(
            "POST",
            "/tokenize",
            json={"content": text, "add_special": add_special, "with_pieces": False},
        )
        return self._tokens_from_response(payload)

    async def _ensure_special_tokens(self) -> None:
        if self._pad_token_id is not None and self._skip_token_ids is not None:
            return
        cached = _SPECIAL_TOKEN_CACHE.get(self._cache_key)
        if cached is not None:
            self._pad_token_id, self._skip_token_ids = cached
            return
        async with self._special_lock:
            if self._pad_token_id is not None and self._skip_token_ids is not None:
                return
            cached = _SPECIAL_TOKEN_CACHE.get(self._cache_key)
            if cached is not None:
                self._pad_token_id, self._skip_token_ids = cached
                return
            token_lists = await asyncio.gather(
                self._tokenize("<|im_end|>", add_special=False),
                *(self._tokenize(word, add_special=False) for word in _SKIPLIST_WORDS),
            )
            if len(token_lists[0]) != 1:
                raise LateInteractionProviderError("model pad token did not map to exactly one token")
            self._pad_token_id = token_lists[0][0]
            self._skip_token_ids = frozenset(token for values in token_lists[1:] for token in values)
            _SPECIAL_TOKEN_CACHE[self._cache_key] = (self._pad_token_id, self._skip_token_ids)

    async def health(self) -> dict[str, Any]:
        payload = await self._request("GET", "/health")
        status = str((payload or {}).get("status") or "").lower() if isinstance(payload, dict) else ""
        return {
            "status": "healthy" if status in {"ok", "ready"} else "unhealthy",
            "provider": self.provider,
            "model": self.model,
            "model_revision": self.model_revision,
            "dimension": self.dimension,
            "endpoint_status": status or "unknown",
        }

    async def props(self) -> dict[str, Any]:
        payload = await self._request("GET", "/props")
        if not isinstance(payload, dict):
            raise LateInteractionProviderError("llama.cpp props response is invalid")
        return payload

    async def embed(self, text: str, *, is_query: bool) -> EncodedText:
        if not text:
            raise LateInteractionProviderError("cannot embed empty text")
        await self._ensure_special_tokens()
        prefix = "[Q] " if is_query else "[D] "
        tokens = await self._tokenize(prefix + text, add_special=True)
        max_tokens = self.query_max_tokens if is_query else self.document_max_tokens
        truncated = len(tokens) > max_tokens
        tokens = tokens[:max_tokens]
        if is_query and len(tokens) < max_tokens:
            assert self._pad_token_id is not None
            tokens.extend([self._pad_token_id] * (max_tokens - len(tokens)))

        payload = await self._request("POST", "/embedding", json={"content": tokens})
        try:
            raw = payload[0]["embedding"]
            matrix = np.asarray(raw, dtype=np.float32)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise LateInteractionProviderError("embedding response is not a token matrix") from exc
        if matrix.ndim != 2 or matrix.shape[1] != self.dimension or matrix.shape[0] != len(tokens):
            raise LateInteractionProviderError(
                f"unexpected token matrix shape {matrix.shape}; expected ({len(tokens)}, {self.dimension})"
            )
        if not np.isfinite(matrix).all():
            raise LateInteractionProviderError("embedding response contains non-finite values")

        if not is_query:
            assert self._skip_token_ids is not None
            keep = np.asarray([token not in self._skip_token_ids for token in tokens], dtype=bool)
            matrix = matrix[keep]
            if matrix.shape[0] == 0:
                raise LateInteractionProviderError("document embedding became empty after skiplist filtering")

        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms <= 0):
            raise LateInteractionProviderError("embedding response contains a zero-norm token vector")
        matrix = matrix / norms
        return EncodedText(vectors=matrix, token_count=int(matrix.shape[0]), truncated=truncated)


def get_lfm_colbert_provider() -> LfmColbertProvider:
    """Process-local provider with keep-alive and one-time special-token discovery."""
    global _SHARED_PROVIDER
    if _SHARED_PROVIDER is None:
        _SHARED_PROVIDER = LfmColbertProvider()
    return _SHARED_PROVIDER


async def close_lfm_colbert_provider() -> None:
    global _SHARED_PROVIDER
    if _SHARED_PROVIDER is not None:
        await _SHARED_PROVIDER.aclose()
        _SHARED_PROVIDER = None


async def warmup_lfm_colbert_provider() -> None:
    """Discover special tokens and load both query/document execution paths."""
    provider = get_lfm_colbert_provider()
    await provider.embed("Project Brain warmup query", is_query=True)
    await provider.embed("Project Brain warmup document", is_query=False)
