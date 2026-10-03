"""OpenAI-compatible provider presets and endpoint resolution.

Every remote/local provider that speaks the OpenAI REST dialect
(``POST {base_url}/chat/completions`` and ``POST {base_url}/embeddings``) is
served by one generic implementation
(:mod:`brain.llm.providers.openai_compatible`). A *preset* only supplies
defaults — base URL, the provider-specific key env var, default models,
embedding dimension and endpoint quirks — so ``DEFAULT_LLM_PROVIDER=groq`` works
with nothing but ``GROQ_API_KEY``. ``openai_compatible`` (aliases ``custom`` /
``openai-compatible``) is the preset-less form configured entirely through the
universal ``LLM_*`` / ``EMBEDDING_*`` settings.

Precedence (highest first):

* LLM: ``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL`` (only for the provider
  selected by ``DEFAULT_LLM_PROVIDER``) > legacy per-provider settings
  (``NVIDIA_API_KEY``, ``NVIDIA_LLM_MODEL``…) > preset defaults.
* Embeddings: ``EMBEDDING_*`` (only for ``DEFAULT_EMBEDDING_PROVIDER``) > the
  ``LLM_BASE_URL`` / ``LLM_API_KEY`` of the LLM provider when both slots name the
  same provider > legacy settings > preset defaults.

The base URL must include the API version path (``https://api.openai.com/v1``,
``http://localhost:11434/v1``) exactly like the OpenAI SDK's ``OPENAI_BASE_URL``;
the client appends ``/chat/completions`` or ``/embeddings`` and nothing else.

This module is deliberately import-light (settings only): it is used by
``brain.embeddings.constants`` at import time and by the CLI doctor. Every
lookup goes through ``getattr(cfg, name, None)`` so a partial settings stub
(tests, doctor) works.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

OPENAI_COMPATIBLE = "openai_compatible"
NATIVE_PROVIDERS = frozenset({"mock", "anthropic", "google"})
_ALIASES = {
    "custom": OPENAI_COMPATIBLE,
    "openai-compatible": OPENAI_COMPATIBLE,
}

# Model ids the router used before presets existed. Non-OpenAI-compatible
# providers (mock/anthropic/google) keep exactly these task defaults.
LEGACY_FAST_TASK_MODEL = "meta/llama-3.1-8b-instruct"
LEGACY_SYNTHESIS_TASK_MODEL = "meta/llama-3.1-70b-instruct"


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    label: str  # short name used in error messages ("NVIDIA API key is not configured")
    display_name: str  # human-facing provider description
    base_url: Optional[str]
    key_env: Optional[str]  # provider-specific key setting (LLM_API_KEY always wins)
    requires_key: bool
    llm_model: Optional[str] = None
    summarizer_model: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_dimension: Optional[int] = None  # valid for ``embedding_model`` only
    embedding_max_input_chars: Optional[int] = None
    embedding_max_batch_size: Optional[int] = None
    embedding_input_type: Optional[str] = None  # default ``input_type`` sent; None = never send
    key_prefix: Optional[str] = None
    legacy_llm_model_env: Optional[str] = None
    legacy_summarizer_model_env: Optional[str] = None
    legacy_embedding_model_env: Optional[str] = None


PRESETS: dict[str, ProviderPreset] = {
    p.name: p
    for p in (
        ProviderPreset(
            name="openai",
            label="OpenAI",
            display_name="OpenAI API",
            base_url="https://api.openai.com/v1",
            key_env="OPENAI_API_KEY",
            requires_key=True,
            llm_model="gpt-4o",
            summarizer_model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
            embedding_dimension=1536,
        ),
        ProviderPreset(
            name="nvidia",
            label="NVIDIA",
            display_name="NVIDIA NIM API",
            base_url="https://integrate.api.nvidia.com/v1",
            key_env="NVIDIA_API_KEY",
            requires_key=True,
            llm_model="meta/llama-3.1-70b-instruct",
            summarizer_model="meta/llama-3.1-8b-instruct",
            embedding_model="nvidia/nv-embedcode-7b-v1",
            embedding_dimension=4096,
            # nv-embedcode has a per-request TOKEN limit (~1k). Probed against the
            # live API with real code: <=3072 chars OK, a dense 4096-char chunk ->
            # 500. 2048 keeps a safe margin. Re-index after changing it.
            embedding_max_input_chars=2048,
            embedding_max_batch_size=8,
            embedding_input_type="query",
            key_prefix="nvapi-",
            legacy_llm_model_env="NVIDIA_LLM_MODEL",
            legacy_summarizer_model_env="NVIDIA_SUMMARIZER_MODEL",
            legacy_embedding_model_env="NVIDIA_EMBEDDING_MODEL",
        ),
        ProviderPreset(
            name="openrouter",
            label="OpenRouter",
            display_name="OpenRouter API",
            base_url="https://openrouter.ai/api/v1",
            key_env="OPENROUTER_API_KEY",
            requires_key=True,
            llm_model="meta-llama/llama-3.1-70b-instruct",
            summarizer_model="meta-llama/llama-3.1-8b-instruct",
            key_prefix="sk-or-",
        ),
        ProviderPreset(
            name="groq",
            label="Groq",
            display_name="Groq API",
            base_url="https://api.groq.com/openai/v1",
            key_env="GROQ_API_KEY",
            requires_key=True,
            llm_model="llama-3.3-70b-versatile",
            summarizer_model="llama-3.1-8b-instant",
            key_prefix="gsk_",
        ),
        ProviderPreset(
            name="together",
            label="Together",
            display_name="Together AI API",
            base_url="https://api.together.xyz/v1",
            key_env="TOGETHER_API_KEY",
            requires_key=True,
            llm_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            summarizer_model="meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            embedding_model="BAAI/bge-base-en-v1.5",
            embedding_dimension=768,
        ),
        ProviderPreset(
            name="deepseek",
            label="DeepSeek",
            display_name="DeepSeek API",
            base_url="https://api.deepseek.com/v1",
            key_env="DEEPSEEK_API_KEY",
            requires_key=True,
            llm_model="deepseek-chat",
            summarizer_model="deepseek-chat",
        ),
        ProviderPreset(
            name="mistral",
            label="Mistral",
            display_name="Mistral API",
            base_url="https://api.mistral.ai/v1",
            key_env="MISTRAL_API_KEY",
            requires_key=True,
            llm_model="mistral-large-latest",
            summarizer_model="mistral-small-latest",
            embedding_model="mistral-embed",
            embedding_dimension=1024,
        ),
        ProviderPreset(
            name="ollama",
            label="Ollama",
            display_name="Ollama (local)",
            base_url="http://localhost:11434/v1",
            key_env=None,
            requires_key=False,
            llm_model="llama3.1",
            summarizer_model="llama3.1",
            embedding_model="nomic-embed-text",
            embedding_dimension=768,
        ),
        ProviderPreset(
            name="lmstudio",
            label="LM Studio",
            display_name="LM Studio (local)",
            base_url="http://localhost:1234/v1",
            key_env=None,
            requires_key=False,
        ),
        ProviderPreset(
            name=OPENAI_COMPATIBLE,
            label="OpenAI-compatible",
            display_name="OpenAI-compatible API",
            base_url=None,
            key_env=None,
            requires_key=False,
        ),
    )
}


def _settings(cfg: Any = None) -> Any:
    if cfg is not None:
        return cfg
    from brain.config.settings import settings

    return settings


def _str(cfg: Any, attr: Optional[str]) -> Optional[str]:
    """A string setting, with blank/whitespace treated as unset."""
    if not attr:
        return None
    value = getattr(cfg, attr, None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _int(cfg: Any, attr: str) -> Optional[int]:
    value = getattr(cfg, attr, None)
    try:
        value = int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return value if value and value > 0 else None


def normalize_provider_name(name: Optional[str]) -> str:
    """Canonical provider name: lower-cased, stripped, aliases resolved."""
    value = (name or "").strip().lower()
    return _ALIASES.get(value, value)


def is_openai_compatible(name: Optional[str]) -> bool:
    return normalize_provider_name(name) in PRESETS


def is_known_provider(name: Optional[str]) -> bool:
    canonical = normalize_provider_name(name)
    return canonical in PRESETS or canonical in NATIVE_PROVIDERS


def get_preset(name: Optional[str]) -> Optional[ProviderPreset]:
    return PRESETS.get(normalize_provider_name(name))


def supported_provider_names() -> list[str]:
    """Every accepted DEFAULT_*_PROVIDER spelling (aliases included)."""
    return sorted(set(NATIVE_PROVIDERS) | set(PRESETS) | set(_ALIASES))


def is_local_base_url(base_url: Optional[str]) -> bool:
    """True for loopback, private-network, ``*.local``/``*.localhost``/
    ``*.internal`` and single-label hosts (Docker service names such as
    ``http://ollama:11434/v1``) — endpoints that typically need no API key."""
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return ip.is_loopback or ip.is_private or ip.is_unspecified or ip.is_link_local


def base_url_host(base_url: Optional[str]) -> Optional[str]:
    """Host[:port] of a base URL — safe to display (never userinfo/path/query)."""
    if not base_url:
        return None
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname
        if not host:
            return None
        return f"{host}:{parsed.port}" if parsed.port else host
    except ValueError:
        return None


def join_url(base_url: str, path: str) -> str:
    """``base_url`` (version path included) + endpoint path, slash-safe."""
    return base_url.rstrip("/") + "/" + path.lstrip("/")


@dataclass(frozen=True)
class ResolvedEndpoint:
    provider: str  # canonical preset name
    preset: ProviderPreset
    base_url: Optional[str]
    api_key: Optional[str]
    key_source: Optional[str]  # setting that supplied (or would supply) the key
    requires_key: bool
    model: Optional[str]
    # Embedding-only fields (None for LLM endpoints).
    dimension: Optional[int] = None
    max_input_chars: Optional[int] = None
    max_batch_size: Optional[int] = None
    input_type: Optional[str] = None

    @property
    def label(self) -> str:
        return self.preset.label

    @property
    def base_url_host(self) -> Optional[str]:
        return base_url_host(self.base_url)

    def problems(self) -> list[str]:
        """Configuration errors that make every call fail (no network needed)."""
        issues: list[str] = []
        if not self.base_url:
            issues.append("base URL is not set (LLM_BASE_URL / EMBEDDING_BASE_URL)")
        if not self.model:
            issues.append("model is not set")
        if self.requires_key and not self.api_key:
            issues.append(f"{self.key_source or 'LLM_API_KEY'} is not set")
        return issues


def _require_preset(name: Optional[str]) -> ProviderPreset:
    preset = get_preset(name)
    if preset is None:
        raise ValueError(f"'{name}' is not an OpenAI-compatible provider preset")
    return preset


def _is_active(cfg: Any, attr: str, canonical: str) -> bool:
    return normalize_provider_name(_str(cfg, attr)) == canonical


def _resolve_key(
    cfg: Any, preset: ProviderPreset, universal_attrs: tuple[str, ...]
) -> tuple[Optional[str], Optional[str]]:
    for attr in universal_attrs:
        value = _str(cfg, attr)
        if value:
            return value, attr
    if preset.key_env:
        return _str(cfg, preset.key_env), preset.key_env
    return None, (universal_attrs[-1] if universal_attrs else None)


def resolve_llm_endpoint(
    name: Optional[str], *, model: Optional[str] = None, cfg: Any = None
) -> ResolvedEndpoint:
    cfg = _settings(cfg)
    preset = _require_preset(name)
    active = _is_active(cfg, "DEFAULT_LLM_PROVIDER", preset.name)
    base_url = (_str(cfg, "LLM_BASE_URL") if active else None) or preset.base_url
    api_key, key_source = _resolve_key(cfg, preset, ("LLM_API_KEY",) if active else ())
    if key_source is None:
        key_source = "LLM_API_KEY"
    resolved_model = (
        (model or "").strip()
        or (_str(cfg, "LLM_MODEL") if active else None)
        or _str(cfg, preset.legacy_llm_model_env)
        or preset.llm_model
    )
    return ResolvedEndpoint(
        provider=preset.name,
        preset=preset,
        base_url=base_url,
        api_key=api_key,
        key_source=key_source,
        requires_key=preset.requires_key and not is_local_base_url(base_url),
        model=resolved_model or None,
    )


def resolve_summarizer_endpoint(
    name: Optional[str], *, model: Optional[str] = None, cfg: Any = None
) -> ResolvedEndpoint:
    cfg = _settings(cfg)
    preset = _require_preset(name)
    llm = resolve_llm_endpoint(name, cfg=cfg)
    active = _is_active(cfg, "DEFAULT_LLM_PROVIDER", preset.name)
    resolved_model = (
        (model or "").strip()
        or (_str(cfg, "SUMMARIZER_MODEL") if active else None)
        or _str(cfg, preset.legacy_summarizer_model_env)
        or preset.summarizer_model
        or llm.model
    )
    return ResolvedEndpoint(
        provider=llm.provider,
        preset=preset,
        base_url=llm.base_url,
        api_key=llm.api_key,
        key_source=llm.key_source,
        requires_key=llm.requires_key,
        model=resolved_model or None,
    )


def resolve_embedding_endpoint(name: Optional[str], *, cfg: Any = None) -> ResolvedEndpoint:
    cfg = _settings(cfg)
    preset = _require_preset(name)
    active = _is_active(cfg, "DEFAULT_EMBEDDING_PROVIDER", preset.name)
    shares_llm = active and _is_active(cfg, "DEFAULT_LLM_PROVIDER", preset.name)
    base_url = (
        (_str(cfg, "EMBEDDING_BASE_URL") if active else None)
        or (_str(cfg, "LLM_BASE_URL") if shares_llm else None)
        or preset.base_url
    )
    universal_keys: tuple[str, ...] = ()
    if active:
        universal_keys = ("EMBEDDING_API_KEY", "LLM_API_KEY") if shares_llm else ("EMBEDDING_API_KEY",)
    api_key, key_source = _resolve_key(cfg, preset, universal_keys)
    if key_source is None:
        key_source = "EMBEDDING_API_KEY"

    universal_model = _str(cfg, "EMBEDDING_MODEL") if active else None
    model = universal_model or _str(cfg, preset.legacy_embedding_model_env) or preset.embedding_model

    dimension = _int(cfg, "EMBEDDING_DIMENSION")
    if dimension is None and preset.embedding_dimension:
        # A preset's dimension describes its default model only; an explicitly
        # chosen different model needs EMBEDDING_DIMENSION. Legacy model
        # settings (NVIDIA_EMBEDDING_MODEL) keep the historical preset width.
        if universal_model is None or universal_model == preset.embedding_model:
            dimension = preset.embedding_dimension

    max_input_chars = (_int(cfg, "EMBEDDING_MAX_INPUT_CHARS") if active else None) or preset.embedding_max_input_chars
    max_batch_size = (_int(cfg, "EMBEDDING_BATCH_SIZE") if active else None) or preset.embedding_max_batch_size

    input_type = preset.embedding_input_type
    send_input_type = getattr(cfg, "EMBEDDING_SEND_INPUT_TYPE", None) if active else None
    if send_input_type is True:
        input_type = input_type or "query"
    elif send_input_type is False:
        input_type = None

    return ResolvedEndpoint(
        provider=preset.name,
        preset=preset,
        base_url=base_url,
        api_key=api_key,
        key_source=key_source,
        requires_key=preset.requires_key and not is_local_base_url(base_url),
        model=model or None,
        dimension=dimension,
        max_input_chars=max_input_chars,
        max_batch_size=max_batch_size,
        input_type=input_type,
    )


_TASK_ATTRS = {
    "classification": "LLM_TASK_CLASSIFICATION_MODEL",
    "summarization": "LLM_TASK_SUMMARIZATION_MODEL",
    "synthesis": "LLM_TASK_SYNTHESIS_MODEL",
    "insight": "LLM_TASK_INSIGHT_MODEL",
}


def resolve_task_model(task: str, provider_name: Optional[str] = None, *, cfg: Any = None) -> str:
    """Model for one routed task kind.

    ``LLM_TASK_<KIND>_MODEL`` always wins. Otherwise synthesis uses the main
    model and the other (latency-sensitive) kinds use the small/fast model:
    ``LLM_MODEL`` / ``SUMMARIZER_MODEL`` then the preset defaults. For the
    NVIDIA preset those defaults are exactly the historical task defaults.
    Non-OpenAI-compatible providers keep the historical defaults verbatim.
    """
    cfg = _settings(cfg)
    explicit = _str(cfg, _TASK_ATTRS[task])
    if explicit:
        return explicit
    synthesis = task == "synthesis"
    name = provider_name if provider_name is not None else _str(cfg, "DEFAULT_LLM_PROVIDER")
    preset = get_preset(name)
    if preset is None:
        return LEGACY_SYNTHESIS_TASK_MODEL if synthesis else LEGACY_FAST_TASK_MODEL
    llm_model = _str(cfg, "LLM_MODEL")
    if synthesis:
        candidates = (llm_model, preset.llm_model)
    else:
        candidates = (_str(cfg, "SUMMARIZER_MODEL"), preset.summarizer_model, llm_model, preset.llm_model)
    for candidate in candidates:
        if candidate:
            return candidate
    # Preset-less endpoint with no model configured: report it, calls will fail
    # with a clear "model is not set" error.
    return ""


def llm_provider_ready(cfg: Any = None) -> tuple[bool, str]:
    """Whether DEFAULT_LLM_PROVIDER can make calls, without network.

    Native providers (mock/anthropic/google) report ready — their adapters
    validate their own keys at call time, as before.
    """
    cfg = _settings(cfg)
    name = _str(cfg, "DEFAULT_LLM_PROVIDER") or "mock"
    if not is_openai_compatible(name):
        return True, f"provider '{normalize_provider_name(name)}'"
    endpoint = resolve_llm_endpoint(name, cfg=cfg)
    issues = endpoint.problems()
    if issues:
        return False, "; ".join(issues)
    return True, f"{endpoint.provider} via {endpoint.base_url_host}"


def describe_llm_provider(cfg: Any = None) -> str:
    """Human-facing name of the configured LLM provider (no secrets)."""
    cfg = _settings(cfg)
    name = normalize_provider_name(_str(cfg, "DEFAULT_LLM_PROVIDER") or "mock")
    preset = PRESETS.get(name)
    if preset is None:
        return name
    if preset.name == OPENAI_COMPATIBLE:
        host = base_url_host(_str(cfg, "LLM_BASE_URL"))
        return f"{preset.display_name} ({host})" if host else preset.display_name
    return preset.display_name
