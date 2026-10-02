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

_PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}

_TIMEOUT_SECONDS = 20.0


def key_fingerprint(value: Optional[str]) -> Optional[str]:
    """Truncated SHA-256 of the credential — identity without the secret."""
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _env_key(provider: str) -> Optional[str]:
    return _PROVIDER_KEY_ENV.get((provider or "").strip().lower())


def _credential(provider: str) -> Optional[str]:
    env = _env_key(provider)
    return getattr(settings, env, None) if env else None


def provider_state(provider: str, kind: str) -> Dict[str, Any]:
    """Observed state of one provider slot: demo | missing | configured |
    verified | failed (verified/failed only match the current key value)."""
    name = (provider or "mock").strip().lower() or "mock"
    if name == "mock":
        return {
            "provider": name,
            "state": "demo",
            "detail": "built-in mock — demo mode, not real-provider readiness",
        }
    env = _env_key(name)
    if env is None:
        return {"provider": name, "state": "missing", "detail": f"unknown {kind} provider"}
    credential = _credential(name)
    if not credential:
        return {
            "provider": name,
            "state": "missing",
            "detail": f"{env} is not set",
        }
    recorded = (setup_state.load_state().get("provider_verifications") or {}).get(
        f"{kind}:{name}"
    )
    if recorded and recorded.get("key_fingerprint") == key_fingerprint(credential):
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
    name = (provider or "mock").strip().lower() or "mock"
    slot = f"{kind}:{name}"
    if name == "mock":
        setup_state.record_provider_verification(slot, name, None, "demo")
        return {"provider": name, "state": "demo", "detail": "mock — demo mode"}
    credential = _credential(name)
    if credential is None:
        return provider_state(name, kind)
    try:
        if kind == "embedding":
            await _probe_embedding(name)
        else:
            await _probe_llm(name)
    except Exception as exc:
        masked = _mask_error(exc, credential)
        setup_state.record_provider_verification(
            slot, name, key_fingerprint(credential), "failed", masked
        )
        return {
            "provider": name,
            "state": "failed",
            "detail": masked,
        }
    setup_state.record_provider_verification(slot, name, key_fingerprint(credential), "verified")
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
