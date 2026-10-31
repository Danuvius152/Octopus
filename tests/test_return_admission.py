"""Return admission uses the original durable input, never the current caller."""

import asyncio
from dataclasses import replace

import pytest

from server.knowledge import capture, derive
from server.service_health import ServiceUnavailable
from server.sessions.runtime_types import SessionMailboxFull, SessionOwnerUnavailable
from server.sessions.submission import (
    ReturnBody,
    ReturnRefused,
    ReturnSkipped,
    ReturnTarget,
    ReturnUnconfirmed,
    SubmissionReceipt,
)
from server.submission_models import SubmissionConflict
from tests.test_source_return_owner_failure import fail_destination_owner
from tests.test_submission_recovery import runtime as runtime


def target_for(session, *, delivery_id="bg:original", work_id=None):
    binding = replace(capture(session, None, prompt="Original request"), work_id=work_id)
    return ReturnTarget(session.id, "bg", delivery_id, work_id, derive(binding, "Independent work"))


async def test_lost_ack_then_owner_failure_observes_original_input_without_preparing_again(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime
    target = target_for(session)
    submit = manager.start_message
    preparations, submissions = [], []

    async def prepare():
        preparations.append("original material")
        return ReturnBody("Returned result")

    async def lose_ack(*args, **kwargs):
        receipt = await submit(*args, **kwargs)
        submissions.append(receipt.request_id)
        await receipt.wait()
        await fail_destination_owner(manager, session, monkeypatch)
        raise SessionOwnerUnavailable("Original acknowledgment lost")

    monkeypatch.setattr(manager, "start_message", lose_ack)
    original = await manager.admit_return(target, prepare=prepare)
    assert isinstance(original, SubmissionReceipt)
    assert (await original.wait()).outcome.status == "success"
    await manager.archive_session(session.id, replace=False)

    async def deleted_material():
        pytest.fail("An existing receipt must not read deleted source material")

    duplicate = await manager.admit_return(replace(target, owned=None), prepare=deleted_material)
    assert isinstance(duplicate, SubmissionReceipt) and duplicate.request_id == original.request_id
    assert manager.get_session(session.id) is None
    assert preparations == ["original material"] and submissions == [original.request_id]
    assert starts.qsize() == 1
    row = await db.get_source_submission("bg", target.delivery_id, session_id=session.id)
    assert row["request_id"] == original.request_id


async def test_slow_preparation_does_not_block_human_input_or_restore_a_closed_work(runtime):
    from server.mcp_identity import close_work

    manager, db, session, _, starts, _ = runtime
    target = target_for(session, work_id="return-preparation-work")
    preparing, release = asyncio.Event(), asyncio.Event()

    async def prepare():
        preparing.set()
        await release.wait()
        return ReturnBody("A result whose original work closes")

    delivery = asyncio.create_task(manager.admit_return(target, prepare=prepare))
    try:
        await asyncio.wait_for(preparing.wait(), 2)
        human = await asyncio.wait_for(manager.start_message(session.id, "Human correction"), 2)
        assert (await asyncio.wait_for(human.wait(), 2)).outcome.status == "success"
        await manager.interrupt(session.id)
        await asyncio.wait_for(manager.archive_session(session.id, replace=False), 2)
        close_work(target.work_id)
        release.set()
        result = await asyncio.wait_for(delivery, 2)
        assert isinstance(result, ReturnRefused) and result.reason == "admission_closed"
        assert manager.get_session(session.id) is None
        assert await db.get_source_submission("bg", target.delivery_id, session_id=session.id) is None
        assert starts.qsize() == 1
    finally:
        release.set()
        await delivery


async def test_concurrent_preparation_can_repeat_but_commits_only_one_input(runtime):
    manager, _, session, _, starts, _ = runtime
    target = target_for(session)
    await manager.interrupt(session.id)
    both_preparing, release = asyncio.Event(), asyncio.Event()
    preparations = []

    async def prepare():
        preparations.append("pure read")
        if len(preparations) == 2:
            both_preparing.set()
        await release.wait()
        return ReturnBody("Same result")

    deliveries = [asyncio.create_task(manager.admit_return(target, prepare=prepare)) for _ in range(2)]
    try:
        await asyncio.wait_for(both_preparing.wait(), 2)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*deliveries), 2)
        assert all(isinstance(result, SubmissionReceipt) for result in results)
        assert results[0].request_id == results[1].request_id
        assert len(manager.dispatch_snapshot(session.id)["items"]) == 1 and starts.empty()
        await manager.resume_dispatch(session.id)
        assert (await asyncio.wait_for(results[0].wait(), 2)).outcome.status == "success"
        assert starts.qsize() == 1
    finally:
        release.set()
        await asyncio.gather(*deliveries)


async def test_source_skip_still_observes_admission_that_committed_during_preparation(runtime):
    manager, _, session, _, _, _ = runtime
    target = target_for(session)
    await manager.interrupt(session.id)

    async def prepare():
        other = await manager.start_message(session.id, "Already handed off", source="bg", delivery_id=target.delivery_id)
        assert other.accepted
        return ReturnSkipped("The source closed while reading")

    result = await manager.admit_return(target, prepare=prepare)
    assert isinstance(result, SubmissionReceipt) and result.disposition == "duplicate"
    assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [result.request_id]


