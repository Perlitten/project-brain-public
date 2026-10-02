import asyncio
import httpx
from typing import Any, Dict, List, Optional
from brain.config.settings import settings
from brain.llm.providers.base import LLMProvider, EmbeddingProvider, SummarizerProvider

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)

class AnthropicLLMProvider(LLMProvider):
    """Anthropic Claude API implementation of LLMProvider."""

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-3-5-sonnet-20241022"):
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.model = model
        self.api_url = "https://api.anthropic.com/v1/messages"

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> str:
        if not self.api_key:
            raise ValueError("Anthropic API key is not configured")

        from brain.llm.observability import audit_and_bound_llm_input

        prompt, system_instruction, audit = audit_and_bound_llm_input(
            prompt,
            system_instruction,
            lineage_verified=kwargs.pop("lineage_verified", True),
            cache_hit=kwargs.pop("cache_hit", False),
        )

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        # Anthropic messages API requires max_tokens
        max_tokens = kwargs.pop("max_tokens", 4096)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": max_tokens,
            **kwargs
        }

        if system_instruction:
            payload["system"] = system_instruction

        retries, delay = 3, 1.0
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(self.api_url, headers=headers, json=payload, timeout=60.0)
                    response.raise_for_status()
                    res_json = response.json()
                    content = res_json.get("content") or []
                    if not content:
                        raise ValueError(f"Anthropic response had no content: {str(res_json)[:200]}")
                    return content[0]["text"]
            except httpx.HTTPStatusError as e:
                if attempt == retries - 1 or e.response.status_code not in _RETRYABLE_STATUS:
                    raise
                await asyncio.sleep(delay * (2 ** attempt))
            except Exception:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(delay * (2 ** attempt))

        raise RuntimeError("Anthropic request failed after all retries")


class AnthropicEmbeddingProvider(EmbeddingProvider):
    """Anthropic does not offer a native embedding API.
    This class raises NotImplementedError advising the use of other embedding providers.
    """

    async def embed(self, text: str, **kwargs) -> List[float]:
        raise NotImplementedError(
            "Anthropic does not offer a native embedding API. "
            "Please use a different DEFAULT_EMBEDDING_PROVIDER (e.g., 'openai', 'google', or 'mock')."
        )

    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        raise NotImplementedError(
            "Anthropic does not offer a native embedding API. "
            "Please use a different DEFAULT_EMBEDDING_PROVIDER (e.g., 'openai', 'google', or 'mock')."
        )


class AnthropicSummarizerProvider(SummarizerProvider):
    """Anthropic Claude API implementation of SummarizerProvider using Claude-3.5-Haiku by default."""

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-3-5-haiku-20241022"):
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.llm = AnthropicLLMProvider(api_key=self.api_key, model=model)

    async def summarize(self, text: str, max_length: Optional[int] = None, **kwargs) -> str:
        prompt = f"Please summarize the following content:\n\n{text}"
        if max_length:
            prompt += f"\n\nKeep the summary within approximately {max_length} characters."
        system_instruction = "You are a precise code and project documentation summarizer."
        return await self.llm.generate(prompt, system_instruction=system_instruction, **kwargs)
