"""First-use onboarding: readiness steps, env-file writes (non-secret only),
and the /api/setup surface — all faked at the session/settings boundary."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

import apps.api.helpers as api_helpers
import apps.api.routers.setup as api_setup
from brain.database.models import ContextPack, IndexingRun, Repository
from brain.onboarding import envfile, provider_check, readiness, setup_state

client = TestClient(app, raise_server_exceptions=False)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _result(*, scalar=None, scalar_one_or_none=None, scalars_all=None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_one_or_none
    result.scalar.return_value = scalar
    result.scalars.return_value.all.return_value = scalars_all or []
    return result


def _factory_for(session):
    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


def _healthy(status="healthy"):
    return {
        name: {"status": status, "message": "ok"}
        for name in ("postgres", "redis", "neo4j")
    }


def _repo(path="/repos/brain"):
    repo = MagicMock(spec=Repository)
    repo.id = 7
    repo.path = path
    return repo


def _indexing_run(status="completed", indexed=540, failed=0, discovered=550,
                  commit="abcdef123456"):
    run = MagicMock(spec=IndexingRun)
    run.id = 42
    run.status = status
    run.commit_hash = commit
    run.file_counts = {
        "indexed": indexed,
        "failed": failed,
        "discovered": discovered,
    }
    return run


def _pack(tmp_path, repo_commit="abcdef123456", content="# pack\n", name="pack.md"):
    """A ContextPack row pointing at a real artifact file."""
    artifact = tmp_path / name
    artifact.write_text(content)
    pack = MagicMock(spec=ContextPack)
    pack.path = str(artifact)
    pack.repo_commit = repo_commit
    return pack


# ---- envfile -----------------------------------------------------------


def test_envfile_writes_and_rewrites_only_allowed_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-keep-me\nTARGET_REPO_PATH=/old\nOTHER=1\n")
    applied = envfile.update_env_file(
        env, {"TARGET_REPO_PATH": "/new", "DEFAULT_LLM_PROVIDER": "anthropic"}
    )
    assert applied == {
        "TARGET_REPO_PATH": "/new",
        "DEFAULT_LLM_PROVIDER": "anthropic",
    }
    text = env.read_text()
    assert "TARGET_REPO_PATH=/new" in text
    assert "DEFAULT_LLM_PROVIDER=anthropic" in text
    assert "OPENAI_API_KEY=sk-keep-me" in text  # untouched


def test_envfile_rejects_keys_outside_the_allowlist(tmp_path):
    env = tmp_path / ".env"
    try:
        envfile.update_env_file(env, {"OPENAI_API_KEY": "sk-no"})
    except ValueError as exc:
        assert "writable setup key" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("secret keys must never be writable")
    assert not env.exists()


def test_envfile_rejects_multiline_values(tmp_path):
    try:
        envfile.update_env_file(tmp_path / ".env", {"TARGET_REPO_PATH": "/a\nB=c"})
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("multiline values must be rejected")


# ---- readiness ---------------------------------------------------------


def _collect(*, health=None, repo=None, run=None, packs=None, total_packs=None,
             state=None, client_activity=None):
    """Drive collect_setup_status with a fake session: get_repository_by_path
    opens its own session (patched directly), then the IndexingRun list, the
    repo-scoped pack list and the total pack count run on the shared factory
    session, in that order. Setup-state reads are faked too."""
    packs = packs or []
    if total_packs is None:
        total_packs = len(packs)
    queries = [_result(scalar=total_packs)]
    if repo is not None:
        queries.insert(0, _result(scalars_all=packs))
        queries.insert(0, _result(scalars_all=[run] if run else []))
    session = AsyncMock()
    session.execute.side_effect = queries
    with (
        patch.object(readiness, "check_health", new=AsyncMock(return_value=health or _healthy())),
        patch.object(readiness, "async_session_factory", new=_factory_for(session)),
        patch.object(readiness, "get_repository_by_path", new=AsyncMock(return_value=repo)),
        patch.object(readiness, "resolve_repo_path", return_value="/repos/brain") as resolver,
        patch.object(readiness, "load_state", return_value=state or {}),
        patch.object(readiness, "last_client_activity", return_value=client_activity),
    ):
        status = _run(readiness.collect_setup_status())
        resolver.assert_called_once_with(readiness.settings.TARGET_REPO_PATH)
        return status


def test_readiness_blocked_services_is_first_action():
    status = _collect(health=_healthy(status="unhealthy"))
    steps = {s["id"]: s for s in status["steps"]}
    assert steps["services"]["status"] == "blocked"
    assert status["next_step"] == "services"
    assert status["first_use_complete"] is False


def test_readiness_first_use_complete_when_a_pack_exists(tmp_path):
    status = _collect(repo=_repo(), run=_indexing_run(), packs=[_pack(tmp_path)])
    assert status["first_use_complete"] is True
    steps = {s["id"]: s for s in status["steps"]}
    assert steps["indexed"]["status"] == "done"
    assert steps["first_task"]["status"] == "done"
    assert steps["first_task"]["title"] == "Build your first context pack"
    assert "1 usable context pack" in steps["first_task"]["detail"]


def test_readiness_pack_from_another_repository_does_not_complete(tmp_path):
    """The pre-existing pack belongs to a different repository: zero scoped
    packs come back, but the total shows the foreign pack exists."""
    status = _collect(
        repo=_repo(), run=_indexing_run(), packs=[], total_packs=1,
    )
    assert status["first_use_complete"] is False
    step = next(s for s in status["steps"] if s["id"] == "first_task")
    assert step["status"] == "action"
    assert "another repository" in step["detail"]


def test_readiness_switching_repository_resets_completion(tmp_path):
    """Repository A completed first use; switching TARGET_REPO_PATH to B
    must not inherit A's pack."""
    pack_a = _pack(tmp_path)
    complete = _collect(repo=_repo("/repos/a"), run=_indexing_run(), packs=[pack_a])
    assert complete["first_use_complete"] is True
    switched = _collect(
        repo=_repo("/repos/b"), run=_indexing_run(), packs=[], total_packs=1,
    )
    assert switched["first_use_complete"] is False