@pytest.mark.parametrize("failure,kind", [
    (SessionMailboxFull("Capacity before effects"), ReturnRefused),
    (ConnectionError("The original ACK is unavailable"), ReturnUnconfirmed),
])
async def test_known_handoff_failures_do_not_claim_success_or_replay(runtime, monkeypatch, failure, kind):
    manager, db, session, _, starts, _ = runtime
    calls = []

    async def reject(*args, **kwargs):
        calls.append(kwargs["delivery_id"])
        raise failure

    async def prepare():
        return ReturnBody("Returned material")

    monkeypatch.setattr(manager, "start_message", reject)
    result = await manager.admit_return(target_for(session), prepare=prepare)
    assert isinstance(result, kind) and result.cause is failure
    if isinstance(result, ReturnRefused):
        assert result.reason == "capacity"
    assert calls == ["bg:original"] and starts.empty()
    assert await db.get_source_submission("bg", "bg:original", session_id=session.id) is None


@pytest.mark.parametrize("phase", ["initial", "after_prepare"])
async def test_receipt_read_capacity_is_uncertainty_at_every_read_phase(runtime, monkeypatch, phase):
    manager, _, session, _, starts, _ = runtime
    lookup = manager.source_receipt
    unavailable = SessionMailboxFull("Receipt observation is temporarily unavailable")
    reads, preparations = [], []

    async def read(*args, **kwargs):
        reads.append("lookup")
        if len(reads) == (1 if phase == "initial" else 2):
            raise unavailable
        return await lookup(*args, **kwargs)

    async def prepare():
        preparations.append("pure material")
        return ReturnBody("A read error never permits a new handoff")

    monkeypatch.setattr(manager, "source_receipt", read)
    result = await manager.admit_return(target_for(session), prepare=prepare)
    assert isinstance(result, ReturnUnconfirmed) and result.cause is unavailable
    assert preparations == ([] if phase == "initial" else ["pure material"])
    assert len(reads) == (1 if phase == "initial" else 2) and starts.empty()


@pytest.mark.parametrize("failure", [ValueError("Bad material"), TimeoutError("Material read expired")])
async def test_preparation_errors_propagate_without_becoming_admission_outcomes(runtime, failure):
    manager, _, session, _, starts, _ = runtime

    async def prepare():
        raise failure

    with pytest.raises(type(failure)) as raised:
        await manager.admit_return(target_for(session), prepare=prepare)
    assert raised.value is failure and starts.empty()


async def test_concurrent_submissions_with_different_material_propagate_the_real_digest_conflict(runtime, monkeypatch):
    manager, _, session, _, starts, _ = runtime
    target = target_for(session)
    submit = manager.start_message
    await manager.interrupt(session.id)
    both_submitting = asyncio.Event()
    calls = []

    async def simultaneous_submit(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            both_submitting.set()
        await both_submitting.wait()
        return await submit(*args, **kwargs)

    async def first_body():
        return ReturnBody("First result")

    async def different_body():
        return ReturnBody("Conflicting result")

    monkeypatch.setattr(manager, "start_message", simultaneous_submit)
    results = await asyncio.wait_for(asyncio.gather(
        manager.admit_return(target, prepare=first_body),
        manager.admit_return(target, prepare=different_body),
        return_exceptions=True,
    ), 2)
    assert sum(isinstance(result, SubmissionReceipt) for result in results) == 1
    assert sum(isinstance(result, SubmissionConflict) for result in results) == 1
    assert len(manager.dispatch_snapshot(session.id)["items"]) == 1 and starts.empty()


async def test_cancelling_the_handoff_waiter_does_not_revoke_admitted_work(runtime, monkeypatch):
    manager, _, session, _, starts, _ = runtime
    target = target_for(session)
    submit = manager.start_message
    committed, confirmation = asyncio.Event(), asyncio.Event()
    await manager.interrupt(session.id)

    async def delayed_ack(*args, **kwargs):
        receipt = await submit(*args, **kwargs)
        committed.set()
        await confirmation.wait()
        return receipt

    async def prepare():
        return ReturnBody("Work remains owned after the waiter leaves")

    monkeypatch.setattr(manager, "start_message", delayed_ack)
    waiter = asyncio.create_task(manager.admit_return(target, prepare=prepare))
    await asyncio.wait_for(committed.wait(), 2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    async def never_prepare_again():
        pytest.fail("Cancellation did not revoke the input")

    original = await manager.admit_return(target, prepare=never_prepare_again)
    assert isinstance(original, SubmissionReceipt) and starts.empty()
    await manager.resume_dispatch(session.id)
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "success"
    assert starts.qsize() == 1


@pytest.mark.parametrize("failure", [
    SubmissionConflict("Conflicting original identity"),
    ValueError("An unclassified lifecycle defect"),
    ServiceUnavailable("Authoritative storage is untrustworthy"),
])
async def test_unclassified_conflict_and_storage_failures_propagate_without_reconciliation(runtime, monkeypatch, failure):
    manager, _, session, _, starts, _ = runtime
    lookup = manager.source_receipt
    handed_off = False

    async def original_receipt(*args, **kwargs):
        assert not handed_off, "These failures must not read storage to guess a result"
        return await lookup(*args, **kwargs)

    async def fail(*args, **kwargs):
        nonlocal handed_off
        handed_off = True
        raise failure

    async def prepare():
        return ReturnBody("Original material")

    monkeypatch.setattr(manager, "source_receipt", original_receipt)
    monkeypatch.setattr(manager, "start_message", fail)
    with pytest.raises(type(failure)) as raised:
        await manager.admit_return(target_for(session), prepare=prepare)
    assert raised.value is failure and handed_off and starts.empty()
