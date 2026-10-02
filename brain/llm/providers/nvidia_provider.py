import asyncio
import httpx
from typing import List, Optional
from loguru import logger
from brain.config.settings import settings
from brain.llm.providers.openai_provider import OpenAILLMProvider, OpenAIEmbeddingProvider, OpenAISummarizerProvider

class NvidiaLLMProvider(OpenAILLMProvider):
    """NVIDIA NIM API implementation of LLMProvider using integrate.api.nvidia.com."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        api_key = api_key if api_key is not None else settings.NVIDIA_API_KEY
        model = model or settings.NVIDIA_LLM_MODEL or "meta/llama-3.1-70b-instruct"
        super().__init__(api_key=api_key, model=model)
        self.api_url = "https://integrate.api.nvidia.com/v1/chat/completions"


class NvidiaEmbeddingProvider(OpenAIEmbeddingProvider):
    """NVIDIA NIM API implementation of EmbeddingProvider using integrate.api.nvidia.com."""

    # nv-embedcode has a per-request TOKEN limit (~1k). Probed against the live
    # API with real code: <=3072 chars OK, a dense 4096-char chunk -> 500. 512 was
    # over-conservative (truncated most chunks to ~12 lines); 2048 captures ~4x
    # more context with a safe margin below the token ceiling for dense code.
    # (Re-index after changing this so stored vectors are consistent.)
    MAX_INPUT_CHARS = 2048
    MAX_BATCH_SIZE = 8

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        api_key = api_key if api_key is not None else settings.NVIDIA_API_KEY
        model = model or settings.NVIDIA_EMBEDDING_MODEL or "nvidia/nv-embedcode-7b-v1"
        super().__init__(api_key=api_key, model=model)
        self.api_url = "https://integrate.api.nvidia.com/v1/embeddings"
        self.provider = "nvidia"
        self.dimension = 4096


    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        if not self.api_key:
            raise ValueError("NVIDIA API key is not configured.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload_kwargs = {**kwargs}
        if "input_type" not in payload_kwargs:
            payload_kwargs["input_type"] = "query"

        n_truncated = sum(1 for t in texts if len(t) > self.MAX_INPUT_CHARS)
        if n_truncated:
            logger.warning(
                f"NvidiaEmbeddingProvider: truncated {n_truncated}/{len(texts)} "
                f"input(s) to {self.MAX_INPUT_CHARS} chars before embedding."
            )
        clipped = [
            t[: self.MAX_INPUT_CHARS] if len(t) > self.MAX_INPUT_CHARS else t
            for t in texts
        ]

        payload = {
            "model": self.model,
            "input": clipped,
            **payload_kwargs
        }

        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(self.api_url, headers=headers, json=payload, timeout=60.0)
                    response.raise_for_status()
                    data = sorted(response.json()["data"], key=lambda item: item["index"])
                    if [item["index"] for item in data] != list(range(len(texts))):
                        raise ValueError("NVIDIA embedding response cardinality/index mismatch")
                    return [item["embedding"] for item in data]
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise
                delay = float(2 ** attempt)
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        delay = max(delay, min(60.0, float(exc.response.headers.get("Retry-After", "0"))))
                    except ValueError:
                        pass
                logger.warning("NVIDIA embedding retry {}/3 after {}", attempt + 1, type(exc).__name__)
                await asyncio.sleep(delay)
        raise RuntimeError("NVIDIA embedding retries exhausted")


class NvidiaSummarizerProvider(OpenAISummarizerProvider):
    """NVIDIA NIM API implementation of SummarizerProvider using integrate.api.nvidia.com."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        api_key = api_key if api_key is not None else settings.NVIDIA_API_KEY
        model = model or settings.NVIDIA_SUMMARIZER_MODEL or "meta/llama-3.1-8b-instruct"
        self.api_key = api_key
        self.llm = NvidiaLLMProvider(api_key=self.api_key, model=model)
