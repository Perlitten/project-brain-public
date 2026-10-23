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
    "PROJECT_BRAIN_WEBHOOK_TOKEN",
    "PROJECT_BRAIN_WEBHOOK_REF",
    "BRAIN_ORIGIN_POLL_INTERVAL_S",
    "BRAIN_SOURCE_MIRROR_DIR",
    "BRAIN_ORIGIN_REMOTES",
    "BRAIN_PUBLIC_URL",
    "BRAIN_PUBLIC_HOST",
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
    assert "orchestrator" not in response.text.lower()


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
        {"values": {"LEGACY_ORCHESTRATOR_URL": "http://orch:5678"}},  # retired integration key
        {"values": {"TELEGRAM_ALERT_BOT_TOKEN": "has space"}},
        {"values": {"TELEGRAM_ALERT_BOT_TOKEN": ""}},
        {"clear": ["TELEGRAM_ALERT_COOLDOWN_SECONDS"]},  # not clearable
        {"values": {"BRAIN_ORIGIN_REMOTES": '{"/app": "https://github.com/o/r.git"}'}},  # may embed credentials
        {"values": {"PROJECT_BRAIN_GIT_REPO_PATH": "/app"}},  # not exposed
        {"values": {"PROJECT_BRAIN_WEBHOOK_REF": "main"}},  # must be a full ref
        {"values": {"PROJECT_BRAIN_WEBHOOK_REF": "refs/heads/main\nOTHER=1"}},
        {"values": {"BRAIN_ORIGIN_POLL_INTERVAL_S": -1}},
        {"values": {"BRAIN_SOURCE_MIRROR_DIR": "relative/dir"}},
        {"clear": ["PROJECT_BRAIN_WEBHOOK_REF"]},  # not clearable
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


def test_retired_provider_test_endpoint_is_gone():
    assert client.post("/api/settings/test/retired_orchestrator").status_code in (404, 405)


def test_git_section_lists_only_allowlisted_keys():
    response = client.get("/api/settings")
    assert response.status_code == 200
    git = next(s for s in response.json()["sections"] if s["id"] == "git")
    assert git["title"] == "Git & origin sync"
    assert [f["key"] for f in git["fields"]] == [
        "PROJECT_BRAIN_WEBHOOK_TOKEN",
        "PROJECT_BRAIN_WEBHOOK_REF",
        "BRAIN_WEBHOOK_FETCH_SOURCE",
        "BRAIN_WEBHOOK_FETCH_TIMEOUT_S",
        "BRAIN_ORIGIN_POLL_INTERVAL_S",
        "BRAIN_ORIGIN_POLL_TIMEOUT_S",
        "BRAIN_SOURCE_MIRROR_DIR",
    ]
    assert _field(response.json(), "BRAIN_ORIGIN_POLL_INTERVAL_S")["min"] == 0


def test_webhook_secret_is_write_only():
    settings.PROJECT_BRAIN_WEBHOOK_TOKEN = "whsec-very-secret"
    response = client.get("/api/settings")
    assert "whsec-very-secret" not in response.text
    token = _field(response.json(), "PROJECT_BRAIN_WEBHOOK_TOKEN")
    assert token["kind"] == "secret" and token["set"] is True and token["value"] is None

    response = client.patch("/api/settings", json={"values": {"PROJECT_BRAIN_WEBHOOK_TOKEN": "whsec-new-secret"}})
    assert response.status_code == 200, response.text
    assert "whsec-new-secret" not in response.text
    assert settings.PROJECT_BRAIN_WEBHOOK_TOKEN == "whsec-new-secret"


def test_patch_git_settings(tmp_path):
    response = client.patch(
        "/api/settings",
        json={
            "values": {
                "PROJECT_BRAIN_WEBHOOK_REF": "refs/heads/release/1.x",
                "BRAIN_ORIGIN_POLL_INTERVAL_S": 0,
                "BRAIN_SOURCE_MIRROR_DIR": "/var/lib/brain/mirrors",
            }
        },
    )
    assert response.status_code == 200, response.text
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PROJECT_BRAIN_WEBHOOK_REF=refs/heads/release/1.x" in env
    assert "BRAIN_ORIGIN_POLL_INTERVAL_S=0" in env
    assert settings.BRAIN_ORIGIN_POLL_INTERVAL_S == 0
    assert settings.BRAIN_SOURCE_MIRROR_DIR == "/var/lib/brain/mirrors"
    assert any("Restart the workers and scheduler" in note for note in response.json()["notes"])


def test_origin_remotes_are_listed_with_credentials_masked():
    settings.BRAIN_ORIGIN_REMOTES = (
        '{"/app": {"url": "https://x-access-token:ghp_SECRET1@github.com/org/repo.git?token=SECRET2", "branch": "main"},'
        ' "/srv/other": "oauth2:SECRET3@gitlab.com/org/other.git",'
        ' "/srv/ssh": "git@github.com:org/ssh.git",'
        ' "/broken": {"branch": "main"}}'
    )
    response = client.get("/api/settings")
    assert response.status_code == 200
    for secret in ("SECRET1", "SECRET2", "SECRET3", "x-access-token"):
        assert secret not in response.text
    remotes = response.json()["git"]["origin_remotes"]
    assert remotes["error"] is None and remotes["skipped"] == 1
    assert remotes["remotes"] == [
        {"path": "/app", "url": "https://***@github.com/org/repo.git", "branch": "main"},
        {"path": "/srv/other", "url": "***@gitlab.com/org/other.git", "branch": None},
        {"path": "/srv/ssh", "url": "git@github.com:org/ssh.git", "branch": None},
    ]


def test_origin_remotes_invalid_json_is_reported_not_echoed():
    settings.BRAIN_ORIGIN_REMOTES = '{"/app": "https://u:SECRET@h/r.git"'
    response = client.get("/api/settings")
    assert "SECRET" not in response.text
    assert response.json()["git"]["origin_remotes"] == {"remotes": [], "skipped": 0, "error": "not valid JSON; ignored"}


def test_webhook_url_uses_public_address():
    settings.BRAIN_PUBLIC_URL = None
    settings.BRAIN_PUBLIC_HOST = None
    git = client.get("/api/settings").json()["git"]
    assert git["webhook_path"] == "/webhooks/git-merge" and git["webhook_url"] is None
    assert git["workflow_example"] == "docs/examples/reindex-on-push.yml"

    settings.BRAIN_PUBLIC_HOST = "brain.example.com"
    assert client.get("/api/settings").json()["git"]["webhook_url"] == "https://brain.example.com/webhooks/git-merge"
    settings.BRAIN_PUBLIC_URL = "https://api.example.com/"
    assert client.get("/api/settings").json()["git"]["webhook_url"] == "https://api.example.com/webhooks/git-merge"
