"""Loading a transcript must not rewind a writer's live message state."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.deps import get_session_manager
from server.main import app
from server.sessions import SessionManager
from tests.accounts import create_account


@pytest.mark.parametrize(("suffix", "read_status"), [
    ("", 200),
    ("/fork-preview?rewind_to_msg_seq=0", 400),
])
async def test_delayed_transcript_read_does_not_erase_a_later_confirmed_message(
    tmp_path, monkeypatch, suffix, read_status,
):
    database = Database(str(tmp_path / "read-projection.sqlite3"))
    await database.initialize()
    _, bearer = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Read snapshot", str(tmp_path))
    captured, release = asyncio.Event(), asyncio.Event()
    read_sequence = database.message_sequence_state
    held = False

    async def hold_completed_read(session_id):
        nonlocal held
        state = await read_sequence(session_id)
        if session_id == session.id and not held:
            held = True
            captured.set()
            await release.wait()
        return state

    # Hold only the delivery of a real, finished SQLite read. A later public
    # question request can commit normally; no query result is fabricated.
    monkeypatch.setattr(database, "message_sequence_state", hold_completed_read)
    app.dependency_overrides[get_session_manager] = lambda: manager
    loading = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + bearer},
        ) as client:
            loading = asyncio.create_task(client.get(f"/api/sessions/{session.id}{suffix}"))
            await asyncio.wait_for(captured.wait(), 3)
            created = await client.post(
                f"/api/sessions/{session.id}/questions",
                json={"questions": [{"question": "Keep the confirmed record?"}]},
            )
            assert created.status_code == 201
            release.set()
            assert (await loading).status_code == read_status

            listed = await client.get("/api/sessions")
            assert listed.status_code == 200
            summary = next(row for row in listed.json() if row["id"] == session.id)
            assert summary["message_count"] == 1
            current = await client.get(f"/api/sessions/{session.id}")
            assert current.json()["message_count"] == 1
            assert current.json()["messages"][0]["type"] == "question_request"
            assert current.json()["next_message_seq"] == 1
    finally:
        release.set()
        if loading is not None:
            await asyncio.gather(loading, return_exceptions=True)
        app.dependency_overrides.pop(get_session_manager, None)
        await database.close()
