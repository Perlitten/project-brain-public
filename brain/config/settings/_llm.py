"""LLM provider keys, embeddings and summarizer fields."""
from __future__ import annotations

from typing import Optional
from pydantic import field_validator


class SettingsLlm:
    # LLM / AI Configuration
    OPENAI_API_KEY: Optional[str] = None

    ANTHROPIC_API_KEY: Optional[str] = None

    GOOGLE_API_KEY: Optional[str] = None

    NVIDIA_API_KEY: Optional[str] = None

    NVIDIA_LLM_MODEL: str = "meta/llama-3.1-70b-instruct"

    NVIDIA_EMBEDDING_MODEL: str = "nvidia/nv-embedcode-7b-v1"

    NVIDIA_SUMMARIZER_MODEL: str = "meta/llama-3.1-8b-instruct"

    # Keys for the other OpenAI-compatible presets (brain/llm/presets.py).
    OPENROUTER_API_KEY: Optional[str] = None

    GROQ_API_KEY: Optional[str] = None

    TOGETHER_API_KEY: Optional[str] = None

    DEEPSEEK_API_KEY: Optional[str] = None

    MISTRAL_API_KEY: Optional[str] = None

    # Per-task model overrides. Unset = derived from the active provider: the
    # small/fast model (SUMMARIZER_MODEL or preset default) for classification,
    # summarization and insight, the main model (LLM_MODEL or preset default)
    # for synthesis. For nvidia that is exactly the historical 8b/70b split;
    # keep self-diagnosis (insight) on the low-latency route — the operational
    # snapshot is large enough that slower reasoning models can exceed the SLA.
    LLM_TASK_CLASSIFICATION_MODEL: Optional[str] = None

    LLM_TASK_SUMMARIZATION_MODEL: Optional[str] = None

    LLM_TASK_SYNTHESIS_MODEL: Optional[str] = None

    # Explicit bounded /ask route; unset preserves the synthesis model.
    LLM_TASK_ASK_MODEL: Optional[str] = None

    # Provider-specific ask-only option, opt in for Nemotron models supporting
    # chat_template_kwargs. None leaves their default reasoning mode untouched.
    LLM_TASK_ASK_ENABLE_THINKING: Optional[bool] = None

    LLM_TASK_INSIGHT_MODEL: Optional[str] = None

    # Universal OpenAI-compatible endpoint settings. They apply to the provider
    # selected by DEFAULT_LLM_PROVIDER and override its preset defaults.
    # LLM_BASE_URL must include the version path (https://host/v1).
    LLM_BASE_URL: Optional[str] = None

    LLM_API_KEY: Optional[str] = None

    LLM_MODEL: Optional[str] = None

    SUMMARIZER_MODEL: Optional[str] = None

    # Same for DEFAULT_EMBEDDING_PROVIDER. EMBEDDING_BASE_URL / EMBEDDING_API_KEY
    # fall back to LLM_BASE_URL / LLM_API_KEY when both slots use the same provider.
    EMBEDDING_BASE_URL: Optional[str] = None

    EMBEDDING_API_KEY: Optional[str] = None

    EMBEDDING_MODEL: Optional[str] = None

    # Single configured embedding dimension (0 = auto from provider/preset).
    # Required for an embedding model the preset does not know. Changing it
    # rebuilds the pgvector column — re-index everything afterwards.
    EMBEDDING_DIMENSION: int = 0

    # Client-side per-input char cap and per-request batch size (0 = preset/default).
    EMBEDDING_MAX_INPUT_CHARS: int = 0

    # Retry budget for LLM/embedding API calls: total attempts per request and
    # base delay (seconds) for exponential backoff. Covers transient 429/5xx
    # and network blips from providers like NVIDIA.
    LLM_MAX_RETRIES: int = 5

    LLM_RETRY_BASE_DELAY_S: float = 2.0

    EMBEDDING_BATCH_SIZE: int = 0

    # Send the asymmetric-retrieval ``input_type`` field (NVIDIA-style).
    # Unset = preset behaviour (nvidia: yes, others: no).
    EMBEDDING_SEND_INPUT_TYPE: Optional[bool] = None

    @field_validator(
        "LLM_TASK_ASK_ENABLE_THINKING", mode="before",
    )
    @classmethod
    def normalize_blank_ask_thinking(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(
        "EMBEDDING_DIMENSION",
        "EMBEDDING_MAX_INPUT_CHARS",
        "EMBEDDING_BATCH_SIZE",
        mode="before",
    )
    @classmethod
    def normalize_blank_embedding_integer(cls, value):
        # ``EMBEDDING_DIMENSION=`` in an env file means "auto", not a parse error.
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0
        return value

    @field_validator("EMBEDDING_DIMENSION", "EMBEDDING_MAX_INPUT_CHARS", "EMBEDDING_BATCH_SIZE")
    @classmethod
    def validate_embedding_integer(cls, value: int) -> int:
        if value < 0:
            raise ValueError("embedding size settings must be >= 0 (0 = auto)")
        return value

    @field_validator("EMBEDDING_SEND_INPUT_TYPE", mode="before")
    @classmethod
    def normalize_blank_optional_bool(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value
