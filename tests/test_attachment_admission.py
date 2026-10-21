"""A message's files are required input, scoped to its own session."""

import asyncio
import json

import pytest

from server.attachments import MAX_FILE_BYTES, get_path, save_upload
from server.config import settings
from tests.test_attachments import attachments_root as attachments_root
from tests.test_runtime_review_races import (
    connect_ws,
    disconnect_ws,
    wait_dispatch_idle,
)
from tests.test_runtime_review_races import runtime as runtime


@pytest.mark.parametrize("problem", ["missing", "foreign", "mixed", "directory", "symlink", "empty", "oversize", "glob", "type"])
async def test_whole_input_is_rejected_before_queue_or_model(runtime, attachments_root, problem):
    manager, database, session, _, starts, events = runtime
    valid = save_upload(session.id, "required.txt", b"Complete input")
    if problem == "foreign":
        identifier = save_upload("another-session", "private.txt", b"Other input").id
    elif problem == "missing" or problem == "mixed":
        identifier = "a" * 12
    elif problem == "glob":
        identifier = "*"
    elif problem == "type":
        identifier = 17
    else:
        identifier = valid.id
        if problem == "symlink":
            target = attachments_root / "outside.txt"
            target.write_text("Not an owned upload")
            valid.path.unlink()
            valid.path.symlink_to(target)
        elif problem == "directory":
            valid.path.unlink()
            valid.path.mkdir()
        elif problem == "empty":
            valid.path.write_bytes(b"")
        else:
            with valid.path.open("wb") as file:
                file.truncate(MAX_FILE_BYTES + 1)
    identifiers = [valid.id, identifier] if problem == "mixed" else [identifier]
    before = await database.load_messages(session.id)
    with pytest.raises(ValueError) as raised:
        await manager.start_message(session.id, "Use all of these files", attachment_ids=identifiers, steerable=True)
    assert getattr(raised.value, "code", None) == "attachment_unavailable"
    assert manager.dispatch_snapshot(session.id)["items"] == []
    assert not manager.session_is_executing(session)
    assert starts.empty() and events == []
    assert await database.load_messages(session.id) == before


async def test_missing_queued_file_fails_without_a_second_model_and_retains_input(runtime, attachments_root):
    manager, database, session, _, starts, events = runtime
    await manager.start_message(session.id, "held blocker")
    await asyncio.wait_for(starts.get(), 5)
    record = save_upload(session.id, "required.txt", b"Do not drop this")
    second = await manager.start_message(session.id, "Read the complete file", attachment_ids=[record.id])
    assert second.accepted
    record.path.unlink()
    await manager.interrupt(session.id)
    # Stop pauses the accepted queue. Resume it explicitly once the captured
    # blocker and its process have retired; no timing sleeps hide the loss.
    await manager.await_session_execution(session.id, timeout=5)
    await manager.resume_dispatch(session.id)
    completion = await asyncio.wait_for(second.wait(), 5)
    await wait_dispatch_idle(manager, session.id)
    assert starts.empty()
    assert completion.outcome.status == "failed"
    assert completion.outcome.code == "attachment_unavailable"
    messages = await database.load_messages(session.id)
    assert not [row for row in messages if row["role"] == "user" and row["content"] == "Read the complete file"]
    result = next(row for row in messages if row["type"] == "result" and row["content"].get("request_id") == second.request_id)
    assert result["tool_input"]["rejected_input"] == {"content": "Read the complete file", "attachment_ids": [record.id]}
    terminal = [event for event in events if event["type"] == "result" and event.get("request_id") == second.request_id]
    assert len(terminal) == 1 and terminal[0]["code"] == "attachment_unavailable"


@pytest.mark.parametrize("identifiers", [[17], ["abcdef123456", None], "abcdef123456", {"id": "abcdef123456"}])
async def test_websocket_does_not_filter_invalid_file_input_into_a_partial_message(runtime, identifiers):
    manager, _, session, _, starts, _ = runtime
    incoming, outgoing, connection = await connect_ws(manager, settings.auth_token)
    try:
        await incoming.put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "session_id": session.id, "content": "Required files",
            "attachment_ids": identifiers, "client_request_id": "malformed-files",
        })})
        async with asyncio.timeout(5):
            while True:
                event = await outgoing.get()
                if event["type"] == "websocket.send":
                    payload = json.loads(event["text"])
                    if payload.get("type") == "submission":
                        break
        assert payload["accepted"] is False and payload["code"] == "attachment_unavailable"
        assert starts.empty() and session._pending_queue == []
    finally:
        await disconnect_ws(incoming, connection)


def test_attachment_selector_is_literal_and_never_follows_symlinks(attachments_root):
    own = save_upload("session", "source.txt", b"Owned")
    assert get_path("session", own.id) == own.path
    assert get_path("session", "*") is None
    outside = attachments_root / "outside.txt"
    outside.write_bytes(b"Outside")
    own.path.unlink()
    own.path.symlink_to(outside)
    assert get_path("session", own.id) is None
