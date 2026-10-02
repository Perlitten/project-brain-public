import httpx
import asyncio
from typing import List, Optional
from loguru import logger
from brain.config.settings import settings
from brain.llm.providers.base import LLMProvider, EmbeddingProvider, SummarizerProvider

class OpenAILLMProvider(LLMProvider):
    """OpenAI API implementation of LLMProvider."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o"):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model
        self.api_url = "https://api.openai.com/v1/chat/completions"

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> str:
        if not self.api_key:
            raise ValueError("OpenAI API key is not configured.")

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

        payload = {
            "model": self.model,
            "messages": messages,
            **kwargs
        }

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        retries = 3
        delay = 1.0
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(self.api_url, headers=headers, json=payload, timeout=60.0)
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
                if e.response.status_code in (429, 500, 502, 503, 504):
                    wait_time = delay * (2 ** attempt)
                    logger.warning(f"API request failed with status {e.response.status_code}. Retrying in {wait_time:.1f}s... (Attempt {attempt+1}/{retries})")
                    await asyncio.sleep(wait_time)
                else:
                    raise e
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                wait_time = delay * (2 ** attempt)
                logger.warning(f"API request failed: {e}. Retrying in {wait_time:.1f}s... (Attempt {attempt+1}/{retries})")
                await asyncio.sleep(wait_time)

        raise RuntimeError("OpenAI request failed after all retries")


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI API implementation of EmbeddingProvider."""

    def __init__(self, api_key: Optional[str] = None, model: str = "text-embedding-3-small"):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model
        self.api_url = "https://api.openai.com/v1/embeddings"
        self.provider = "openai"
        self.dimension = 1536

    async def embed(self, text: str, **kwargs) -> List[float]:
        res = await self.embed_batch([text], **kwargs)
        return res[0]

    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        if not self.api_key:
            raise ValueError("OpenAI API key is not configured.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Pop provider-specific parameters like input_type
        payload_kwargs = {**kwargs}
        payload_kwargs.pop("input_type", None)

        payload = {
            "model": self.model,
            "input": texts,
            **payload_kwargs
        }

        retries = 3
        delay = 1.0
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(self.api_url, headers=headers, json=payload, timeout=60.0)
                    response.raise_for_status()
                    res_json = response.json()
                    sorted_data = sorted(res_json["data"], key=lambda x: x["index"])
                    return [data["embedding"] for data in sorted_data]
            except httpx.HTTPStatusError as e:
                if attempt == retries - 1:
                    raise e
                if e.response.status_code in (429, 500, 502, 503, 504):
                    wait_time = delay * (2 ** attempt)
                    logger.warning(f"API request failed with status {e.response.status_code}. Retrying in {wait_time:.1f}s... (Attempt {attempt+1}/{retries})")
                    await asyncio.sleep(wait_time)
                else:
                    raise e
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                wait_time = delay * (2 ** attempt)
                logger.warning(f"API request failed: {e}. Retrying in {wait_time:.1f}s... (Attempt {attempt+1}/{retries})")
                await asyncio.sleep(wait_time)

        raise RuntimeError("OpenAI embed request failed after all retries")


class OpenAISummarizerProvider(SummarizerProvider):
    """OpenAI API implementation of SummarizerProvider using GPT-4o-mini by default."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.llm = OpenAILLMProvider(api_key=self.api_key, model=model)

    async def summarize(self, text: str, max_length: Optional[int] = None, **kwargs) -> str:
        prompt = f"Please summarize the following content:\n\n{text}"
        if max_length:
            prompt += f"\n\nKeep the summary within approximately {max_length} characters."
        system_instruction = "You are a precise code and project documentation summarizer."
        return await self.llm.generate(prompt, system_instruction=system_instruction, **kwargs)
