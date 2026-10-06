"""Test isolation from a configured checkout.

``brain.config.settings`` loads ``.env`` from the repository root at import
time, so a normally-installed checkout (``.env`` copied from
``.env.example``) leaks operator configuration into the suite — most visibly
``PROJECT_BRAIN_API_KEY``, which turns API auth on and makes every
unauthenticated ``TestClient`` call return 401. Real environment variables
take precedence over ``.env`` values, so assigning neutral values to the
behavior-flipping keys here restores the unconfigured baseline; individual
tests still patch ``settings`` per module as usual.
"""

import asyncio
import os

import pytest

_NEUTRAL_ENV = {
    "PROJECT_BRAIN_API_KEY": "",
    "PROJECT_BRAIN_WEBHOOK_TOKEN": "",
    "BRAIN_WEB_URL": "",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "NVIDIA_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "GROQ_API_KEY": "",
    "TOGETHER_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "LLM_BASE_URL": "",
    "LLM_API_KEY": "",
    "LLM_MODEL": "",
    "SUMMARIZER_MODEL": "",
    "EMBEDDING_BASE_URL": "",
    "EMBEDDING_API_KEY": "",
    "EMBEDDING_MODEL": "",
    "EMBEDDING_DIMENSION": "",
    "EMBEDDING_MAX_INPUT_CHARS": "",
    "EMBEDDING_BATCH_SIZE": "",
    "EMBEDDING_SEND_INPUT_TYPE": "",
    "DEFAULT_LLM_PROVIDER": "mock",
    "DEFAULT_EMBEDDING_PROVIDER": "mock",
}

for _key, _value in _NEUTRAL_ENV.items():
    os.environ[_key] = _value


@pytest.fixture(autouse=True)
def _discard_stale_db_connections():
    """Drop pooled DB connections between tests.

    pytest-asyncio gives every async test its own event loop, and each
    ``TestClient`` runs the app on its own portal loop. An asyncpg
    connection checked into the shared ``async_engine`` pool is bound to the
    loop that opened it, so the next test to check it out dies with
    "attached to a different loop". Discarding the pool after every test
    makes each test open connections on its own loop; ``close=False``
    skips awaiting per-connection closes on loops that are already closed.
    Checked-out connections are not in the pool, so in-flight use is
    unaffected.
    """
    yield
    from brain.database.session import async_engine

    pending = async_engine.dispose(close=False)
    if asyncio.iscoroutine(pending):  # skipped when tests mock the engine
        asyncio.run(pending)