def test_readiness_pack_with_incomplete_index_does_not_complete(tmp_path):
    status = _collect(
        repo=_repo(), run=_indexing_run(status="failed"), packs=[_pack(tmp_path)],
    )
    assert status["first_use_complete"] is False
    step = next(s for s in status["steps"] if s["id"] == "first_task")
    assert step["status"] == "blocked"
    assert "index never completed" in step["detail"]


def test_readiness_missing_pack_artifact_does_not_complete(tmp_path):
    pack = MagicMock(spec=ContextPack)
    pack.path = str(tmp_path / "deleted-pack.md")
    pack.repo_commit = "abcdef123456"
    status = _collect(repo=_repo(), run=_indexing_run(), packs=[pack])
    assert status["first_use_complete"] is False
    step = next(s for s in status["steps"] if s["id"] == "first_task")
    assert "missing or empty" in step["detail"]


def test_readiness_empty_pack_artifact_does_not_complete(tmp_path):
    status = _collect(
        repo=_repo(), run=_indexing_run(), packs=[_pack(tmp_path, content="")],
    )
    assert status["first_use_complete"] is False
    step = next(s for s in status["steps"] if s["id"] == "first_task")
    assert "missing or empty" in step["detail"]


def test_readiness_stale_pack_does_not_complete(tmp_path):
    """Pack built at commit A while the latest completed index is at B:
    predates the current index revision."""
    status = _collect(
        repo=_repo(),
        run=_indexing_run(commit="newer-commit-999"),
        packs=[_pack(tmp_path, repo_commit="abcdef123456")],
    )
    assert status["first_use_complete"] is False
    step = next(s for s in status["steps"] if s["id"] == "first_task")
    assert "predates the latest index" in step["detail"]


