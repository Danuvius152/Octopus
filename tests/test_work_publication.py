import asyncio

import pytest

from server.sessions import SessionManager
from server.sessions.publication import WorkPublications


@pytest.mark.parametrize("sent_before_release", [False, True])
async def test_release_applies_to_late_sends_with_no_current_tail(sent_before_release):
    publications = WorkPublications()
    source = publications.create_scope("original")
    other = publications.create_scope("unrelated")
    if sent_before_release:
        await publications.publish(source, lambda: asyncio.sleep(0))
        assert not publications.pending
    source.release()
    first_started = asyncio.Event()
    finish_first = asyncio.Event()
    sent: list[str] = []

    async def send(label: str) -> None:
        if label == "first":
            first_started.set()
            await finish_first.wait()
        sent.append(label)

    try:
        await asyncio.wait_for(publications.publish(source, lambda: send("first")), 1)
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(publications.publish(source, lambda: send("second")), 1)
        await asyncio.wait_for(publications.publish(other, lambda: send("other")), 1)
        assert sent == ["other"]
        pending = publications.pending
        assert pending and all(not task.done() for task in pending)
    finally:
        finish_first.set()
        await asyncio.gather(*publications.pending)
    assert sent == ["other", "first", "second"]
    assert not publications.pending


@pytest.mark.parametrize("failure", ["required", "optional", "after_release"])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_send_failure_is_reported_by_its_owner(failure, error_type, caplog):
    publications = WorkPublications()
    source = publications.create_scope("original")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def subscriber(_event) -> None:
        entered.set()
        await finish.wait()
        raise error_type("subscriber failed")

    manager = SessionManager()
    manager.on_broadcast("test-subscriber", subscriber, required=True)
    producer = asyncio.create_task(publications.publish(
        source, lambda: manager._broadcast({"type": "test"}), required=failure != "optional",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        actual_send, = publications.pending
        if failure == "required":
            # Release after the send fails and before the producer can resume.
            actual_send.add_done_callback(lambda _: source.release())
            finish.set()
            with pytest.raises(error_type, match="subscriber failed"):
                await producer
            assert "subscriber failed" not in caplog.text
        else:
            if failure == "after_release":
                source.release()
                await asyncio.wait_for(producer, 1)
                assert not actual_send.done()
            finish.set()
            await producer
            await actual_send
            assert caplog.text.count(f"{error_type.__name__}: subscriber failed") == 1
        assert not publications.pending
    finally:
        finish.set()
        await asyncio.gather(producer, *publications.pending, return_exceptions=True)


async def test_cancelling_waiters_withdraws_unsent_work_and_joins_an_inflight_send(caplog):
    publications = WorkPublications()
    source = publications.create_scope("original")
    entered, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent_after_cancellation = asyncio.Event()

    async def first_send() -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await finish.wait()

    async def second_send() -> None:
        sent_after_cancellation.set()

    first = asyncio.create_task(publications.publish(source, first_send))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(publications.publish(source, second_send))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        first.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not first.done() and publications.pending
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.gather(*publications.pending)
        assert not sent_after_cancellation.is_set()
        assert "Work publication failed" not in caplog.text
    finally:
        finish.set()
        if not first.done() and not first.cancelling():
            first.cancel()
        await asyncio.gather(first, *([second] if second else []), *publications.pending, return_exceptions=True)
