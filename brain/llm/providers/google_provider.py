import asyncio
import httpx
from typing import Any, Dict, List, Optional
from brain.config.settings import settings
from brain.llm.providers.base import LLMProvider, EmbeddingProvider, SummarizerProvider

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)

class GoogleLLMProvider(LLMProvider):
    """Google Gemini API implementation of LLMProvider."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-1.5-flash"):
        self.api_key = api_key or settings.GOOGLE_API_KEY
        self.model = model
        # Base url without model, model is appended in generate call
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> str:
        if not self.api_key:
            raise ValueError("Google API key is not configured")

        from brain.llm.observability import audit_and_bound_llm_input

        prompt, system_instruction, audit = audit_and_bound_llm_input(
            prompt,
            system_instruction,
            lineage_verified=kwargs.pop("lineage_verified", True),
            cache_hit=kwargs.pop("cache_hit", False),
        )

        url = f"{self.base_url}/{self.model}:generateContent"

        payload: Dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ]
        }

        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [
                    {"text": system_instruction}
                ]
            }

        # Handle optional generation configuration
        generation_config: Dict[str, Any] = {}
        if "temperature" in kwargs:
            generation_config["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            generation_config["maxOutputTokens"] = kwargs["max_tokens"]
        if generation_config:
            payload["generationConfig"] = generation_config

        retries, delay = 3, 1.0
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    # Key in a header, never the URL query string (URLs leak into logs).
                    response = await client.post(url, json=payload, headers={"x-goog-api-key": self.api_key}, timeout=60.0)
                    response.raise_for_status()
                    res_json = response.json()
                    candidates = res_json.get("candidates") or []
                    if not candidates:
                        raise ValueError(f"Gemini response had no candidates: {str(res_json)[:200]}")
                    # Gemini response contains candidates -> content -> parts -> text
                    return candidates[0]["content"]["parts"][0]["text"]
            except httpx.HTTPStatusError as e:
                if attempt == retries - 1 or e.response.status_code not in _RETRYABLE_STATUS:
                    raise
                await asyncio.sleep(delay * (2 ** attempt))
            except Exception:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(delay * (2 ** attempt))

        raise RuntimeError("Google request failed after all retries")


class GoogleEmbeddingProvider(EmbeddingProvider):
    """Google Gemini API implementation of EmbeddingProvider using text-embedding-004."""

    def __init__(self, api_key: Optional[str] = None, model: str = "text-embedding-004"):
        self.api_key = api_key or settings.GOOGLE_API_KEY
        self.model = model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def embed(self, text: str, **kwargs) -> List[float]:
        if not self.api_key:
            raise ValueError("Google API key is not configured.")

        url = f"{self.base_url}/{self.model}:embedContent"

        payload = {
            "model": f"models/{self.model}",
            "content": {
                "parts": [
                    {"text": text}
                ]
            }
        }

        async with httpx.AsyncClient() as client:
            # Key in a header, never the URL query string (URLs leak into logs).
            response = await client.post(url, json=payload, headers={"x-goog-api-key": self.api_key}, timeout=60.0)
            response.raise_for_status()
            res_json = response.json()
            return res_json["embedding"]["values"]

    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        if not self.api_key:
            raise ValueError("Google API key is not configured.")

        url = f"{self.base_url}/{self.model}:batchEmbedContents"

        requests = [
            {
                "model": f"models/{self.model}",
                "content": {
                    "parts": [
                        {"text": text}
                    ]
                }
            }
            for text in texts
        ]

        payload = {"requests": requests}

        async with httpx.AsyncClient() as client:
            # Key in a header, never the URL query string (URLs leak into logs).
            response = await client.post(url, json=payload, headers={"x-goog-api-key": self.api_key}, timeout=60.0)
            response.raise_for_status()
            res_json = response.json()
            return [emb["values"] for emb in res_json["embeddings"]]


class GoogleSummarizerProvider(SummarizerProvider):
    """Google Gemini API implementation of SummarizerProvider using gemini-1.5-flash by default."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-1.5-flash"):
        self.api_key = api_key or settings.GOOGLE_API_KEY
        self.llm = GoogleLLMProvider(api_key=self.api_key, model=model)

    async def summarize(self, text: str, max_length: Optional[int] = None, **kwargs) -> str:
        prompt = f"Please summarize the following content:\n\n{text}"
        if max_length:
            prompt += f"\n\nKeep the summary within approximately {max_length} characters."
        system_instruction = "You are a precise code and project documentation summarizer."
        return await self.llm.generate(prompt, system_instruction=system_instruction, **kwargs)
