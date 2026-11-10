"""Human steering through browser admission, the owned host and saved evidence."""

import asyncio
import json

import pytest

from server.harness import HarnessRun
from server.sessions.client_submissions import submission_status, submit_client_message
from tests import native_app_server as native_tests
from tests.native_app_server import active_root, wait_until
from tests.session_runtime import wait_dispatch_idle

native_runtime = native_tests.native_runtime


async def test_human_input_steers_the_bound_root_during_native_wait(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    from server.sessions.presentation import session_info
    assert session_info(session).can_steer is True
    first = await active_root(native_runtime, tmp_path)
    submitted = await submit_client_message(manager, session.id, "Use the other file", [], "correction", wiki_mode=None)
    assert submitted["disposition"] == "steered"

    async def accepted():
        saved = await submission_status(manager, session.id, "correction")
        return any(item["state"] == "accepted" for item in saved["execution"]["native_inputs"])

    await wait_until(accepted)
    assert not (await database.list_recoverable_submissions(session.id))[0]
    duplicate = await submit_client_message(manager, session.id, "Use the other file", [], "correction", wiki_mode=None)
    assert duplicate["request_id"] == submitted["request_id"]
    frames = [json.loads(line) for line in (tmp_path / "native-steers.jsonl").read_text().splitlines()]
    assert len(frames) == 1
    assert frames[0]["params"] == {"threadId": "exact-native-thread", "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "Use the other file"}], "clientUserMessageId": submitted["request_id"]}
    assert len(starts) == 1
    (tmp_path / "native-finish").touch()
    await wait_dispatch_idle(manager, session.id)
    saved = await submission_status(manager, session.id, "correction")
    assert saved["execution"]["execution_owner_request_id"] == first["request_id"]
    assert saved["execution"]["native_inputs"][0]["state"] == "accepted"
    assert "processing" in saved["message"].lower()
    assert not saved["execution"]["recoverable_input"]
    notices, _ = await database.list_recoverable_submissions(session.id)
    assert not notices


async def test_explicit_rejection_requeues_original_before_later_human_input(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "steer_reject"
    await active_root(native_runtime, tmp_path)
    original = await submit_client_message(manager, session.id, "Rejected correction", [], "rejected", wiki_mode=None)

    async def requeued():
        return bool(manager.dispatch_snapshot(session.id)["items"])

    await wait_until(requeued)
    later = await submit_client_message(manager, session.id, "Following input", [], "later", wiki_mode=None)
    assert later["disposition"] == "accepted"
    assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [original["request_id"], later["request_id"]]
    mode[0] = "normal"
    (tmp_path / "native-finish").touch()
    await wait_dispatch_idle(manager, session.id)
    saved = await submission_status(manager, session.id, "rejected")
    assert saved["request_id"] == original["request_id"]
    assert saved["execution"]["execution_owner_request_id"] == original["request_id"]
    assert [item["state"] for item in saved["execution"]["native_inputs"]] == ["rejected", "accepted"]
    assert len(starts) == 3
    assert [json.loads(line)["input"][0]["text"] for line in (tmp_path / "native-business.jsonl").read_text().splitlines()] == [
        "Wait for the helper", "Rejected correction", "Following input"]
    rows = await database.load_messages(session.id)
    assert [row["content"] for row in rows if row["role"] == "user"] == ["Wait for the helper", "Rejected correction", "Following input"]


async def test_ninth_input_queues_and_terminal_freezes_writer_before_retirement(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, events = native_runtime
    await active_root(native_runtime, tmp_path)
    writing, release, retiring, retired = (asyncio.Event() for _ in range(4))
    record, stop = database.record_native_input, HarnessRun._stop

    async def gate_record(session_id, request_id, evidence, **kwargs):
        if evidence.kind == "steer" and evidence.state == "unknown":
            writing.set()
            await release.wait()
        await record(session_id, request_id, evidence, **kwargs)

    async def gate_retirement(run):
        retiring.set()
        await retired.wait()
        await stop(run)

    monkeypatch.setattr(database, "record_native_input", gate_record)
    monkeypatch.setattr(HarnessRun, "_stop", gate_retirement)
    try:
        replies = [await submit_client_message(manager, session.id, "Correction 1", [], "c1", wiki_mode=None)]
        await asyncio.wait_for(writing.wait(), 3)
        for i in range(2, 11):
            replies.append(await submit_client_message(manager, session.id, f"Correction {i}", [], f"c{i}", wiki_mode=None))
        assert [item["disposition"] for item in replies] == ["steered"] * 8 + ["accepted"] * 2
        assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [item["request_id"] for item in replies[8:]]
        (tmp_path / "native-finish").touch()
        await asyncio.wait_for(retiring.wait(), 3)
        closing = await submit_client_message(manager, session.id, "After terminal", [], "closing", wiki_mode=None)
        assert closing["disposition"] == "accepted"
        assert len(starts) == 1
        release.set()
        mode[0] = "normal"
    finally:
        release.set()
        retired.set()
    await wait_dispatch_idle(manager, session.id)
    assert not (tmp_path / "native-steers.jsonl").exists()
    assert len(starts) == 12
    delivered = [json.loads(line)["input"][0]["text"] for line in (tmp_path / "native-business.jsonl").read_text().splitlines()]
    assert delivered == ["Wait for the helper", *[f"Correction {i}" for i in range(1, 11)], "After terminal"]
    assert (await submission_status(manager, session.id, "c1"))["request_id"] == replies[0]["request_id"]


@pytest.mark.parametrize("change", ["attachments", "wiki", "model", "background"])
async def test_ineligible_inputs_preserve_ordinary_queue_and_order(native_runtime, tmp_path, change):
    manager, database, session, mode, starts, events = native_runtime
    await active_root(native_runtime, tmp_path)
    files, wiki = [], None
    if change == "attachments":
        from server.attachments import save_upload
        files = [save_upload(session.id, "required.txt", b"Required", user_id=session.user_id).id]
    if change == "wiki":
        wiki = "read"
    if change == "model":
        await manager.update_session_configuration(session.id, {"model": "other-model"}, session.user_id)
    if change == "background":
        receipt = await manager.start_message(session.id, "Background input", source="bg", steerable=True)
        assert receipt.disposition == "accepted"
    else:
        reply = await submit_client_message(manager, session.id, "Ordinary next input", files, "next", wiki_mode=wiki)
        assert reply["disposition"] == "accepted"
        later = await submit_client_message(manager, session.id, "Later correction", [], "later", wiki_mode=None)
        assert later["disposition"] == "accepted"
    assert not (tmp_path / "native-steers.jsonl").exists()
    assert len(starts) == 1
    await manager.interrupt(session.id)
    await wait_until(lambda: _is_inactive(manager, session.id))
    assert manager.dispatch_snapshot(session.id)["items"]


async def _is_inactive(manager, session_id):
    return manager.dispatch_snapshot(session_id)["active_request_id"] is None


@pytest.mark.parametrize("mode_name", ["steer_lost", "steer_internal_error", "steer_user_message"])
async def test_lost_ack_only_accepts_correlated_native_evidence(native_runtime, tmp_path, mode_name):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = mode_name
    await active_root(native_runtime, tmp_path)
    reply = await submit_client_message(manager, session.id, "Set model to something else", [], "steer", wiki_mode=None)
    assert reply["disposition"] == "steered", "Free text is not a configuration override"

    async def observed():
        saved = await submission_status(manager, session.id, "steer")
        return saved["execution"]["state"] == "unknown" if mode_name != "steer_user_message" else any(
            item["state"] == "accepted" for item in saved["execution"]["native_inputs"])

    await wait_until(observed)
    (tmp_path / "native-finish").touch()
    await wait_dispatch_idle(manager, session.id)
    saved = await submission_status(manager, session.id, "steer")
    assert saved["outcome"]["status"] == "success"
    assert saved["execution"]["state"] == ("done" if mode_name == "steer_user_message" else "unknown")
    assert saved["input"]["content"] == "Set model to something else"
    assert not saved["execution"]["recoverable_input"]
    if mode_name == "steer_user_message":
        assert saved["execution"]["native_inputs"][0]["acceptance_source"] == "user_message"
    await submit_client_message(manager, session.id, saved["input"]["content"], [], "steer", wiki_mode=None)
    assert len(starts) == 1
    assert len((tmp_path / "native-steers.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("proof", ["accepted", "rejected"])
async def test_stop_late_ack_updates_original_without_replaying(native_runtime, tmp_path, proof):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "steer_late_" + proof
    root = await active_root(native_runtime, tmp_path)
    reply = await submit_client_message(manager, session.id, "Late acknowledgment", [], "late", wiki_mode=None)

    async def written():
        return (tmp_path / "native-steers.jsonl").exists()

    await wait_until(written)
    await manager.interrupt(session.id)
    await wait_until(lambda: _is_inactive(manager, session.id))
    saved = await submission_status(manager, session.id, "late")
    assert saved["request_id"] == reply["request_id"]
    evidence = saved["execution"]["native_inputs"][0]
    assert evidence["state"] == proof and evidence["target_turn_id"] == "turn-1"
    assert saved.get("outcome", {}).get("status", "interrupted") == "interrupted"
    assert (await database.get_committed_terminal(session.id, root["request_id"]))["status"] == "interrupted"
    assert not manager.dispatch_snapshot(session.id)["items"] and manager.dispatch_snapshot(session.id)["paused"]
    assert len(starts) == 1


@pytest.mark.parametrize("boundary", ["process_record", "initial_record", "native_record"])
async def test_stop_before_write_returns_same_input_to_paused_queue(native_runtime, tmp_path, monkeypatch, boundary):
    manager, database, session, mode, starts, events = native_runtime
    await active_root(native_runtime, tmp_path)
    writing, release = asyncio.Event(), asyncio.Event()
    record = database.record_native_input

    async def gate_record(session_id, request_id, evidence, **kwargs):
        if boundary == "initial_record" and evidence.kind == "steer" and evidence.state == "not_sent":
            await record(session_id, request_id, evidence, **kwargs)
            writing.set()
            await release.wait()
            return
        if boundary == "native_record" and evidence.kind == "steer" and evidence.state == "unknown":
            writing.set()
            await release.wait()
        await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", gate_record)
    note = database.note_submission_process

    async def gate_process(*args):
        if boundary == "process_record":
            writing.set()
            await release.wait()
        await note(*args)

    monkeypatch.setattr(database, "note_submission_process", gate_process)
    reply = await submit_client_message(manager, session.id, "Unwritten correction", [], "unsent", wiki_mode=None)
    await asyncio.wait_for(writing.wait(), 3)
    try:
        await manager.interrupt(session.id)
        release.set()
        await wait_until(lambda: _is_inactive(manager, session.id))
        snapshot = manager.dispatch_snapshot(session.id)
        assert snapshot["paused"]
        assert [item["request_id"] for item in snapshot["items"]] == [reply["request_id"]]
        assert not (tmp_path / "native-steers.jsonl").exists() and len(starts) == 1
        mode[0] = "normal"
        await manager.resume_dispatch(session.id)
        await wait_dispatch_idle(manager, session.id)
        assert (await submission_status(manager, session.id, "unsent"))["outcome"]["status"] == "success"
        assert not (await database.list_recoverable_submissions(session.id))[0]
        assert len(starts) == 2
    finally:
        release.set()


async def test_writer_runs_while_subscriber_is_blocked_and_stdout_is_quiet(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    blocked, release = asyncio.Event(), asyncio.Event()

    async def slow_subscriber(event):
        if event.get("type") == "assistant_text":
            blocked.set()
            await release.wait()

    manager.on_broadcast("slow-steer-client", slow_subscriber)
    mode[0] = "steer_wait"
    try:
        await submit_client_message(manager, session.id, "Wait quietly", [], "root", wiki_mode=None)
        await asyncio.wait_for(blocked.wait(), 3)
        reply = await submit_client_message(manager, session.id, "Control without output", [], "control", wiki_mode=None)
        assert reply["disposition"] == "steered"

        async def accepted():
            saved = await submission_status(manager, session.id, "control")
            return saved["execution"]["delivery_state"] == "confirmed"

        await wait_until(accepted)
        assert not release.is_set()
        assert len((tmp_path / "native-steers.jsonl").read_text().splitlines()) == 1
    finally:
        release.set()


async def test_new_human_root_and_steer_leave_old_backlog_paused(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    await active_root(native_runtime, tmp_path)
    old = await manager.start_message(session.id, "Old human input")
    await manager.interrupt(session.id)
    await wait_until(lambda: _is_inactive(manager, session.id))
    background = await manager.start_message(session.id, "Independent return", source="bg")
    await submit_client_message(manager, session.id, "New human root", [], "fresh", wiki_mode=None)

    async def active():
        saved = await submission_status(manager, session.id, "fresh")
        return saved["execution"]["delivery_state"] == "confirmed"

    await wait_until(active)
    reply = await submit_client_message(manager, session.id, "Correct the fresh root", [], "fresh-steer", wiki_mode=None)
    assert reply["disposition"] == "steered"

    async def accepted():
        return (await submission_status(manager, session.id, "fresh-steer"))["execution"]["delivery_state"] == "confirmed"

    await wait_until(accepted)
    (tmp_path / "native-finish").touch()
    await wait_until(lambda: _is_inactive(manager, session.id))
    assert len(starts) == 2
    assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [old.request_id, background.request_id]
    assert manager.dispatch_snapshot(session.id)["paused"]
    mode[0] = "normal"
    await manager.resume_dispatch(session.id)
    await wait_dispatch_idle(manager, session.id)
    assert len(starts) == 4


async def test_wait_return_does_not_close_root_and_connections_have_distinct_identity(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    generations = []
    await active_root(native_runtime, tmp_path)
    for cycle in range(2):
        for number in range(2):
            client = f"correction-{cycle}-{number}"
            await submit_client_message(manager, session.id, client, [], client, wiki_mode=None)

            async def accepted(client_id=client):
                saved = await submission_status(manager, session.id, client_id)
                return saved["execution"]["delivery_state"] == "confirmed"

            await wait_until(accepted)
            saved = await submission_status(manager, session.id, client)
            evidence = saved["execution"]["native_inputs"][0]
            assert evidence["target_turn_id"] == f"turn-{cycle + 1}"
            if number == 0:
                generations.append(evidence["connection_generation"])
        await manager.interrupt(session.id)
        await wait_until(lambda: _is_inactive(manager, session.id))
        # A new browser submission is necessary to start the next root.
        if cycle == 0:
            mode[0] = "steer_wait"
            await submit_client_message(manager, session.id, "Next root", [], "next-root", wiki_mode=None)

            async def next_active():
                return (await submission_status(manager, session.id, "next-root"))["execution"]["delivery_state"] == "confirmed"

            await wait_until(next_active)
    assert len(set(generations)) == 2 and len(starts) == 2


async def test_late_ack_survives_reader_cancellation_before_database_transaction(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "steer_late_accepted"
    await active_root(native_runtime, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    record = database.record_native_input

    async def gate_proof(session_id, request_id, evidence, **kwargs):
        if evidence.kind == "steer" and evidence.state == "accepted":
            entered.set()
            await release.wait()
        await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", gate_proof)
    await submit_client_message(manager, session.id, "Retain observed ACK", [], "late-proof", wiki_mode=None)

    async def written():
        return (tmp_path / "native-steers.jsonl").exists()

    await wait_until(written)
    backend = session._run.backend
    reader = backend._stdout_task
    try:
        await manager.interrupt(session.id)
        await asyncio.wait_for(entered.wait(), 3)

        async def cancelled():
            return reader.cancelling() > 0

        await wait_until(cancelled)
    finally:
        release.set()
    await wait_until(lambda: _is_inactive(manager, session.id))
    saved = await submission_status(manager, session.id, "late-proof")
    assert saved["execution"]["native_inputs"][0]["state"] == "accepted"
    assert saved["outcome"]["status"] == "interrupted" and len(starts) == 1


async def test_stop_before_writer_coroutine_enters_preserves_original_paused_input(native_runtime, tmp_path, monkeypatch):
    from server.sessions.owner_runtime import OwnerRuntime
    manager, _, session, mode, _, _ = native_runtime
    await active_root(native_runtime, tmp_path)
    queued, release = asyncio.Event(), asyncio.Event()
    task = OwnerRuntime.task

    def delay_writer(runtime, operation, name):
        if name.startswith("steer-write-"):
            async def scheduled():
                queued.set()
                try:
                    await release.wait()
                except BaseException:
                    operation.close()  # The actual writer has never entered.
                    raise
                return await operation
            return task(runtime, scheduled(), name)
        return task(runtime, operation, name)

    monkeypatch.setattr(OwnerRuntime, "task", delay_writer)
    try:
        receipt = await submit_client_message(manager, session.id, "Unstarted writer", [], "unstarted", wiki_mode=None)
        await asyncio.wait_for(queued.wait(), 3)
        await manager.interrupt(session.id)
        await wait_until(lambda: _is_inactive(manager, session.id))
        snapshot = manager.dispatch_snapshot(session.id)
        assert snapshot["paused"]
        assert [row["request_id"] for row in snapshot["items"]] == [receipt["request_id"]]
        assert not (tmp_path / "native-steers.jsonl").exists()
        mode[0] = "normal"
        release.set()
        await manager.resume_dispatch(session.id)
        await wait_dispatch_idle(manager, session.id)
        assert (await submission_status(manager, session.id, "unstarted"))["request_id"] == receipt["request_id"]
    finally:
        release.set()


@pytest.mark.parametrize("was_enabled", [False, True])
async def test_changed_connector_selection_queues_new_human_input(native_runtime, tmp_path, was_enabled):
    manager, database, session, mode, starts, events = native_runtime
    from server.crypto import encrypt

    await database.save_connector_installation(installation_id="steering-github", kind="github", label="Fixture account",
        auth_type="oauth", secret_encrypted=encrypt("offline-only-token", "auth"), created_at="2026-11-01T00:00:00Z",
        external_account_id="91001", user_id=session.user_id)
    await database.replace_agent_connectors(session.agent_id, ["steering-github"] if was_enabled else [], session.user_id)
    await active_root(native_runtime, tmp_path)
    await database.replace_agent_connectors(session.agent_id, [] if was_enabled else ["steering-github"], session.user_id)
    reply = await submit_client_message(manager, session.id, "Use the current connectors", [], "connector-change", wiki_mode=None)
    assert reply["disposition"] == "accepted"
    assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [reply["request_id"]]
    assert not (tmp_path / "native-steers.jsonl").exists() and len(starts) == 1


@pytest.mark.parametrize("late", [False, True])
async def test_ack_updates_saved_input_notices_while_root_remains_active(native_runtime, tmp_path, monkeypatch, late):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "steer_gate"
    await active_root(native_runtime, tmp_path)
    # Only the steer ACK is late; native initialization has already finished.
    monkeypatch.setattr("server.harness.codex_rpc.RPC_TIMEOUT", .05 if late else 5)
    anchor = [("a" * 40, False)]

    async def capture_anchor(_working_dir):
        return anchor[0]

    monkeypatch.setattr("server.sessions.owner_runtime.fork_helpers.capture_git_anchor", capture_anchor)
    await submit_client_message(manager, session.id, "Confirm acceptance live", [], "visible-ack", wiki_mode=None)

    async def before_ack():
        saved = await submission_status(manager, session.id, "visible-ack")
        return (tmp_path / "native-steers.jsonl").exists() and (
            saved["execution"]["state"] == "unknown" if late else bool(saved["execution"]["native_inputs"]))

    await wait_until(before_ack)
    old_page, _ = await database.list_recoverable_submissions(session.id)
    assert old_page[0]["native_inputs"][0]["state"] == "unknown"
    revision = manager.dispatch_snapshot(session.id)["revision"]
    anchor[0] = ("b" * 40, True)
    (tmp_path / "native-release-ack").touch()

    async def refreshed():
        return any(event.get("type") == "dispatch_state" and event["dispatch_state"]["revision"] > revision
                   for event in events)

    await wait_until(refreshed)
    new_page, _ = await database.list_recoverable_submissions(session.id)
    if late:
        # The earlier RPC timeout still leaves execution unknown until the
        # root ends. Acceptance alone must not hide that separate uncertainty.
        assert len(new_page) == 1
        assert new_page[0]["execution_state"] == "unknown"
    else:
        assert not new_page
    saved = await submission_status(manager, session.id, "visible-ack")
    assert saved["execution"]["native_inputs"][0]["state"] == "accepted"
    assert manager.dispatch_snapshot(session.id)["active_request_id"] is not None
    assert len(starts) == 1 and len((tmp_path / "native-steers.jsonl").read_text().splitlines()) == 1
    (tmp_path / "native-finish").touch()
    await wait_dispatch_idle(manager, session.id)
    assert not (await database.list_recoverable_submissions(session.id))[0]
    stored = next(row for row in await database.load_messages(session.id) if row["content"] == "Confirm acceptance live")
    assert (stored["git_head"], stored["git_status_clean"]) == ("a" * 40, False)
