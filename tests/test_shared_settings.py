"""Git settings saved in the web UI reach the worker and scheduler through the database."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import brain.database.session as db_session
from brain.config import shared_settings
from brain.config.settings import settings
from brain.database.models import BrainRuntimeSetting

KEYS = ("PROJECT_BRAIN_WEBHOOK_REF", "BRAIN_ORIGIN_POLL_INTERVAL_S", "BRAIN_SOURCE_MIRROR_DIR", "BRAIN_WEBHOOK_FETCH_SOURCE")


@pytest_asyncio.fixture
async def shared_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(BrainRuntimeSetting.__table__.create)
    monkeypatch.setattr(db_session, "async_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(shared_settings, "_last_refresh", 0.0)
    saved = {k: getattr(settings, k) for k in KEYS}
    yield engine
    for k, v in saved.items():
        setattr(settings, k, v)
    await engine.dispose()


@pytest.mark.asyncio
async def test_saved_values_are_applied_in_another_process(shared_db):
    await shared_settings.save_shared_settings(
        {
            "PROJECT_BRAIN_WEBHOOK_REF": "refs/heads/release/1.x",
            "BRAIN_ORIGIN_POLL_INTERVAL_S": 900,
            "BRAIN_SOURCE_MIRROR_DIR": None,
            "BRAIN_WEBHOOK_FETCH_SOURCE": False,
            "TELEGRAM_ALERT_BOT_TOKEN": "never-stored",
        }
    )
    # Simulate the worker: its settings came from its own environment.
    settings.PROJECT_BRAIN_WEBHOOK_REF = "refs/heads/main"
    settings.BRAIN_ORIGIN_POLL_INTERVAL_S = 300
    settings.BRAIN_SOURCE_MIRROR_DIR = "/var/lib/brain/mirrors"
    settings.BRAIN_WEBHOOK_FETCH_SOURCE = True

    applied = await shared_settings.apply_shared_settings()

    assert "TELEGRAM_ALERT_BOT_TOKEN" not in applied
    assert settings.PROJECT_BRAIN_WEBHOOK_REF == "refs/heads/release/1.x"
    assert settings.BRAIN_ORIGIN_POLL_INTERVAL_S == 900
    assert settings.BRAIN_SOURCE_MIRROR_DIR is None
    assert settings.BRAIN_WEBHOOK_FETCH_SOURCE is False

    await shared_settings.save_shared_settings({"BRAIN_ORIGIN_POLL_INTERVAL_S": 600})
    assert (await shared_settings.load_shared_settings())["BRAIN_ORIGIN_POLL_INTERVAL_S"] == 600


@pytest.mark.asyncio
async def test_values_of_the_wrong_type_are_ignored(shared_db):
    from sqlalchemy import text

    async with shared_db.begin() as conn:
        await conn.execute(
            text("INSERT INTO brain_runtime_settings (key, value) VALUES ('BRAIN_ORIGIN_POLL_INTERVAL_S', '\"soon\"')")
        )
    settings.BRAIN_ORIGIN_POLL_INTERVAL_S = 300
    assert await shared_settings.apply_shared_settings() == {}
    assert settings.BRAIN_ORIGIN_POLL_INTERVAL_S == 300
    # A bool is not an int here, and an int is not a bool.
    await shared_settings.save_shared_settings({"BRAIN_ORIGIN_POLL_INTERVAL_S": True, "BRAIN_WEBHOOK_FETCH_SOURCE": 1})
    assert await shared_settings.load_shared_settings() == {}


@pytest.mark.asyncio
async def test_refresh_is_throttled(shared_db):
    await shared_settings.save_shared_settings({"BRAIN_ORIGIN_POLL_INTERVAL_S": 900})
    await shared_settings.refresh_shared_settings()
    assert settings.BRAIN_ORIGIN_POLL_INTERVAL_S == 900

    await shared_settings.save_shared_settings({"BRAIN_ORIGIN_POLL_INTERVAL_S": 1800})
    await shared_settings.refresh_shared_settings()  # within the interval: not re-read
    assert settings.BRAIN_ORIGIN_POLL_INTERVAL_S == 900


@pytest.mark.asyncio
async def test_an_unreachable_database_keeps_the_environment(monkeypatch):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(db_session, "async_session_factory", broken)
    before = settings.BRAIN_ORIGIN_POLL_INTERVAL_S
    assert await shared_settings.apply_shared_settings() == {}
    assert settings.BRAIN_ORIGIN_POLL_INTERVAL_S == before
