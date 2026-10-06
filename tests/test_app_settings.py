"""/api/settings: typed, allowlisted .env writes; secrets are write-only."""

from unittest.mock import patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

import pytest
from fastapi.testclient import TestClient

from brain.config.settings import settings

client = TestClient(app, raise_server_exceptions=False)

TOUCHED = (
    "TELEGRAM_ALERTS_ENABLED",
    "TELEGRAM_ALERT_BOT_TOKEN",
    "TELEGRAM_ALERT_CHAT_ID",
    "TELEGRAM_ALERT_COOLDOWN_SECONDS",
)


@pytest.fixture(autouse=True)
def _restore_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = {k: getattr(settings, k) for k in TOUCHED}
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


def _field(body, key):
    for section in body["sections"]:
        for f in section["fields"]:
            if f["key"] == key:
                return f
    raise AssertionError(key)


def test_get_never_returns_secret_values():
    settings.TELEGRAM_ALERT_BOT_TOKEN = "123:secret-token"
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert "123:secret-token" not in response.text
    token = _field(body, "TELEGRAM_ALERT_BOT_TOKEN")
    assert token["set"] is True and token["value"] is None
    assert {s["id"] for s in body["sections"]} >= {"telegram", "automation"}
    assert "n8n" not in response.text.lower()


def test_patch_writes_env_and_applies_live(tmp_path):
    response = client.patch(
        "/api/settings",
        json={
            "values": {
                "TELEGRAM_ALERTS_ENABLED": True,
                "TELEGRAM_ALERT_CHAT_ID": "-1001234567890",
                "TELEGRAM_ALERT_COOLDOWN_SECONDS": 3600,
                "TELEGRAM_ALERT_BOT_TOKEN": "123:bot-token-abc",
            }
        },
    )
    assert response.status_code == 200, response.text
    assert "bot-token-abc" not in response.text
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "TELEGRAM_ALERTS_ENABLED=true" in env
    assert "TELEGRAM_ALERT_CHAT_ID=-1001234567890" in env
    assert "TELEGRAM_ALERT_COOLDOWN_SECONDS=3600" in env
    assert "TELEGRAM_ALERT_BOT_TOKEN=123:bot-token-abc" in env
    assert settings.TELEGRAM_ALERTS_ENABLED is True
    assert settings.TELEGRAM_ALERT_COOLDOWN_SECONDS == 3600
    assert settings.TELEGRAM_ALERT_BOT_TOKEN == "123:bot-token-abc"


def test_patch_clear_removes_a_secret(tmp_path):
    settings.TELEGRAM_ALERT_BOT_TOKEN = "old"
    response = client.patch("/api/settings", json={"clear": ["TELEGRAM_ALERT_BOT_TOKEN"]})
    assert response.status_code == 200
    assert settings.TELEGRAM_ALERT_BOT_TOKEN is None
    assert "TELEGRAM_ALERT_BOT_TOKEN=''\n" in (tmp_path / ".env").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {"values": {"OPENAI_API_KEY": "sk-x"}},  # not an editable setting
        {"values": {"LLM_API_KEY": "sk-x"}},  # provider keys go through /api/setup
        {"values": {"TELEGRAM_ALERT_COOLDOWN_SECONDS": 1}},  # below range
        {"values": {"TELEGRAM_ALERT_COOLDOWN_SECONDS": "lots"}},
        {"values": {"TELEGRAM_ALERTS_ENABLED": "yes"}},
        {"values": {"TELEGRAM_ALERT_CHAT_ID": "123\nOPENAI_API_KEY=sk"}},
        {"values": {"TELEGRAM_ALERT_CHAT_ID": "not a chat"}},
        {"values": {"N8N_BASE_URL": "http://n8n:5678"}},  # removed with n8n
        {"values": {"TELEGRAM_ALERT_BOT_TOKEN": "has space"}},
        {"values": {"TELEGRAM_ALERT_BOT_TOKEN": ""}},
        {"clear": ["TELEGRAM_ALERT_COOLDOWN_SECONDS"]},  # not clearable
        {},
    ],
)
def test_patch_rejects_bad_input_without_writing(tmp_path, payload):
    response = client.patch("/api/settings", json=payload)
    assert response.status_code in (400, 422), response.text
    assert not (tmp_path / ".env").exists()


def test_patch_preserves_other_env_lines(tmp_path):
    (tmp_path / ".env").write_text("# keep\nOTHER=1\nTELEGRAM_ALERTS_ENABLED=false\n", encoding="utf-8")
    response = client.patch("/api/settings", json={"values": {"TELEGRAM_ALERTS_ENABLED": True}})
    assert response.status_code == 200
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "# keep\nOTHER=1\nTELEGRAM_ALERTS_ENABLED=true\n"


def test_telegram_test_reports_missing_credentials():
    settings.TELEGRAM_ALERT_BOT_TOKEN = None
    settings.TELEGRAM_ALERT_CHAT_ID = None
    response = client.post("/api/settings/test/telegram")
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


def test_n8n_test_endpoint_is_gone():
    assert client.post("/api/settings/test/n8n").status_code in (404, 405)
