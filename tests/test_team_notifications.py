"""Review-needed notifications go to the members who can review, in their own inbox and linked chat.

Covers ``brain.team.notifications`` and the ``/team/me/notifications`` and
``/team/me/telegram`` routes: recipients follow the approval rule (project
Reviewers for a project repository, team administrators for system-global
items, never the author), one row per proposal and recipient, a bounded inbox,
self-only reads and writes, and a Telegram link that needs the code the bot
sent to the chat. Solo state writes nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from apps.api.routers import team as team_router
from brain.config.settings import settings
from brain.team import notifications
from brain.team.models import TeamNotification, TeamTelegramLink
from tests.test_memory_proposals import ADMIN, CONTRIBUTOR, READER, REPO, REVIEWER, decision, review_app
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import add_outsider_and_unattached_repo, client_for, revoke_everyone

team_db = team_fixture
pytestmark = pytest.mark.team_state

CHAT = "123456789"


def notification_app(factory, monkeypatch):
    app = review_app(factory, monkeypatch)
    monkeypatch.setattr(team_router, "async_session_factory", factory)
    return app


def telegram_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TEAM_MEMBER_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(settings, "TELEGRAM_ALERT_BOT_TOKEN", "synthetic-token")


async def inbox(client, headers, **params) -> dict:
    response = await client.get("/team/me/notifications", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def propose(client, title="Use one queue", repo_path=REPO) -> int:
    made = await client.post("/decisions", json=decision(title, repo_path=repo_path), headers=CONTRIBUTOR)
    assert made.status_code == 202, made.text
    return made.json()["proposal_id"]


async def rows(factory) -> list[TeamNotification]:
    async with factory() as session:
        return list((await session.execute(select(TeamNotification).order_by(TeamNotification.id))).scalars())


@pytest.mark.asyncio
async def test_project_reviewers_are_notified_and_nobody_else(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    async with client_for(app) as client:
        proposal_id = await propose(client, "Secret title")
        mine = await inbox(client, REVIEWER)
        assert mine["unread_count"] == 1 and mine["team_state"] == "team"
        [entry] = mine["notifications"]
        assert entry["proposal_id"] == proposal_id and entry["proposal_state"] == "pending"
        assert entry["kind"] == notifications.REVIEW_KIND and entry["read_at"] is None
        assert "Secret" not in repr(entry) and "A" in entry["summary"]
        # The reader cannot approve, the contributor wrote it, the administrator is no reviewer of project A.
        for headers in (READER, CONTRIBUTOR, ADMIN):
            assert (await inbox(client, headers))["notifications"] == []


@pytest.mark.asyncio
async def test_system_global_proposals_notify_team_administrators(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    async with client_for(app) as client:
        proposal_id = await propose(client, "Global", repo_path=None)
        assert [n["proposal_id"] for n in (await inbox(client, ADMIN))["notifications"]] == [proposal_id]
        assert (await inbox(client, REVIEWER))["notifications"] == []


@pytest.mark.asyncio
async def test_one_notification_per_proposal_and_recipient(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    async with client_for(app) as client:
        proposal_id = await propose(client)
    assert await notifications.notify_review_needed(factory, proposal_id) == []
    assert await notifications.notify_review_needed(factory, proposal_id) == []
    assert [(row.recipient_principal_id, row.dedupe_key) for row in await rows(factory)] == [
        (3, notifications.review_dedupe_key(proposal_id))]


@pytest.mark.asyncio
async def test_inbox_is_bounded(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    monkeypatch.setattr(notifications, "INBOX_LIMIT", 2)
    async with client_for(app) as client:
        ids = [await propose(client, f"Decision {i}") for i in range(3)]
        kept = [n["proposal_id"] for n in (await inbox(client, REVIEWER))["notifications"]]
    assert kept == [ids[2], ids[1]]


@pytest.mark.asyncio
async def test_members_see_and_mark_only_their_own_notifications(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    async with client_for(app) as client:
        await propose(client)
        await propose(client, "Global", repo_path=None)
        [theirs] = (await inbox(client, REVIEWER))["notifications"]
        [admins] = (await inbox(client, ADMIN))["notifications"]
        # Another member's notification answers like a missing one and stays unread.
        for headers in (READER, ADMIN):
            denied = await client.post(f"/team/me/notifications/{theirs['id']}/read", headers=headers)
            assert denied.status_code == 404 and denied.json()["detail"] == "Notification not found"
        assert (await client.post("/team/me/notifications/read-all", headers=READER)).json() == {"updated": 0}
        assert (await inbox(client, REVIEWER))["unread_count"] == 1
        marked = await client.post(f"/team/me/notifications/{theirs['id']}/read", headers=REVIEWER)
        assert marked.status_code == 200 and marked.json()["read_at"]
        reviewer = await inbox(client, REVIEWER)
        assert reviewer["unread_count"] == 0 and len(reviewer["notifications"]) == 1
        assert (await inbox(client, REVIEWER, unread_only=True))["notifications"] == []
        # Marking everything read touches the caller's rows only.
        assert (await inbox(client, ADMIN))["unread_count"] == 1
        assert (await client.post("/team/me/notifications/read-all", headers=ADMIN)).json() == {"updated": 1}
        assert (await client.post("/team/me/notifications/read-all", headers=ADMIN)).json() == {"updated": 0}
    by_id = {row.id: row for row in await rows(factory)}
    assert by_id[admins["id"]].read_at is not None and by_id[admins["id"]].recipient_principal_id == 4


@pytest.mark.asyncio
async def test_non_members_are_refused(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    await add_outsider_and_unattached_repo(factory)
    outsider = {"X-API-Key": "fixture-5"}
    async with client_for(app) as client:
        assert (await client.get("/team/me/notifications", headers=outsider)).status_code == 403
        assert (await client.post("/team/me/notifications/read-all", headers=outsider)).status_code == 403
        assert (await client.get("/team/me/telegram", headers=outsider)).status_code == 403


@pytest.mark.asyncio
async def test_solo_state_writes_no_notifications(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    async with client_for(app) as client:
        proposal_id = await propose(client)
        async with factory() as session, session.begin():
            await session.execute(TeamNotification.__table__.delete())
        await revoke_everyone(factory)
        assert await notifications.notify_review_needed(factory, proposal_id) == []
        solo = await inbox(client, REVIEWER)
        assert solo == {"notifications": [], "unread_count": 0, "team_state": "solo"}
        link = await client.post("/team/me/telegram", json={"chat_id": CHAT}, headers=REVIEWER)
        assert link.status_code == 409
    assert await rows(factory) == []


@pytest.mark.asyncio
async def test_telegram_link_needs_the_code_and_only_touches_the_caller(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    sent_codes: list[tuple[str, str]] = []

    async def deliver_code(*, chat_id, code):
        sent_codes.append((chat_id, code))
        return {"status": "sent"}

    monkeypatch.setattr("brain.alerts.telegram.deliver_member_link_code", deliver_code)
    notice = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr("brain.alerts.telegram.deliver_member_review_notice", notice)
    async with client_for(app) as client:
        off = await client.get("/team/me/telegram", headers=REVIEWER)
        assert off.json()["available"] is False and off.json()["linked"] is False
        assert (await client.post("/team/me/telegram", json={"chat_id": CHAT}, headers=REVIEWER)).status_code == 409
        telegram_on(monkeypatch)
        bad = await client.post("/team/me/telegram", json={"chat_id": "@someone"}, headers=REVIEWER)
        assert bad.status_code == 400 and not sent_codes
        started = await client.post("/team/me/telegram", json={"chat_id": CHAT}, headers=REVIEWER)
        assert started.status_code == 202, started.text
        assert started.json()["pending"] is True and started.json()["linked"] is False
        assert (await client.post("/team/me/telegram", json={"chat_id": CHAT}, headers=REVIEWER)).status_code == 400
        [(chat, code)] = sent_codes
        assert chat == CHAT
        wrong = "000000" if code != "000000" else "111111"
        refused = await client.post("/team/me/telegram/verify", json={"code": wrong}, headers=REVIEWER)
        assert refused.status_code == 400
        async with factory() as session:
            assert (await session.get(TeamTelegramLink, 3)).attempts == 1  # the failed attempt was committed
        # The code is bound to the member who asked for it.
        assert (await client.post("/team/me/telegram/verify", json={"code": code}, headers=READER)).status_code == 400
        linked = await client.post("/team/me/telegram/verify", json={"code": code}, headers=REVIEWER)
        assert linked.status_code == 200, linked.text
        assert linked.json()["linked"] is True and linked.json()["chat"] == "…6789"
        assert CHAT not in linked.text
        assert (await client.get("/team/me/telegram", headers=READER)).json()["linked"] is False
        proposal_id = await propose(client, "Secret plan")
        notice.assert_awaited_once()
        assert notice.await_args.kwargs == {"chat_id": CHAT, "proposal_id": proposal_id, "kind": "decision",
                                            "repo_path": REPO}
        assert (await client.delete("/team/me/telegram", headers=READER)).json() == {"unlinked": False}
        assert (await client.delete("/team/me/telegram", headers=REVIEWER)).json() == {"unlinked": True}
    async with factory() as session:
        stored = (await session.execute(select(func.count()).select_from(TeamTelegramLink))).scalar_one()
    assert stored == 0


@pytest.mark.asyncio
async def test_telegram_codes_expire_and_attempts_are_bounded(team_db, monkeypatch):
    factory, _, _ = team_db
    app = notification_app(factory, monkeypatch)
    telegram_on(monkeypatch)
    codes: list[str] = []

    async def deliver_code(*, chat_id, code):
        codes.append(code)
        return {"status": "sent"}

    monkeypatch.setattr("brain.alerts.telegram.deliver_member_link_code", deliver_code)

    async def rewind() -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=20)
        async with factory() as session, session.begin():
            await session.execute(update(TeamTelegramLink).values(code_sent_at=past))

    async with client_for(app) as client:
        assert (await client.post("/team/me/telegram", json={"chat_id": CHAT}, headers=REVIEWER)).status_code == 202
        async with factory() as session, session.begin():
            await session.execute(update(TeamTelegramLink).values(
                code_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        expired = await client.post("/team/me/telegram/verify", json={"code": codes[-1]}, headers=REVIEWER)
        assert expired.status_code == 400 and "expired" in expired.json()["detail"]
        assert (await client.get("/team/me/telegram", headers=REVIEWER)).json()["pending"] is False

        await rewind()
        assert (await client.post("/team/me/telegram", json={"chat_id": CHAT}, headers=REVIEWER)).status_code == 202
        wrong = "000000" if codes[-1] != "000000" else "111111"
        for _ in range(notifications.LINK_MAX_ATTEMPTS):
            assert (await client.post("/team/me/telegram/verify", json={"code": wrong},
                                      headers=REVIEWER)).status_code == 400
        late = await client.post("/team/me/telegram/verify", json={"code": codes[-1]}, headers=REVIEWER)
        assert late.status_code == 400 and "tried too often" in late.json()["detail"]
        assert (await client.get("/team/me/telegram", headers=REVIEWER)).json()["linked"] is False

        # A code the bot could not deliver is never left pending.
        await rewind()
        monkeypatch.setattr("brain.alerts.telegram.deliver_member_link_code",
                            AsyncMock(return_value={"status": "failed"}))
        assert (await client.post("/team/me/telegram", json={"chat_id": CHAT}, headers=REVIEWER)).status_code == 502
    async with factory() as session:
        link = await session.get(TeamTelegramLink, 3)
    assert link is not None and link.pending_chat_id is None and link.chat_id is None