def test_readiness_is_stable_across_refresh(tmp_path):
    """Restart/refresh regression: two consecutive collects agree."""
    first = _collect(repo=_repo(), run=_indexing_run(), packs=[_pack(tmp_path)])
    second = _collect(repo=_repo(), run=_indexing_run(), packs=[_pack(tmp_path)])
    assert first["first_use_complete"] == second["first_use_complete"] is True
    assert [s["status"] for s in first["steps"]] == [s["status"] for s in second["steps"]]


def test_readiness_unknown_pack_revision_does_not_complete(tmp_path):
    status = _collect(repo=_repo(), run=_indexing_run(),
                      packs=[_pack(tmp_path, repo_commit=None)])
    assert status["first_use_complete"] is False
    step = next(s for s in status["steps"] if s["id"] == "first_task")
    assert "revision is unknown" in step["detail"]


def test_readiness_unknown_index_revision_does_not_complete(tmp_path):
    status = _collect(repo=_repo(), run=_indexing_run(commit=None), packs=[_pack(tmp_path)])
    assert status["first_use_complete"] is False


def test_readiness_unindexed_repo_guides_to_index():
    status = _collect(repo=_repo(), run=None, packs=[])
    steps = {s["id"]: s for s in status["steps"]}
    assert steps["indexed"]["status"] == "action"
    assert steps["first_task"]["status"] == "blocked"


def test_readiness_mock_providers_need_no_keys():
    status = _collect()
    provider = next(s for s in status["steps"] if s["id"] == "provider")
    assert provider["status"] == "done"
    assert "mock" in provider["detail"]
    assert provider["llm"]["state"] == "demo"


def test_readiness_agent_step_needs_a_self_check():
    status = _collect()
    agent = next(s for s in status["steps"] if s["id"] == "agent")
    assert agent["status"] == "action"
    assert "no MCP server self-check" in agent["detail"]
    assert agent["external_client"]["connected"] is False


def test_readiness_agent_step_fresh_passed_self_check_is_done():
    check = {
        "status": "ready",
        "checked_at": "2026-10-02T10:00:00+00:00",
        "fingerprint": readiness.config_fingerprint(),
    }
    status = _collect(state={"mcp_self_check": check})
    agent = next(s for s in status["steps"] if s["id"] == "agent")
    assert agent["status"] == "done"
    assert "MCP server self-check passed" in agent["detail"]
    assert "no external client" in agent["detail"]
    assert agent["external_client"]["connected"] is False


def test_readiness_agent_step_stale_self_check_invalidated():
    check = {"status": "ready", "checked_at": "2026-10-01T00:00:00+00:00",
             "fingerprint": "deadbeef"}
    status = _collect(state={"mcp_self_check": check})
    agent = next(s for s in status["steps"] if s["id"] == "agent")
    assert agent["status"] == "action"
    assert "stale" in agent["detail"]


def test_readiness_agent_step_failed_self_check():
    check = {
        "status": "degraded",
        "checked_at": "2026-10-02T10:00:00+00:00",
        "fingerprint": readiness.config_fingerprint(),
    }
    status = _collect(state={"mcp_self_check": check})
    agent = next(s for s in status["steps"] if s["id"] == "agent")
    assert agent["status"] == "action"
    assert "degraded" in agent["detail"]


def test_readiness_agent_step_observed_client_is_done():
    status = _collect(
        client_activity={"client": "claude-code", "version": "1.0",
                         "at": "2026-10-02T10:00:00+00:00"}
    )
    agent = next(s for s in status["steps"] if s["id"] == "agent")
    assert agent["status"] == "done"
    assert "claude-code" in agent["detail"]
    assert agent["external_client"]["connected"] is True


