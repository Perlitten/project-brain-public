"""Low-cost provider credential verification for first-use setup.

Env-key presence proves configuration, not credentials. This module runs a
minimal real call through the existing provider adapters (one short
generation / one embedding) and records the outcome in the setup state keyed
by a fingerprint of the key value, so a rotated key invalidates the old
verdict. Errors are sanitized — a credential value can never reach the
response, logs, or the state file.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Dict, Optional

from brain.config.settings import settings

from brain.onboarding import setup_state
from brain.onboarding.envfile import SUPPORTED_SETUP_PROVIDERS

# Native (non-OpenAI-compatible) providers; every OpenAI-compatible preset
# (openai, nvidia, groq, ollama, openai_compatible…) resolves its key, base URL
# and model through brain.llm.presets.
_PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}

_TIMEOUT_SECONDS = 20.0


def key_fingerprint(value: Optional[str]) -> Optional[str]:
    """Truncated SHA-256 of the credential — identity without the secret."""
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _env_key(provider: str) -> Optional[str]:
    from brain.llm.presets import get_preset

    name = (provider or "").strip().lower()
    if name in _PROVIDER_KEY_ENV:
        return _PROVIDER_KEY_ENV[name]
    preset = get_preset(name)
    if preset is not None:
        return preset.key_env or "LLM_API_KEY"
    return None


def _endpoint(provider: str, kind: str):
    from brain.llm.presets import get_preset, resolve_embedding_endpoint, resolve_llm_endpoint

    if get_preset(provider) is None:
        return None
    if kind == "embedding":
        return resolve_embedding_endpoint(provider, cfg=settings)
    return resolve_llm_endpoint(provider, cfg=settings)


def _slot(provider: str, kind: str) -> Dict[str, Any]:
    """Credential identity of one slot.

    ``credential`` is the secret used to mask probe errors; ``identity`` keys
    recorded verdicts (a keyless local endpoint is identified by its base URL
    so changing the endpoint invalidates the old verdict); ``problem`` is a
    configuration error that makes the slot unusable; ``env`` names the setting
    that supplies (or should supply) the key.
    """
    endpoint = _endpoint(provider, kind)
    if endpoint is None:
        env = _env_key(provider)
        credential = getattr(settings, env, None) if env else None
        return {
            "env": env,
            "credential": credential or None,
            "identity": key_fingerprint(credential),
            "problem": f"{env} is not set" if env and not credential else None,
        }
    env = endpoint.key_source or "LLM_API_KEY"
    problem = None
    if not endpoint.base_url:
        problem = ("EMBEDDING_BASE_URL" if kind == "embedding" else "LLM_BASE_URL") + " is not set"
    elif not endpoint.model:
        problem = ("EMBEDDING_MODEL" if kind == "embedding" else "LLM_MODEL") + " is not set"
    elif endpoint.requires_key and not endpoint.api_key:
        problem = f"{env} is not set"
    if endpoint.api_key:
        identity = key_fingerprint(endpoint.api_key)
    elif endpoint.base_url:
        identity = key_fingerprint(f"keyless:{endpoint.base_url}")
    else:
        identity = None
    return {
        "env": env if endpoint.api_key or endpoint.requires_key else f"keyless endpoint {endpoint.base_url_host}",
        "credential": endpoint.api_key,
        "identity": identity,
        "problem": problem,
    }


def provider_state(provider: str, kind: str) -> Dict[str, Any]:
    """Observed state of one provider slot: demo | missing | configured |
    verified | failed (verified/failed only match the current key value)."""
    from brain.llm.presets import normalize_provider_name

    name = normalize_provider_name(provider) or "mock"
    supported = SUPPORTED_SETUP_PROVIDERS.get(
        {"llm": "DEFAULT_LLM_PROVIDER", "embedding": "DEFAULT_EMBEDDING_PROVIDER"}.get(kind, ""),
        frozenset(),
    )
    if name not in supported:
        return {"provider": name, "state": "missing", "detail": f"unsupported {kind} provider"}
    if name == "mock":
        return {
            "provider": name,
            "state": "demo",
            "detail": "built-in mock — demo mode, not real-provider readiness",
        }
    if _env_key(name) is None:
        return {"provider": name, "state": "missing", "detail": f"unknown {kind} provider"}
    slot = _slot(name, kind)
    env = slot["env"]
    if slot["problem"]:
        return {
            "provider": name,
            "state": "missing",
            "detail": slot["problem"],
        }
    recorded = (setup_state.load_state().get("provider_verifications") or {}).get(
        f"{kind}:{name}"
    )
    if recorded and recorded.get("key_fingerprint") == slot["identity"]:
        if recorded.get("status") == "verified":
            return {
                "provider": name,
                "state": "verified",
                "detail": f"{env} verified at {recorded.get('checked_at')}",
            }
        return {
            "provider": name,
            "state": "failed",
            "detail": f"verification failed at {recorded.get('checked_at')}: "
            f"{recorded.get('error') or 'unknown error'}",
        }
    return {
        "provider": name,
        "state": "configured",
        "detail": f"{env} present — credentials not verified yet",
    }


def _mask_error(exc: BaseException, credential: Optional[str]) -> str:
    text = str(exc) or type(exc).__name__
    if credential:
        text = text.replace(credential, "***")
    return text[:300]


async def _probe_llm(provider: str) -> None:
    from brain.llm import get_llm_provider

    llm = get_llm_provider(provider)
    await asyncio.wait_for(
        llm.generate("Reply with the single word: ok"),
        timeout=_TIMEOUT_SECONDS,
    )


async def _probe_embedding(provider: str) -> None:
    from brain.llm import get_embedding_provider

    provider_obj = get_embedding_provider(provider)
    await asyncio.wait_for(provider_obj.embed("setup credential check"), timeout=_TIMEOUT_SECONDS)


async def verify_provider(provider: str, kind: str) -> Dict[str, Any]:
    """Run the cheap live probe and record the verdict. kind: 'llm'|'embedding'."""
    from brain.llm.presets import normalize_provider_name

    name = normalize_provider_name(provider) or "mock"
    slot_name = f"{kind}:{name}"
    observed = provider_state(name, kind)
    if observed["state"] == "missing":
        return observed
    if name == "mock":
        setup_state.record_provider_verification(slot_name, name, None, "demo")
        return {"provider": name, "state": "demo", "detail": "mock — demo mode"}
    if _env_key(name) is None:
        return provider_state(name, kind)
    slot = _slot(name, kind)
    if slot["problem"] or slot["identity"] is None:
        return provider_state(name, kind)
    credential = slot["credential"]
    try:
        if kind == "embedding":
            await _probe_embedding(name)
        else:
            await _probe_llm(name)
    except Exception as exc:
        masked = _mask_error(exc, credential)
        setup_state.record_provider_verification(
            slot_name, name, slot["identity"], "failed", masked
        )
        return {
            "provider": name,
            "state": "failed",
            "detail": masked,
        }
    setup_state.record_provider_verification(slot_name, name, slot["identity"], "verified")
    return {"provider": name, "state": "verified", "detail": "credential probe passed"}


async def verify_configured_providers() -> Dict[str, Any]:
    """Verify whichever real providers are configured, in parallel."""
    llm_name = (settings.DEFAULT_LLM_PROVIDER or "mock").strip().lower()
    emb_name = (settings.DEFAULT_EMBEDDING_PROVIDER or "mock").strip().lower()
    llm_result, emb_result = await asyncio.gather(
        verify_provider(llm_name, "llm"),
        verify_provider(emb_name, "embedding"),
    )
    return {"llm": llm_result, "embedding": emb_result}
