"""/api/web-login: open until set, scrypt-hashed, current password required to change."""

from unittest.mock import patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

import pytest
from fastapi.testclient import TestClient

from apps.api.routers import web_login
from brain.config.settings import settings

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = {k: getattr(settings, k) for k in web_login.KEYS}
    for k in web_login.KEYS:
        setattr(settings, k, None)
    web_login._failures.clear()
    yield
    for k, v in saved.items():
        setattr(settings, k, v)
    web_login._failures.clear()


def _set(user="owner", password="correct horse", current=None):
    body = {"user": user, "password": password}
    if current is not None:
        body["current_password"] = current
    return client.put("/api/web-login", json=body)


def test_fresh_install_is_open():
    body = client.get("/api/web-login").json()
    assert body["mode"] == "open" and body["user"] is None
    assert client.post("/api/web-login/verify", json={"user": "a", "password": "b"}).json()["ok"] is False


def test_first_set_needs_no_current_password_and_stores_only_a_hash(tmp_path):
    response = _set()
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "password" and response.json()["user"] == "owner"
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "correct horse" not in env and "correct horse" not in response.text
    assert "WEB_LOGIN_PASSWORD_HASH=scrypt:" in env
    assert "password_hash" not in response.text.lower()


def test_verify_checks_name_and_password():
    _set()
    ok = client.post("/api/web-login/verify", json={"user": "owner", "password": "correct horse"}).json()
    assert ok["ok"] is True and ok["version"] == settings.WEB_LOGIN_VERSION
    for user, password in [("owner", "wrong one"), ("other", "correct horse")]:
        assert client.post("/api/web-login/verify", json={"user": user, "password": password}).json()["ok"] is False


def test_change_requires_current_password_and_rotates_version():
    _set()
    version = settings.WEB_LOGIN_VERSION
    assert _set(password="new password 1").status_code == 403
    assert _set(password="new password 1", current="wrong").status_code == 403
    changed = _set(user="boss", password="new password 1", current="correct horse")
    assert changed.status_code == 200
    assert changed.json()["user"] == "boss" and changed.json()["version"] != version


def test_validation():
    assert _set(user="has space").status_code == 400
    assert _set(password="short").status_code == 400
    assert _set(password=" padded password ").status_code == 400


def test_disable_returns_to_open():
    _set()
    assert client.post("/api/web-login/disable", json={"current_password": "nope"}).status_code == 403
    response = client.post("/api/web-login/disable", json={"current_password": "correct horse"})
    assert response.status_code == 200 and response.json()["mode"] == "open"
    assert client.get("/api/web-login").json()["mode"] == "open"


def test_wrong_passwords_are_throttled_per_login():
    _set()
    for _ in range(web_login.FAIL_LIMIT):
        client.post("/api/web-login/verify", json={"user": "owner", "password": "guess"})
    blocked = client.post("/api/web-login/verify", json={"user": "owner", "password": "correct horse"})
    assert blocked.status_code == 429


def test_hash_roundtrip():
    stored = web_login.hash_password("p@ss word")
    assert web_login.check_password("p@ss word", stored)
    assert not web_login.check_password("p@ss words", stored)
    assert not web_login.check_password("x", "garbage")