def test_readiness_provider_uses_loaded_settings(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(readiness.settings, "OPENAI_API_KEY", "loaded-from-env-file")
    status = readiness._provider_status("openai", "LLM")
    assert status["configured"] is True
    assert "loaded-from-env-file" not in str(status)
    assert readiness._provider_status("anthropic", "embedding")["configured"] is False


# ---- /api/setup --------------------------------------------------------


def test_api_setup_status_returns_steps():
    fake = {"steps": [], "first_use_complete": False, "next_step": "services"}
    with patch.object(
        api_setup, "collect_setup_status", new=AsyncMock(return_value=fake)
    ):
        response = client.get("/api/setup/status")
    assert response.status_code == 200
    assert response.json()["first_use_complete"] is False


def test_api_setup_config_writes_env_and_patches_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo_dir = tmp_path / "therepo"
    repo_dir.mkdir()
    settings = api_setup.settings
    original = settings.TARGET_REPO_PATH
    fake_status = {"steps": [], "first_use_complete": False, "next_step": None}
    try:
        with patch.object(
            api_setup, "collect_setup_status", new=AsyncMock(return_value=fake_status)
        ):
            response = client.post(
                "/api/setup/config",
                json={"target_repo_path": str(repo_dir), "default_llm_provider": "mock"},
            )
    finally:
        settings.TARGET_REPO_PATH = original
    assert response.status_code == 200
    env = (tmp_path / ".env").read_text()
    from dotenv import dotenv_values

    assert dotenv_values(tmp_path / ".env")["TARGET_REPO_PATH"] == str(repo_dir)
    assert "DEFAULT_LLM_PROVIDER=mock" in env


def test_api_setup_verify_agent_delegates_to_readiness():
    probe = AsyncMock(return_value={"readiness_status": "ready"})
    with patch.object(api_setup, "run_mcp_self_check", new=probe):
        response = client.post("/api/setup/verify-agent")
    assert response.status_code == 200
    assert response.json()["readiness_status"] == "ready"
    probe.assert_awaited_once()


def test_api_setup_config_rejects_non_directory():
    response = client.post(
        "/api/setup/config", json={"target_repo_path": "/definitely/not/here-xyz"}
    )
    assert response.status_code == 400
    assert "not a directory" in response.json()["detail"]


def test_api_setup_config_never_writes_secret_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = client.post(
        "/api/setup/config",
        json={"default_llm_provider": "mock\nOPENAI_API_KEY=sk-smuggled"},
    )
    assert response.status_code == 400
    assert not (tmp_path / ".env").exists()


_PROVIDER_ATTRS = (
    "DEFAULT_LLM_PROVIDER", "DEFAULT_EMBEDDING_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY",
    "LLM_MODEL", "SUMMARIZER_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL", "EMBEDDING_DIMENSION",
)


def _restore_provider_settings():
    settings = api_setup.settings
    saved = {name: getattr(settings, name) for name in _PROVIDER_ATTRS}

    def restore():
        for name, value in saved.items():
            setattr(settings, name, value)

    return restore


def test_api_setup_provider_writes_key_but_never_echoes_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    restore = _restore_provider_settings()
    try:
        response = client.post(
            "/api/setup/provider",
            json={
                "slot": "llm",
                "provider": "custom",
                "base_url": "https://llm.example.com/v1/",
                "model": "my-model",
                "api_key": "sk-secret-value-123",
            },
        )
        assert response.status_code == 200
        body = response.text
        assert "sk-secret-value-123" not in body
        data = response.json()
        assert data["api_key_written"] is True
        assert "LLM_API_KEY" in data["applied"]
        assert data["current"]["llm"]["provider"] == "openai_compatible"
        assert data["current"]["llm"]["base_url"] == "https://llm.example.com/v1"
        assert data["current"]["llm"]["key_set"] is True
        from dotenv import dotenv_values

        env = dotenv_values(tmp_path / ".env")
        assert env["LLM_API_KEY"] == "sk-secret-value-123"
        assert env["DEFAULT_LLM_PROVIDER"] == "openai_compatible"
        assert api_setup.settings.LLM_MODEL == "my-model"
    finally:
        restore()


def test_api_setup_provider_blank_key_keeps_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LLM_API_KEY=sk-keep\n")
    restore = _restore_provider_settings()
    try:
        response = client.post(
            "/api/setup/provider", json={"slot": "llm", "provider": "groq", "api_key": "  "}
        )
        assert response.status_code == 200
        assert response.json()["api_key_written"] is False
        assert "LLM_API_KEY=sk-keep" in (tmp_path / ".env").read_text()
    finally:
        restore()


def test_api_setup_provider_rejects_credentials_in_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    restore = _restore_provider_settings()
    try:
        response = client.post(
            "/api/setup/provider",
            json={"slot": "embedding", "provider": "openai_compatible",
                  "base_url": "https://user:pw@emb.example.com/v1"},
        )
    finally:
        restore()
    assert response.status_code == 400
    assert "pw" not in response.json()["detail"]
    assert not (tmp_path / ".env").exists()


def test_api_setup_provider_rejects_chat_only_preset_for_embeddings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = client.post("/api/setup/provider", json={"slot": "embedding", "provider": "groq"})
    assert response.status_code == 400


def test_api_setup_providers_lists_presets_without_secrets():
    restore = _restore_provider_settings()
    try:
        api_setup.settings.DEFAULT_LLM_PROVIDER = "openai"
        api_setup.settings.LLM_API_KEY = "sk-should-not-leak"
        response = client.get("/api/setup/providers")
    finally:
        restore()
    assert response.status_code == 200
    assert "sk-should-not-leak" not in response.text
    data = response.json()
    names = {p["name"] for p in data["presets"]}
    assert {"openai", "nvidia", "groq", "ollama", "openai_compatible"} <= names
    assert data["current"]["llm"]["key_set"] is True
    groq = next(p for p in data["presets"] if p["name"] == "groq")
    assert groq["embeddings"] is False


def test_envfile_secret_keys_need_explicit_opt_in(tmp_path):
    env = tmp_path / ".env"
    try:
        envfile.update_env_file(env, {"LLM_API_KEY": "sk-x"})
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("secret keys need allow_secrets=True")
    assert envfile.update_env_file(env, {"LLM_API_KEY": "sk-x"}, allow_secrets=True) == {"LLM_API_KEY": "sk-x"}


# ---- setup_state / provider verification / client activity -------------


def test_setup_state_roundtrip_and_bounded_events(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    setup_state.record_self_check("ready", "fp1", {"retrieval_probe": "passed"})
    state = setup_state.load_state()
    assert state["mcp_self_check"]["status"] == "ready"
    assert state["mcp_self_check"]["fingerprint"] == "fp1"
    for i in range(15):
        setup_state.record_client_activity(f"client-{i}", "1.0")
    events = setup_state.load_state()["client_activity"]
    assert len(events) == 10
    assert events[-1]["client"] == "client-14"
    assert setup_state.last_client_activity()["client"] == "client-14"


def test_setup_state_provider_verification_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    setup_state.record_provider_verification(
        "llm:openai", "openai", "fp123", "verified"
    )
    state = setup_state.load_state()["provider_verifications"]["llm:openai"]
    assert state["status"] == "verified"
    assert state["key_fingerprint"] == "fp123"


def test_provider_state_mock_is_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    assert provider_check.provider_state("mock", "llm")["state"] == "demo"


def test_unsupported_provider_kind_never_probes(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(api_setup.settings, "ANTHROPIC_API_KEY", "test-key")
    probe = AsyncMock()
    monkeypatch.setattr(provider_check, "_probe_embedding", probe)
    assert _run(provider_check.verify_provider("anthropic", "embedding"))["state"] == "missing"
    assert _run(provider_check.verify_provider("mock", "unknown"))["state"] == "missing"
    probe.assert_not_awaited()


def test_provider_state_missing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    monkeypatch.delattr(api_setup.settings, "OPENAI_API_KEY", raising=False)
    assert provider_check.provider_state("openai", "llm")["state"] == "missing"


def test_provider_state_configured_then_verified_then_rotated(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    key = "sk-test-aaaa"
    monkeypatch.setattr(api_setup.settings, "OPENAI_API_KEY", key)
    assert provider_check.provider_state("openai", "llm")["state"] == "configured"
    setup_state.record_provider_verification(
        "llm:openai", "openai", provider_check.key_fingerprint(key), "verified"
    )
    assert provider_check.provider_state("openai", "llm")["state"] == "verified"
    # Rotated key: the recorded verdict no longer matches the fingerprint.
    monkeypatch.setattr(api_setup.settings, "OPENAI_API_KEY", "sk-test-bbbb")
    assert provider_check.provider_state("openai", "llm")["state"] == "configured"


def test_provider_state_failed_verification_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    key = "sk-test-cccc"
    monkeypatch.setattr(api_setup.settings, "ANTHROPIC_API_KEY", key)
    setup_state.record_provider_verification(
        "llm:anthropic", "anthropic", provider_check.key_fingerprint(key),
        "failed", "401 invalid x-api-key",
    )
    result = provider_check.provider_state("anthropic", "llm")
    assert result["state"] == "failed"
    assert "401" in result["detail"]
    assert key not in result["detail"]


def test_verify_provider_mock_records_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    result = _run(provider_check.verify_provider("mock", "llm"))
    assert result["state"] == "demo"
    assert setup_state.load_state()["provider_verifications"]["llm:mock"]["status"] == "demo"


def test_verify_provider_masks_credential_in_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    key = "sk-secret-value-123"
    monkeypatch.setattr(api_setup.settings, "OPENAI_API_KEY", key)

    async def _boom(provider):
        raise RuntimeError(f"auth failed for {key}")

    monkeypatch.setattr(provider_check, "_probe_llm", _boom)
    result = _run(provider_check.verify_provider("openai", "llm"))
    assert result["state"] == "failed"
    assert key not in result["detail"]
    recorded = setup_state.load_state()["provider_verifications"]["llm:openai"]
    assert key not in (recorded["error"] or "")


def test_verify_agent_records_self_check(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    fake_runtime = {
        "readiness_status": "ready",
        "retrieval": {"probe_status": "passed", "error": None},
        "stage_timings_ms": {"total": 12.0},
    }
    with patch.object(
        api_helpers, "get_mcp_readiness_status", new=AsyncMock(return_value=fake_runtime)
    ):
        _run(api_helpers.run_mcp_self_check())
    recorded = setup_state.load_state()["mcp_self_check"]
    assert recorded["status"] == "ready"
    assert recorded["fingerprint"] == readiness.config_fingerprint()


def test_self_check_records_the_configuration_it_started_with(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    original_fingerprint = readiness.config_fingerprint()

    async def changed_during_probe(timeout):
        monkeypatch.setattr(readiness.settings, "TARGET_REPO_PATH", "/changed-during-probe")
        return {"readiness_status": "ready", "retrieval": {"probe_status": "passed"}}

    with patch.object(api_helpers, "get_mcp_readiness_status", new=changed_during_probe):
        _run(api_helpers.run_mcp_self_check())
    assert setup_state.load_state()["mcp_self_check"]["fingerprint"] == original_fingerprint
    assert readiness.config_fingerprint() != original_fingerprint


def test_api_setup_verify_provider_returns_probe_results():
    fake = {"llm": {"provider": "mock", "state": "demo"},
            "embedding": {"provider": "mock", "state": "demo"}}
    with patch.object(
        api_setup, "verify_configured_providers", new=AsyncMock(return_value=fake)
    ):
        response = client.post("/api/setup/verify-provider")
    assert response.status_code == 200
    assert response.json()["llm"]["state"] == "demo"


def test_mcp_server_records_client_initialize(tmp_path, monkeypatch):
    """The stdio server's session hook must record a real client handshake."""
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    from apps.mcp_server import server as mcp_server

    class _Info:
        name = "claude-code"
        version = "1.0.0"

    mcp_server._record_external_client(_Info())
    activity = setup_state.last_client_activity()
    assert activity is not None
    assert activity["client"] == "claude-code"
    assert activity["version"] == "1.0.0"
