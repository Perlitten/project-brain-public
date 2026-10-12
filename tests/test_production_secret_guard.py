"""Production must refuse every credential that ships as a published placeholder."""

import pytest
from pydantic import ValidationError

from brain.config.settings import Settings

SAFE = {
    "POSTGRES_PASSWORD": "a" * 32,
    "NEO4J_PASSWORD": "b" * 32,
    "PROJECT_BRAIN_API_KEY": "c" * 64,
    "PROJECT_BRAIN_WEBHOOK_TOKEN": "d" * 64,
}


def _settings(**overrides):
    return Settings(_env_file=None, ENVIRONMENT="production", **{**SAFE, **overrides})


def test_production_accepts_real_secrets():
    assert _settings().PROJECT_BRAIN_API_KEY == "c" * 64


@pytest.mark.parametrize(
    "name,placeholder",
    [
        ("POSTGRES_PASSWORD", "postgres_password"),
        ("NEO4J_PASSWORD", "neo4j_password"),
        ("PROJECT_BRAIN_API_KEY", "change-me-in-production"),
        ("PROJECT_BRAIN_WEBHOOK_TOKEN", "change-me-to-a-random-64-character-token"),
    ],
)
def test_production_rejects_published_placeholder(name, placeholder):
    with pytest.raises(ValidationError, match=name):
        _settings(**{name: placeholder})


def test_development_tolerates_placeholders():
    s = Settings(_env_file=None, ENVIRONMENT="development", PROJECT_BRAIN_API_KEY="change-me-in-production")
    assert s.PROJECT_BRAIN_API_KEY == "change-me-in-production"
