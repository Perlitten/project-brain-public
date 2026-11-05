from abc import ABC, abstractmethod
from typing import List, Optional
from functools import wraps
from inspect import iscoroutinefunction
from brain.llm.call_budget import require_provider_budget


def _guard_provider_method(method):
    if iscoroutinefunction(method):
        @wraps(method)
        async def guarded(*args, **kwargs):
            require_provider_budget()
            return await method(*args, **kwargs)
    else:
        @wraps(method)
        def guarded(*args, **kwargs):
            require_provider_budget()
            return method(*args, **kwargs)
    return guarded


class _ProviderBudgetGuard:
    """Guard concrete overrides as well as inherited/cached provider methods.

    Checking on method execution denies a coroutine created before the budget
    scope and awaited inside it. Constructors cannot download a local model or
    create a client during zero-provider maintenance either.
    """
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name in ('__init__', 'generate', 'generate_with_metadata', 'embed', 'embed_batch', 'summarize'):
            method = cls.__dict__.get(name)
            if method is not None:
                setattr(cls, name, _guard_provider_method(method))


class LLMProvider(_ProviderBudgetGuard, ABC):
    """Abstract base class for LLM text generation providers."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> str:
        """Asynchronously generates a response for the given prompt.

        Args:
            prompt: The user input text.
            system_instruction: Optional context/instructions for the system prompt.
            **kwargs: Extra arguments for generation (e.g. temperature, max_tokens).

        Returns:
            The generated response string.
        """
        pass

    async def generate_with_metadata(self, prompt: str, system_instruction: Optional[str] = None, **kwargs):
        """Generate text plus provider metadata when the provider exposes it."""
        text = await self.generate(prompt, system_instruction=system_instruction, **kwargs)
        return {"text": text, "finish_reason": None, "usage": None}


class EmbeddingProvider(_ProviderBudgetGuard, ABC):
    """Abstract base class for text embedding providers."""

    @abstractmethod
    async def embed(self, text: str, **kwargs) -> List[float]:
        """Asynchronously generates an embedding vector for a single piece of text.

        Args:
            text: Input text string.
            **kwargs: Extra provider-specific parameters.

        Returns:
            List of floats representing the embedding vector.
        """
        pass

    @abstractmethod
    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        """Asynchronously generates embedding vectors for a list of texts.

        Args:
            texts: List of input text strings.
            **kwargs: Extra provider-specific parameters.

        Returns:
            List of embedding vectors (list of float lists).
        """
        pass


class SummarizerProvider(_ProviderBudgetGuard, ABC):
    """Abstract base class for text summarization providers."""

    @abstractmethod
    async def summarize(self, text: str, max_length: Optional[int] = None, **kwargs) -> str:
        """Asynchronously generates a summary of the input text or file chunk.

        Args:
            text: The text/code chunk to summarize.
            max_length: Optional suggested maximum length of the summary.
            **kwargs: Extra parameters.

        Returns:
            The summary string.
        """
        pass
