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
    "BRAIN_PUBLIC_URL": "",
    "BRAIN_PUBLIC_HOST": "",
    "BRAIN_MCP_CONFIRM_WRITES": "",
    "BRAIN_TEAM_MODE": "false",
    "BRAIN_EXECUTION_ENABLED": "false",
    "BRAIN_EXECUTION_TOOLS": "{}",
    "BRAIN_EXECUTION_SECRETS": "{}",
    "BRAIN_EXECUTION_EGRESS": "",
    # Single sign-on changes invitation acceptance; tests that need it patch settings.
    "BRAIN_OIDC_ENABLED": "false",
    "BRAIN_OIDC_REQUIRE_FOR_INVITATIONS": "false",
    # Trusting forwarded addresses changes the throttle key; tests that need it patch settings.
    "BRAIN_TRUSTED_PROXY_IPS": "",
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
    # The suite's fixture world lives under tmp_path; production keeps this off.
    "ALLOW_SCRATCH_REPO_ROOTS": "true",
}

for _key, _value in _NEUTRAL_ENV.items():
    os.environ[_key] = _value


@pytest.fixture
def api_principal():
    """Explicit caller for direct handlers, where FastAPI does not resolve Depends."""
    from apps.api.auth import DEV_PRINCIPAL

    return DEV_PRINCIPAL


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


def pytest_configure(config):
    config.addinivalue_line("markers", "team_state: run the real team-state probe instead of the solo stub")


@pytest.fixture(autouse=True)
def _solo_team_state(request, monkeypatch):
    """Tests without a team database run as a team of one.

    The unified team model probes ``team_members`` on every personal request;
    suites that mock the engine or the session factory have no table to probe.
    Tests that provision a team opt back into the real probe with
    ``@pytest.mark.team_state``.
    """
    if request.node.get_closest_marker("team_state"):
        return

    async def solo() -> str:
        return "solo"

    monkeypatch.setattr("apps.api.team_access.current_team_state", solo)
    monkeypatch.setattr("brain.workers.initiator.current_team_state", solo)

    async def no_activity_record(*args, **kwargs) -> None:
        return None

    # Queued jobs append a team activity row; there is no team table to append to here.
    monkeypatch.setattr("apps.api.activity_log.record_job_queued", no_activity_record)
