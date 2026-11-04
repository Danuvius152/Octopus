"""App execution accounting outlives an unavailable receipt until retirement."""

from __future__ import annotations

import asyncio

import pytest

from server import app_agent as app_agent_mod
from server.app_agent import AppAgentError
from server.harness import HarnessEvent
from server.sessions.owner import SessionOwnerUnavailable
from tests import test_app_agent as app_fixtures
from tests.fake_run import FakeRunBase
from tests.test_applications import application_model

aam = app_fixtures.aam
am = app_fixtures.am
apps_root = app_fixtures.apps_root
db = app_fixtures.db
mgr = app_fixtures.mgr


async def test_failed_owner_receipt_keeps_app_counted_until_original_backend_retires(
    aam, am, db, mgr, monkeypatch,
):
    build_entered, build_release, _ = application_model(mgr, monkeypatch, entrypoint_exists=False)
    row = await app_fixtures._make_app(am, db)
    await asyncio.wait_for(build_entered.wait(), 3)
    build_release.set()
    await mgr.await_session_execution(row["session_id"], timeout=5)
    monkeypatch.setattr(app_agent_mod, "MAX_ACTIVE_TURNS_PER_APP", 1)

    entered, stream_release = asyncio.Event(), asyncio.Event()
    stopping, stop_release = asyncio.Event(), asyncio.Event()
    starts = []

    class HeldRun(FakeRunBase):
        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            starts.append(prompt)
            entered.set()

        async def stream(self):
            await stream_release.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            stopping.set()
            await stop_release.wait()

    monkeypatch.setattr(mgr, "_make_run", lambda *args, **kwargs: HeldRun())
    read_notice = db.has_delivery_notice

    async def fail_original_owner(session_id, delivery_id):
        found = await read_notice(session_id, delivery_id)
        if delivery_id == "fail-original-app-owner":
            raise RuntimeError("Original app owner failed before backend retirement")
        return found

    monkeypatch.setattr(db, "has_delivery_notice", fail_original_owner)
    waiting = asyncio.create_task(aam.ask(row, message="Keep counted while the failed owner retires"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        conversation = aam.list_conversations(row["id"])[0]
        turn = aam._turns[conversation["id"]]
        with pytest.raises(SessionOwnerUnavailable):
            await mgr.retain_result_notice(turn.session_id, "fail-original-app-owner", "Unconfirmed material")
        await asyncio.wait_for(stopping.wait(), 3)
        with pytest.raises(AppAgentError) as response:
            await asyncio.wait_for(waiting, 3)
        assert response.value.status_code == 502
        assert "owner" in response.value.message.lower()
        assert aam._turns.get(turn.session_id) is turn
        assert aam.list_conversations(row["id"])[0]["running"]
        status = await aam.get_request_status(row["id"], turn.session_id, turn.request_id)
        assert status["state"] == "unknown" and status["waiting_for_input"] is False
        assert turn.observer is not None and not turn.observer.done()
        with pytest.raises(AppAgentError) as refused:
            await aam.begin_turn(row, message="Still over the execution cap")
        assert refused.value.status_code == 429
        assert len(starts) == 1

        stop_release.set()
        await asyncio.wait_for(asyncio.shield(turn.observer), 3)
        assert not aam._turns
        assert not aam.list_conversations(row["id"])[0]["running"]
        assert len(starts) == 1
    finally:
        stop_release.set()
        stream_release.set()
        await asyncio.gather(waiting, return_exceptions=True)
        await mgr.drain_turns(timeout=5)
