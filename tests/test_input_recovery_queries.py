"""Saved-input pages derive complete evidence in their database query."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.deps import get_session_manager
from server.routers.sessions import router
from server.sessions.client_submissions import submission_status, submit_client_message
from tests.test_submission_recovery import _submit_paused
from tests.test_submission_recovery import offline_harness as offline_harness
from tests.test_submission_recovery import runtime as runtime


async def test_saved_input_page_does_not_fetch_a_terminal_per_item(runtime, monkeypatch):
    manager, db, session, _, _, _ = runtime
    for number in range(3):
        reply = await _submit_paused(manager, session.id, f"Saved {number}", f"saved-{number}")
        await manager.cancel_request(session.id, reply["request_id"])
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    reads = []
    execute = db.conn.execute

    async def observe(sql, parameters=()):
        if "FROM messages" in sql or "JOIN messages" in sql:
            reads.append(sql)
        return await execute(sql, parameters)

    monkeypatch.setattr(db.conn, "execute", observe)
    bearer = await deps.get_user_manager().issue_token(session.user_id)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
                           headers={"Authorization": f"Bearer {bearer}"}) as client:
        page = await client.get(f"/api/sessions/{session.id}/submissions", params={"limit": 2})
        assert page.status_code == 200
        assert len(page.json()["items"]) == 2 and page.json()["next_cursor"] is not None
        assert len(reads) == 1


async def test_terminal_filtering_precedes_pagination_and_prevents_manual_recovery(runtime):
    manager, db, session, _, _, _ = runtime
    for number in range(5):
        reply = await _submit_paused(manager, session.id, f"Saved {number}", f"saved-{number}")
        await manager.cancel_request(session.id, reply["request_id"])
        if number >= 3:
            # Legacy rows can still say withdrawn beside an exact terminal.
            await db.record_terminal(
                request_id=reply["request_id"], session_id=session.id, seq=number, role="system", type="result",
                content={"request_id": reply["request_id"], "status": "success"}, created_at="2026-10-23T00:00:00Z",
            )
    first, cursor = await db.list_recoverable_submissions(session.id, limit=2)
    assert [row["client_id"] for row in first] == ["saved-2", "saved-1"]
    second, last = await db.list_recoverable_submissions(session.id, limit=2, before=cursor)
    assert [row["client_id"] for row in second] == ["saved-0"] and last is None
    refused = await submit_client_message(manager, session.id, "Do not repeat finished work", [], "recovery", wiki_mode=None,
                                          recovery_of_client_id="saved-4")
    assert not refused["accepted"]
    assert await submission_status(manager, session.id, "recovery") is None


@pytest.mark.parametrize("original_owner", ["notification_failed", "retired"])
async def test_dismissal_commits_without_reopening_or_requiring_its_original_owner(runtime, monkeypatch, original_owner):
    manager, db, session, _, _, _ = runtime
    reply = await _submit_paused(manager, session.id, "Possibly executed input", "uncertain")
    await db.claim_client_submission(session.id, reply["request_id"], reply["request_id"], manager._dispatch_generation)
    await db.complete_client_submission(session.id, reply["request_id"], owner=reply["request_id"], state="unknown")
    await manager.cancel_request(session.id, reply["request_id"])
    owner = manager._session_owners[id(session)]
    if original_owner == "retired":
        await owner.close()
    else:
        from server.sessions.owner_runtime import RecoveryDismissed

        notify = owner.call_runtime

        async def unavailable(command):
            if not isinstance(command, RecoveryDismissed):
                return await notify(command)
            saved = await db.get_client_submission(session.id, "uncertain")
            assert saved["dismissed"]
            raise RuntimeError("Original Owner notification unavailable")
        monkeypatch.setattr(owner, "call_runtime", unavailable)
    before = dict(manager._session_owners)
    assert await manager.dismiss_recoverable_input(session.id, "uncertain")
    assert manager._session_owners == before
    assert not (await db.list_recoverable_submissions(session.id))[0]
    saved = await submission_status(manager, session.id, "uncertain")
    assert saved["execution"]["state"] == "unknown" and not saved["execution"]["recoverable_input"]
