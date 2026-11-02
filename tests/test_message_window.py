"""A transcript arrives windowed, and pages back by cursor (§4 B2).

46,448 messages live in the production database across 66 sessions, and the
largest single session holds 4,873 — which `GET /api/sessions/{id}` used to
return in full every time the session was opened. It now returns the most
recent `MESSAGE_WINDOW`, plus the two fields a client needs to ask for the rest,
and `GET …/messages?before_seq=` serves the rest.

The boundary is what these tests are for: the window must end where the cursor
starts, with nothing skipped and nothing served twice.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.database import Database
from server.main import app
from server.routers.sessions import MESSAGE_WINDOW
from server.session_manager import session_manager
from server.users import UserManager


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="window-owner", password="password-for-transcripts")
    deps.set_user_manager(users)
    headers = {"Authorization": "Bearer " + await users.issue_token(user["id"])}
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c

    await db.close()


async def _imported(client, count: int) -> str:
    """A session with `count` user messages, each naming its own index."""
    resp = await client.post(
        "/api/sessions/import",
        json={
            "name": "Long",
            "messages": [
                {"role": "user", "type": "text", "content": f"msg-{i}"}
                for i in range(count)
            ],
        },
        headers=client.headers,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_a_short_transcript_arrives_whole(client):
    sid = await _imported(client, 5)
    data = (await client.get(f"/api/sessions/{sid}", headers=client.headers)).json()
    assert [m["content"] for m in data["messages"]] == [f"msg-{i}" for i in range(5)]
    assert data["oldest_loaded_seq"] == 0
    # Nothing before seq 0, so the client must not ask.
    assert data["has_more_messages"] is False
    assert data["next_message_seq"] == 5


@pytest.mark.asyncio
async def test_an_empty_transcript_has_no_cursor(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/sessions", json={"agent_id": agent["id"], "name": "Empty"}, headers=client.headers
    )
    sid = resp.json()["id"]
    data = (await client.get(f"/api/sessions/{sid}", headers=client.headers)).json()
    assert data["messages"] == []
    assert data["oldest_loaded_seq"] is None
    assert data["has_more_messages"] is False


@pytest.mark.asyncio
async def test_a_long_transcript_arrives_windowed_to_the_newest(client):
    total = MESSAGE_WINDOW + 40
    sid = await _imported(client, total)
    data = (await client.get(f"/api/sessions/{sid}", headers=client.headers)).json()

    assert len(data["messages"]) == MESSAGE_WINDOW
    # The *newest* window, not the first page: the conversation opens where the
    # user left it.
    assert data["messages"][-1]["content"] == f"msg-{total - 1}"
    assert data["messages"][0]["content"] == f"msg-{total - MESSAGE_WINDOW}"
    assert data["oldest_loaded_seq"] == total - MESSAGE_WINDOW
    assert data["has_more_messages"] is True
    # The dedup baseline still counts the whole transcript, not the window.
    assert data["next_message_seq"] == total
    assert data["message_count"] == total


@pytest.mark.asyncio
async def test_the_cursor_returns_the_page_before_it_with_no_gap(client):
    total = MESSAGE_WINDOW + 40
    sid = await _imported(client, total)
    first = (await client.get(f"/api/sessions/{sid}", headers=client.headers)).json()

    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": first["oldest_loaded_seq"]},
            headers=client.headers,
        )
    ).json()
    assert len(page["messages"]) == 40
    assert page["messages"][0]["content"] == "msg-0"
    # Ends exactly where the window began — no gap, no overlap.
    assert page["messages"][-1]["seq"] == first["oldest_loaded_seq"] - 1
    assert page["oldest_loaded_seq"] == 0
    assert page["has_more_messages"] is False

    # The two halves reconstruct the transcript exactly once.
    joined = [m["content"] for m in page["messages"] + first["messages"]]
    assert joined == [f"msg-{i}" for i in range(total)]


@pytest.mark.asyncio
async def test_the_page_size_is_capped_and_honours_limit(client):
    sid = await _imported(client, 50)
    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": 50, "limit": 10},
            headers=client.headers,
        )
    ).json()
    assert len(page["messages"]) == 10
    # The newest ten below the cursor, so paging backwards is contiguous.
    assert page["messages"][-1]["seq"] == 49
    assert page["oldest_loaded_seq"] == 40
    assert page["has_more_messages"] is True

    over = await client.get(
        f"/api/sessions/{sid}/messages",
        params={"before_seq": 50, "limit": 9999},
        headers=client.headers,
    )
    assert over.status_code == 422


@pytest.mark.asyncio
async def test_the_cursor_at_zero_is_the_end_of_the_transcript(client):
    sid = await _imported(client, 5)
    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": 0},
            headers=client.headers,
        )
    ).json()
    assert page["messages"] == []
    assert page["has_more_messages"] is False


@pytest.mark.asyncio
async def test_paging_an_unknown_session_is_404(client):
    resp = await client.get(
        "/api/sessions/nope/messages", params={"before_seq": 10}, headers=client.headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_an_archived_session_is_windowed_and_pages_the_same_way(client):
    total = MESSAGE_WINDOW + 7
    sid = await _imported(client, total)
    assert (
        await client.post(f"/api/sessions/{sid}/archive", headers=client.headers)
    ).status_code == 201

    data = (await client.get(f"/api/sessions/{sid}", headers=client.headers)).json()
    assert data["archived"] is True
    assert len(data["messages"]) == MESSAGE_WINDOW
    assert data["has_more_messages"] is True
    # `len(messages)` stopped being the count when the window landed; both of
    # these used to be derived from it.
    assert data["message_count"] == total
    assert data["next_message_seq"] == total

    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": data["oldest_loaded_seq"]},
            headers=client.headers,
        )
    ).json()
    assert len(page["messages"]) == 7
    assert page["messages"][0]["content"] == "msg-0"


@pytest.mark.asyncio
async def test_paging_requires_auth(client):
    sid = await _imported(client, 3)
    resp = await client.get(f"/api/sessions/{sid}/messages?before_seq=3", headers={"Authorization": ""})
    assert resp.status_code in (401, 403)


async def test_archived_access_looks_up_only_the_requested_binding(client, monkeypatch):
    """Ownership and paging never enumerate unrelated conversations."""
    from unittest.mock import AsyncMock

    sid = await _imported(client, 3)
    db = session_manager.db
    owner = (await db.get_session_binding(sid))["user_id"]
    binding = AsyncMock(wraps=db.get_session_binding)
    monkeypatch.setattr(db, "get_session_binding", binding)
    assert await session_manager.session_belongs_to(sid, owner)
    binding.assert_not_awaited()  # The live owner is already known.
    assert (await client.post(f"/api/sessions/{sid}/archive")).status_code == 201

    enumeration = AsyncMock(side_effect=AssertionError("must query the target binding"))
    monkeypatch.setattr(db, "load_sessions", enumeration)
    binding.reset_mock()
    assert await session_manager.session_belongs_to(sid, owner)
    assert not await session_manager.session_belongs_to(sid, "other-account")
    assert not await session_manager.session_belongs_to("missing", owner)
    assert await session_manager.session_belongs_to("missing", None)
    assert [call.args for call in binding.await_args_list] == [(sid,), (sid,), ("missing",)]
    page = await client.get(f"/api/sessions/{sid}/messages?before_seq=3")
    assert page.status_code == 200, page.text
    assert [message["content"] for message in page.json()["messages"]] == ["msg-0", "msg-1", "msg-2"]
    assert (await client.get(f"/api/sessions/{sid}/messages?before_seq=0")).json()["messages"] == []
    assert (await client.get("/api/sessions/missing/messages?before_seq=0")).status_code == 404
    enumeration.assert_not_awaited()
