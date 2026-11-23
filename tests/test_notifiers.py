"""Tests for the notifier framework — DB CRUD, manager, webhook."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from server.database import Database
from server.notifiers import NotifierEvent, NotifierManager
from server.notifiers.base import NotifierBase
from server.notifiers.webhook import WebhookNotifier

# ---------------------------------------------------------------------------
# database CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_save_and_load_notifier(db):
    await db.save_notifier(
        notifier_id="n-1",
        type="webhook",
        label="My hook",
        config={"url": "https://example.com/hook"},
        created_at=_now(),
    )
    rows = await db.load_notifiers()
    assert len(rows) == 1
    assert rows[0]["id"] == "n-1"
    assert rows[0]["type"] == "webhook"
    assert rows[0]["label"] == "My hook"
    assert rows[0]["config"] == {"url": "https://example.com/hook"}
    assert rows[0]["enabled"] is True


@pytest.mark.asyncio
async def test_update_notifier(db):
    await db.save_notifier("n-2", "webhook", "Old", {"url": "x"}, _now())
    await db.update_notifier(
        "n-2", label="New", config={"url": "y"}, enabled=False
    )
    rows = await db.load_notifiers()
    assert rows[0]["label"] == "New"
    assert rows[0]["config"] == {"url": "y"}
    assert rows[0]["enabled"] is False


@pytest.mark.asyncio
async def test_delete_notifier(db):
    await db.save_notifier("n-3", "webhook", "L", {"url": "x"}, _now())
    assert await db.delete_notifier("n-3") is True
    assert await db.delete_notifier("n-3") is False
    rows = await db.load_notifiers()
    assert rows == []


# ---------------------------------------------------------------------------
# manager dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_loads_only_enabled(db):
    await db.save_notifier("on", "webhook", "On", {"url": "a"}, _now())
    await db.save_notifier("off", "webhook", "Off", {"url": "b"}, _now(), enabled=False)
    mgr = NotifierManager()
    mgr.set_db(db)
    await mgr.load()
    ids = [n.id for n in mgr.list()]
    assert ids == ["on"]


@pytest.mark.asyncio
async def test_manager_fires_to_all_registered(db, monkeypatch):
    await db.save_notifier("a", "webhook", "A", {"url": "https://a"}, _now())
    await db.save_notifier("b", "webhook", "B", {"url": "https://b"}, _now())
    mgr = NotifierManager()
    mgr.set_db(db)
    await mgr.load()

    sent: list[tuple[str, NotifierEvent]] = []

    async def fake_send(self, event):
        sent.append((self.id, event))

    monkeypatch.setattr(WebhookNotifier, "send", fake_send)

    event = NotifierEvent(
        type="session_idle", title="t", message="m", session_id="s1"
    )
    await mgr.fire(event)

    sent_ids = sorted(s[0] for s in sent)
    assert sent_ids == ["a", "b"]
    assert all(s[1].type == "session_idle" for s in sent)


@pytest.mark.asyncio
async def test_manager_swallows_per_notifier_exceptions(db, monkeypatch, caplog):
    """One bad target must not poison the rest."""
    await db.save_notifier("good", "webhook", "G", {"url": "https://g"}, _now())
    await db.save_notifier("bad", "webhook", "B", {"url": "https://b"}, _now())
    mgr = NotifierManager()
    mgr.set_db(db)
    await mgr.load()

    sent: list[str] = []

    async def maybe_raise(self, event):
        if self.id == "bad":
            try:
                raise ValueError("synthetic-cause-query")
            except ValueError as cause:
                raise RuntimeError("synthetic-wrapper-userinfo") from cause
        sent.append(self.id)

    monkeypatch.setattr(WebhookNotifier, "send", maybe_raise)
    await mgr.fire(NotifierEvent(type="x", title="t", message="m"))
    assert sent == ["good"]
    assert "synthetic-cause-query" not in caplog.text
    assert "synthetic-wrapper-userinfo" not in caplog.text
    assert "Notifier bad (webhook) event=x error=send_error" in caplog.text


# ---------------------------------------------------------------------------
# webhook target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG], ids=["info", "debug"])
@pytest.mark.asyncio
async def test_webhook_delivery_logs_are_safe_per_target(db, monkeypatch, caplog, level):
    real_client = httpx.AsyncClient

    def loopback_client(**kwargs):
        return real_client(trust_env=False, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", loopback_client)
    delivered = {}
    webhook_waiting = asyncio.Event()
    release_webhook = asyncio.Event()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if request_line.startswith(b"GET /unrelated "):
                writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = next(
                int(value) for name, _, value in (line.partition(b":") for line in headers.splitlines())
                if name.lower() == b"content-length"
            )
            target = request_line.split()[1].split(b"?")[0].removeprefix(b"/synthetic-path-").decode()
            delivered[target] = json.loads(await reader.readexactly(content_length))
            if target == "disconnected":
                return
            if target == "accepted":
                webhook_waiting.set()
                await release_webhook.wait()
            status, response_length = {
                "accepted": (b"204 No Content", b"0"),
                "rejected": (b"403 Forbidden", b"0"),
                "partial": (b"502 Bad Gateway", b"10"),
                "redirect": (b"302 Found", b"0"),
            }[target]
            response_headers = b"X-Trace: synthetic-transport-header\r\n"
            if target == "redirect":
                response_headers += b"Location: http://127.0.0.1:synthetic-location\r\n"
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Length: " + response_length + b"\r\n"
                b"Connection: close\r\n" + response_headers + b"\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        targets = ("accepted", "rejected", "disconnected", "partial", "redirect")
        for target in targets:
            url = f"http://synthetic-userinfo:synthetic-password@127.0.0.1:{port}/synthetic-path-{target}?key=synthetic-query"
            await db.save_notifier(target, "webhook", target, {"url": url}, _now())
        manager = NotifierManager()
        manager.set_db(db)
        await manager.load()

        caplog.set_level(level)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(manager.fire(NotifierEvent(type="session_idle", title="Idle", message="Done")))
            try:
                await asyncio.wait_for(webhook_waiting.wait(), timeout=5)
                async with httpx.AsyncClient(timeout=5) as client:
                    assert (await client.get(f"http://127.0.0.1:{port}/unrelated")).status_code == 204
            finally:
                release_webhook.set()

    assert set(delivered) == set(targets)
    assert all(payload["type"] == "session_idle" for payload in delivered.values())
    assert not any(secret in caplog.text for secret in (
        "synthetic-path", "synthetic-query", "synthetic-userinfo", "synthetic-password",
        "synthetic-transport-header", "synthetic-location",
    ))
    assert "Webhook accepted event=session_idle method=POST status=204" in caplog.text
    assert "Webhook rejected event=session_idle method=POST status=403 error=http_status" in caplog.text
    assert "Webhook disconnected event=session_idle method=POST status=None error=protocol_error" in caplog.text
    assert "Webhook partial event=session_idle method=POST status=502 error=protocol_error" in caplog.text
    assert "Webhook redirect event=session_idle method=POST status=302 error=protocol_error" in caplog.text
    assert f"GET http://127.0.0.1:{port}/unrelated" in caplog.text


@pytest.mark.asyncio
async def test_webhook_posts_json(monkeypatch):
    """WebhookNotifier issues an httpx POST with the event payload as JSON."""
    posted: dict = {}
    real_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        posted["method"] = request.method
        posted["url"] = str(request.url)
        posted["json"] = json.loads(request.content)
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )

    n = WebhookNotifier(
        id="w-1", label="L", config={"url": "https://example.com/wh"}
    )
    await n.send(
        NotifierEvent(
            type="session_idle",
            title="Hi",
            message="Done.",
            session_id="s-1",
            session_name="My session",
        )
    )
    assert posted["method"] == "POST"
    assert posted["url"] == "https://example.com/wh"
    assert posted["json"]["type"] == "session_idle"
    assert posted["json"]["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_webhook_skips_when_no_url(caplog):
    n = WebhookNotifier(id="w", label="L", config={})
    await n.send(NotifierEvent(type="x", title="t", message="m"))
    # No exception, just a logged warning.
    assert any("no URL" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_session_manager_fires_idle_notification(monkeypatch):
    """When a session's queue drains, the wired notifier_manager is called."""
    from server.session_manager import Session, SessionManager

    sm = SessionManager()
    fake_mgr = AsyncMock()
    sm.set_notifier_manager(fake_mgr)

    # The webhook belongs to this owner, including when dispatch is paused.
    session = Session(id="s-1", name="My session", working_dir="/tmp", user_id="u-1")
    await sm._fire_session_idle_notification(session)

    fake_mgr.fire.assert_awaited_once()
    fired_event = fake_mgr.fire.call_args.args[0]
    assert fired_event.type == "session_idle"
    assert fired_event.session_id == "s-1"
    assert fired_event.session_name == "My session"
    assert fake_mgr.fire.call_args.args[1] == "u-1"


@pytest.mark.asyncio
async def test_an_event_reaches_only_its_own_accounts_targets():
    """One account's session going idle must not poke another's webhook.

    `fire` sent every event to every registered target, which on a single-user
    install is right and on a site is a notification channel between strangers
    (multi-tenancy.md §7).
    """
    from server.notifiers.manager import NotifierManager

    sent: list[str] = []

    class _Target(NotifierBase):
        type = "webhook"

        def __init__(self, id: str) -> None:
            super().__init__(id=id, label=id, config={})

        async def send(self, event: NotifierEvent) -> None:
            sent.append(self.id)

    mgr = NotifierManager()
    mgr._notifiers = {"a": _Target("a"), "b": _Target("b"), "old": _Target("old")}
    mgr._owner = {"a": "u-1", "b": "u-2", "old": None}

    await mgr.fire(NotifierEvent(type="session_idle", title="t", message="m"), "u-1")
    assert sent == ["a"], sent

    # Pre-accounts: no owner anywhere, and the one operator gets everything.
    sent.clear()
    await mgr.fire(NotifierEvent(type="session_idle", title="t", message="m"))
    assert sorted(sent) == ["a", "b", "old"]
