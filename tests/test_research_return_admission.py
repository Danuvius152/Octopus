"""Research owns its report; the session owns receipt-first return admission."""

import asyncio
from pathlib import Path

import pytest

from server.sessions.runtime_types import SessionOwnerUnavailable
from tests.session_runtime import fail_destination_owner
from tests.test_research_session_return import returning_research as returning_research


async def test_unconfirmed_report_recovers_original_receipt_after_owner_failure_without_reading_material(
    returning_research, monkeypatch,
):
    db, manager, research, _, release, processed = returning_research
    session = await manager.create_session((await db.get_system_agent())["id"], "Receipt survives report material")
    await manager.interrupt(session.id)
    submit, lookup = manager.start_message, manager.source_receipt
    submissions = []
    readable = False

    async def lose_ack(*args, **kwargs):
        receipt = await submit(*args, **kwargs)
        submissions.append(receipt.request_id)
        raise TimeoutError("Original report admission acknowledgment lost")

    async def observe(*args, **kwargs):
        if submissions and not readable:
            raise SessionOwnerUnavailable("Original receipt is temporarily unavailable")
        return await lookup(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", lose_ack)
    monkeypatch.setattr(manager, "source_receipt", observe)
    job = await research.start(session.id, "One original report")
    task = research._tasks[job["id"]]
    release.set()
    await asyncio.wait_for(asyncio.shield(task), 3)
    saved = await db.get_research_job(job["id"])
    assert saved["status"] == "completed" and saved["injection_status"] == "unknown"
    Path(saved["report_path"]).unlink()
    await fail_destination_owner(manager, session, monkeypatch)
    readable = True

    class DeletedReport:
        @property
        def report(self):
            pytest.fail("An admitted report must not prepare deleted source material")

    await research._inject_report(job["id"], session.id, job["question"], DeletedReport())
    await research.wait_for_idle()
    recovered = await db.get_research_job(job["id"])
    assert recovered["status"] == "completed" and recovered["injection_status"] == "delivered"
    assert recovered["delivery_receipt"]["request_id"] == submissions[0]
    assert len(submissions) == 1 and processed.empty()
