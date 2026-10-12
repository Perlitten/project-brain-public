"""Regression tests for the external-readiness security fixes."""

from unittest.mock import AsyncMock, MagicMock, patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.routers import web_login
from apps.api.routers.app_settings import SettingField, _coerce
from apps.api.schemas import DiffReviewRequest
from brain.config.settings import settings
from brain.onboarding.envfile import _sanitize_value, check_endpoint_host


# --- 1. /health must not leak raw exception text ---------------------------------


@pytest.mark.asyncio
async def test_check_health_reports_exception_class_only():
    from brain.database import session

    secret = "password authentication failed for user brain at 10.0.0.5"
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError(secret)
    redis = MagicMock()
    redis.ping = AsyncMock(side_effect=ConnectionError(secret))
    neo4j = MagicMock()
    neo4j.verify_connectivity = AsyncMock(side_effect=OSError(secret))
    with (
        patch.object(session, "async_engine", engine),
        patch.object(session, "redis_client", redis),
        patch.object(session, "neo4j_driver", neo4j),
    ):
        status = await session.check_health()
    assert status["postgres"] == {"status": "unhealthy", "error": "RuntimeError"}
    assert status["redis"] == {"status": "unhealthy", "error": "ConnectionError"}
    assert status["neo4j"] == {"status": "unhealthy", "error": "OSError"}
    assert secret not in repr(status)


# --- 2. SSRF: provider base URLs -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.10.1:11434",
        "http://[fe80::1]:8000/v1",
        "http://[::ffff:169.254.169.254]/",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://metadata/",
        "http://2852039166/",
        "http://[fd00:ec2::254]/",
    ],
)
def test_base_url_rejects_link_local_and_metadata(url):
    with pytest.raises(ValueError):
        _sanitize_value("LLM_BASE_URL", url)
    with pytest.raises(ValueError):
        _coerce(SettingField("X_URL", "search", "Endpoint", "url", ""), url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000/v1",
        "http://192.168.1.20:11434",
        "http://10.0.0.5:8000/v1",
        "http://host.docker.internal:11434",
        "https://api.openai.com/v1",
    ],
)
def test_base_url_allows_local_and_lan(url):
    assert _sanitize_value("EMBEDDING_BASE_URL", url) == url
    check_endpoint_host("Endpoint", url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[0])


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "https://user:pw@example.com/v1"])
def test_base_url_requires_http_without_credentials(url):
    with pytest.raises(ValueError):
        _sanitize_value("LLM_BASE_URL", url)


# --- 3. oracle_test argv injection ----------------------------------------------


def _fake_run(calls):
    def run(argv, **kwargs):
        calls.append(argv)
        return MagicMock(returncode=0, stdout="", stderr="")

    return run


@pytest.mark.parametrize(
    "factory, method",
    [
        ("brain.autonomy.goal_run:GoalRunService", "_run_subprocess_test"),
        ("brain.autonomy.operator_engine:AutonomousOperatorEngine", "_run_test"),
    ],
)
def test_oracle_test_rejects_options_and_separates_positional(factory, method):
    import importlib

    module_name, cls_name = factory.split(":")
    module = importlib.import_module(module_name)
    obj = getattr(module, cls_name).__new__(getattr(module, cls_name))
    calls = []
    with patch.object(module.subprocess, "run", side_effect=_fake_run(calls)):
        passed, message = getattr(obj, method)("--rootdir=/tmp -p evil")
        assert passed is False and "Rejected" in message
        assert calls == []
        getattr(obj, method)("tests/test_health.py")
    argv = calls[0]
    assert argv[argv.index("--") + 1 :] == ["tests/test_health.py"]


# --- 4. git refs that look like options ----------------------------------------


@pytest.mark.parametrize("field", ["base", "head"])
def test_diff_review_request_rejects_option_like_refs(field):
    with pytest.raises(ValidationError):
        DiffReviewRequest(**{field: "--output=/tmp/x"})
    assert getattr(DiffReviewRequest(**{field: "main~1"}), field) == "main~1"


def test_git_diff_engine_rejects_option_like_revision(tmp_path):
    from brain.insights.git_diff import GitDiffEngine

    engine = GitDiffEngine.__new__(GitDiffEngine)
    engine.repo_path = tmp_path
    with patch("brain.insights.git_diff.subprocess.run") as run:
        with pytest.raises(ValueError, match="option"):
            engine.resolve_revision("--output=/tmp/pwn")
        run.assert_not_called()


# --- 5. CORS never pairs a wildcard with credentials ----------------------------


def test_cors_wildcard_never_allows_credentials():
    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    for m in cors:
        if "*" in m.kwargs["allow_origins"]:
            assert m.kwargs["allow_credentials"] is False
    if cors and "*" in cors[0].kwargs["allow_origins"]:
        response = TestClient(app).get("/health", headers={"Origin": "https://evil.example"})
        assert response.headers.get("access-control-allow-credentials") != "true"


# --- 6. web login throttled per client address ----------------------------------


@pytest.fixture
def _web_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = {k: getattr(settings, k) for k in web_login.KEYS}
    for k in web_login.KEYS:
        setattr(settings, k, None)
    web_login._failures.clear()
    yield
    for k, v in saved.items():
        setattr(settings, k, v)
    web_login._failures.clear()


def test_web_login_throttles_rotating_names_from_one_ip(_web_login):
    client = TestClient(app, raise_server_exceptions=False)
    assert client.put("/api/web-login", json={"user": "owner", "password": "correct horse"}).status_code == 200
    for i in range(web_login.IP_FAIL_LIMIT):
        r = client.post("/api/web-login/verify", json={"user": f"guess{i}", "password": "nope"})
        assert r.status_code == 200 and r.json()["ok"] is False
    blocked = client.post("/api/web-login/verify", json={"user": "owner", "password": "correct horse"})
    assert blocked.status_code == 429
    # X-Forwarded-For is not trusted: spoofing it does not reset the counter.
    spoofed = client.post(
        "/api/web-login/verify",
        json={"user": "owner", "password": "correct horse"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert spoofed.status_code == 429
