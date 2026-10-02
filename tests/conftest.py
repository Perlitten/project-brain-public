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

import os

_NEUTRAL_ENV = {
    "PROJECT_BRAIN_API_KEY": "",
    "PROJECT_BRAIN_WEBHOOK_TOKEN": "",
    "BRAIN_WEB_URL": "",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "NVIDIA_API_KEY": "",
    "DEFAULT_LLM_PROVIDER": "mock",
    "DEFAULT_EMBEDDING_PROVIDER": "mock",
}

for _key, _value in _NEUTRAL_ENV.items():
    os.environ[_key] = _value
