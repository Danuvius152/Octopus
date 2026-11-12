"""Application creation keeps the real input owner and resource lifetimes."""

import asyncio
from pathlib import Path

import pytest

from server.agent_manager import AgentManager
from server.applications import ApplicationManager
from server.workspace import paths_for
from tests.native_app_server import ROOT, inject, item, terminal, until, wire_frames
from tests.native_app_server import native_runtime as native_runtime


@pytest.fixture
async def creation(native_runtime):
    manager, database, parent, _, _, _ = native_runtime
    builder = await AgentManager(database).create_agent(
        name="Creation builder", backend="codex", user_id=parent.user_id,
    )
    apps = ApplicationManager()
    apps.bind(manager, database)
    native_runtime.starts.clear()  # Exclude the fixture's model-discovery argv.
    try:
        yield native_runtime, apps, builder
    finally:
        apps.shutdown()


@pytest.fixture
async def ready_application(creation):
    runtime, apps, builder = creation
    ready = asyncio.Event()

    async def observe(event):
        if event.get("type") == "application_created":
            Path(event["application"]["app_dir"], "index.html").write_text("<p>Ready</p>")
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            ready.set()

    runtime.manager.on_broadcast("initial-ready-application", observe)
    try:
        row = await apps.create_application(name="Ready application", description="Build a static page", agent_id=builder["id"])
        await asyncio.wait_for(ready.wait(), 4)
        await runtime.manager.await_session_execution(row["session_id"], timeout=4)
        yield runtime, apps, builder, row
    finally:
        runtime.manager.remove_broadcast("initial-ready-application")


@pytest.mark.parametrize("phase", ["before_commit", "accepted_before_ack"])
async def test_cancelled_build_waiter_preserves_real_admission(ready_application, monkeypatch, phase):
    runtime, apps, _, row = ready_application
    manager, database = runtime.manager, runtime.database
    await manager.interrupt(row["session_id"])
    await database.update_application(row["id"], status="ready", error="retained diagnostic")
    entered, release = asyncio.Event(), asyncio.Event()
    receipts = []
    get_row, admit, start = database.get_application, database.admit_client_submission, manager.start_message

    async def held_commit(*args, **kwargs):
        entered.set()
        await release.wait()
        return await admit(*args, **kwargs)

    async def held_ack(*args, **kwargs):
        receipt = await start(*args, **kwargs)
        receipts.append(receipt)
        entered.set()
        await release.wait()
        return receipt

    if phase == "before_commit":
        monkeypatch.setattr(database, "admit_client_submission", held_commit)
    monkeypatch.setattr(manager, "start_message", held_ack)
    task = asyncio.create_task(apps.request_build(row["id"], "A cancellable build waiter"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        before = await get_row(row["id"])
        if phase == "before_commit":
            assert (before["status"], before["error"]) == ("ready", "retained diagnostic")
            assert not manager.dispatch_snapshot(row["session_id"])["items"]
        else:
            assert (before["status"], before["error"]) == ("building", None)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        current = await get_row(row["id"])
        pending = manager.dispatch_snapshot(row["session_id"])["items"]
        assert (current["status"], current["error"]) == ("building", None)
        assert len(pending) == len(receipts) == 1
        saved = await database.get_submission_for_request(row["session_id"], receipts[0].request_id)
        assert saved["receipt"]["accepted"] and saved["execution_state"] == "pending"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("first_end", ["cancel", "refuse"])
async def test_rejected_build_cannot_undo_a_concurrent_real_admission(ready_application, monkeypatch, first_end):
    from server.applications import ApplicationError
    from server.sessions.runtime_types import SessionAdmissionClosed

    runtime, apps, _, row = ready_application
    manager, database = runtime.manager, runtime.database
    await manager.interrupt(row["session_id"])
    await database.update_application(row["id"], status="ready", error="previous error")
    entered, release = asyncio.Event(), asyncio.Event()
    get_row, start = database.get_application, manager.start_message
    receipts = []
    first = True

    async def held_lookup(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        return await get_row(*args, **kwargs)

    async def admission(session_id, body, **kwargs):
        if "Rejected change" in body:
            entered.set()
            await release.wait()
            raise SessionAdmissionClosed("forking admission refused")
        receipt = await start(session_id, body, **kwargs)
        receipts.append(receipt)
        return receipt

    if first_end == "cancel":
        monkeypatch.setattr(database, "get_application", held_lookup)
    monkeypatch.setattr(manager, "start_message", admission)
    rejected = asyncio.create_task(apps.request_build(row["id"], "Rejected change"))
    accepted = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        accepted = asyncio.create_task(apps.request_build(row["id"], "Accepted change"))
        if first_end == "cancel":
            await asyncio.wait_for(accepted, 3)
            rejected.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if first_end == "cancel" else ApplicationError):
            await rejected
        await asyncio.wait_for(accepted, 3)
        current = await get_row(row["id"])
        assert (current["status"], current["error"]) == ("building", None)
        pending = manager.dispatch_snapshot(row["session_id"])["items"]
        assert len(pending) == len(receipts) == 1
        saved = await database.get_submission_for_request(row["session_id"], receipts[0].request_id)
        assert saved["receipt"]["accepted"] and saved["execution_state"] == "pending"
        assert "Accepted change" in saved["payload"]["operation"]["content"]
    finally:
        release.set()
        await asyncio.gather(*(task for task in (rejected, accepted) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("operation", ["duplicate", "compact", "other-builder"])
async def test_nonbuild_admission_does_not_change_the_application_projection(ready_application, operation):
    from server.sessions.client_submissions import submit_client_message
    from tests.native_app_server import wait_until

    runtime, apps, builder, row = ready_application
    manager, database, _, _, _, _ = runtime
    if operation == "duplicate":
        await submit_client_message(manager, row["session_id"], "One new build", [], "same-build", wiki_mode=None)

        async def completed():
            saved = await database.get_client_submission(row["session_id"], "same-build")
            return saved["terminal"] is not None and (await apps.get_application(row["id"]))["status"] == "ready"

        await wait_until(completed)
        before = await apps.get_application(row["id"])
        await submit_client_message(manager, row["session_id"], "One new build", [], "same-build", wiki_mode=None)
    elif operation == "compact":
        before = await apps.get_application(row["id"])
        receipt = await manager.start_compact(row["session_id"], client_request_id="app-compact")
        assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
    else:
        before = await apps.get_application(row["id"])
        conversation = await manager.create_session(builder["id"], "Another app conversation", row["app_dir"],
                                                    origin="application", app_id=row["id"], backend="codex")
        receipt = await manager.start_message(conversation.id, "Another app conversation", source="human")
        assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
    assert await apps.get_application(row["id"]) == before


async def test_failed_atomic_build_projection_rolls_back_the_original_input(ready_application):
    import sqlite3

    runtime, apps, _, row = ready_application
    database = runtime.database
    before = await apps.get_application(row["id"])
    async with database.transaction(write=True) as tx:
        await tx.execute("CREATE TEMP TRIGGER refuse_app_build BEFORE UPDATE OF status ON applications "
                         "WHEN NEW.status='building' BEGIN SELECT RAISE(ABORT,'build projection refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match="build projection refused"):
        await database.admit_client_submission(row["session_id"], "rejected-build", "digest", "rejected-request",
            payload={"operation": {"kind": "message", "content": "Rejected build", "attachment_ids": []}},
            receipt={"accepted": True, "disposition": "accepted"})
    assert await database.get_client_submission(row["session_id"], "rejected-build") is None
    assert await apps.get_application(row["id"]) == before


async def test_waiting_backend_hook_does_not_delay_or_overwrite_new_building(ready_application, monkeypatch):
    from server.app_backends import backend_supervisor

    runtime, apps, _, row = ready_application
    manager = runtime.manager
    hook_entered, hook_release, hook_finished, ready, building = (asyncio.Event() for _ in range(5))
    on_rebuild = backend_supervisor.on_rebuild

    async def hold_hook(*args, **kwargs):
        hook_entered.set()
        await hook_release.wait()
        try:
            await on_rebuild(*args, **kwargs)
        finally:
            hook_finished.set()

    async def observe(event):
        if event.get("type") == "application_updated":
            (ready if event["application"]["status"] == "ready" else building).set()

    monkeypatch.setattr(backend_supervisor, "on_rebuild", hold_hook)
    manager.on_broadcast("hook-independent-app-projection", observe)
    try:
        await apps.request_build(row["id"], "Complete another build")
        await asyncio.wait_for(hook_entered.wait(), 4)
        await asyncio.wait_for(ready.wait(), 2)
        assert not hook_finished.is_set()
        await manager.await_session_execution(row["session_id"], timeout=4)
        await manager.interrupt(row["session_id"])
        building.clear()
        assert (await manager.start_message(row["session_id"], "A later build", source="bg")).accepted
        await asyncio.wait_for(building.wait(), 2)
        current = await apps.get_application(row["id"])
        assert current["status"] == "building"
        hook_release.set()
        await asyncio.wait_for(hook_finished.wait(), 2)
        assert await apps.get_application(row["id"]) == current
    finally:
        hook_release.set()
        manager.remove_broadcast("hook-independent-app-projection")


async def test_waiting_application_notice_cannot_resurrect_a_deleted_application(ready_application):
    runtime, apps, _, row = ready_application
    entered, release, deleted_after_ready = (asyncio.Event() for _ in range(3))
    notices = []

    async def observe(event):
        kind = event.get("type")
        if kind == "application_updated" and event["application"]["status"] == "ready":
            entered.set()
            await release.wait()
            notices.append("ready")
        elif kind == "application_deleted":
            if notices and notices[-1] == "ready":
                deleted_after_ready.set()
            notices.append("deleted")

    runtime.manager.on_broadcast("delete-after-held-app-notice", observe)
    patch = asyncio.create_task(apps.update_application(row["id"], entrypoint="index.html"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await apps.delete_application(row["id"])
        assert await runtime.database.get_application(row["id"]) is None
        release.set()
        await asyncio.wait_for(patch, 2)
        await asyncio.wait_for(deleted_after_ready.wait(), 2)
        assert notices[-1] == "deleted"
    finally:
        release.set()
        await asyncio.gather(patch, return_exceptions=True)
        runtime.manager.remove_broadcast("delete-after-held-app-notice")


@pytest.mark.parametrize("later", ["queued", "replacement-builder", "deleted"])
async def test_old_committed_completion_cannot_change_new_work_or_deleted_application(ready_application, monkeypatch, later):
    from server.app_backends import backend_supervisor
    from server.models import TurnOutcome
    from server.sessions.submission import RequestCompletion
    from tests.native_app_server import wait_until

    runtime, apps, _, row = ready_application
    manager, database, _, _, _, _ = runtime
    messages = await database.load_messages(row["session_id"])
    outcome = TurnOutcome.model_validate(next(message["content"] for message in messages if message["type"] == "result"))
    original = RequestCompletion(outcome.request_id, outcome.request_id, "completed", outcome=outcome, recording_state="committed")
    hooks = []

    async def rebuilt(*args, **kwargs):
        hooks.append(args)

    monkeypatch.setattr(backend_supervisor, "on_rebuild", rebuilt)
    if later == "queued":
        await manager.interrupt(row["session_id"])
        assert (await manager.start_message(row["session_id"], "Later queue", source="bg")).accepted
    elif later == "replacement-builder":
        await manager.archive_session(row["session_id"], replace=False)
        await apps.request_build(row["id"], "Use a fresh builder")

        async def completed():
            return (await apps.get_application(row["id"]))["status"] == "ready"

        await wait_until(completed)
        assert (await apps.get_application(row["id"]))["session_id"] != row["session_id"]
    else:
        await apps.delete_application(row["id"])
    before, count = await database.get_application(row["id"]), len(hooks)
    await apps._on_completion(row["session_id"], original)
    assert await database.get_application(row["id"]) == before
    assert len(hooks) == count


async def test_closed_initial_input_retires_only_its_empty_builder_session(creation):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    manager.close_admission()
    row = await apps.create_application(name="Refused initial build", description="Must not start", agent_id=builder["id"])
    assert row["status"] == "failed"
    assert row["session_id"] is None
    assert await database.application_session_ids(row["id"]) == []
    assert not any(session.app_id == row["id"] for session in manager.sessions.values())
    assert Path(row["app_dir"]).is_dir()
    assert starts == []


async def test_a_creation_subscriber_can_archive_the_builder_before_input(creation):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    observed = []

    async def archive(event):
        if event.get("type") == "application_created":
            await manager.archive_agent(builder["id"])
            observed.append("archive returned")

    async def collect(event):
        if event.get("type") in {"application_created", "application_updated"}:
            observed.append((event["type"], event["application"]["status"]))

    manager.on_broadcast("archive-from-creation", archive)
    manager.on_broadcast("receive-creation-after-archive", collect)
    row = await asyncio.wait_for(apps.create_application(
        name="Archive from created", description="Do not wait on this subscriber", agent_id=builder["id"],
    ), 3)
    assert row["status"] == "failed" and "admission" in row["error"].lower()
    assert observed == ["archive returned", ("application_created", "building"), ("application_updated", "failed")]
    assert (await database.get_agent(builder["id"]))["archived"]
    assert starts == []


async def test_cancelled_creation_notification_does_not_abandon_the_original_build(creation, tmp_path):
    runtime, apps, builder = creation
    manager, _, _, _, starts, _ = runtime
    projected = asyncio.Event()

    async def cancel_notice(event):
        if event.get("type") == "application_created":
            (Path(event["application"]["app_dir"]) / "index.html").write_text("<p>Original build output</p>")
            raise asyncio.CancelledError("Creation subscriber cancelled its notification")
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            projected.set()

    manager.on_broadcast("cancel-creation-notification", cancel_notice)
    row = await asyncio.wait_for(apps.create_application(
        name="Cancelled notice", description="The original operation still owns input handoff", agent_id=builder["id"],
    ), 5)
    await asyncio.wait_for(projected.wait(), 5)
    current = await apps.get_application(row["id"])
    assert current["status"] == "ready" and current["error"] is None
    assert current["session_id"] == row["session_id"]
    assert len(starts) == 1
    assert sum(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path)) == 1


@pytest.mark.parametrize("boundary", ["row", "session", "input"])
async def test_archive_drains_registered_preparation_and_refuses_later_input(creation, monkeypatch, boundary):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    target, method = (database, "save_application") if boundary == "row" else (
        manager, "create_session" if boundary == "session" else "start_message")
    original = getattr(target, method)

    async def held(*args, **kwargs):
        if boundary == "input":
            entered.set()
            await release.wait()
        result = await original(*args, **kwargs)
        if boundary != "input":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(target, method, held)
    creating = asyncio.create_task(apps.create_application(
        name="Archive during creation", description="Drain the original preparation", agent_id=builder["id"],
    ))
    archive = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        archive = asyncio.create_task(manager.archive_agent(builder["id"]))
        await until(lambda: builder["id"] in manager._closed_agents)
        assert not archive.done()
        release.set()
        row = await asyncio.wait_for(creating, 5)
        await asyncio.wait_for(archive, 5)
        assert row["status"] == "failed" and "unconfirmed" not in row["error"]
        assert (await database.get_application(row["id"]))["status"] == "failed"
        assert not any(session.agent_id == builder["id"] for session in manager.sessions.values())
        assert starts == []
    finally:
        release.set()
        await asyncio.gather(*(task for task in (creating, archive) if task is not None), return_exceptions=True)


async def test_refused_initial_input_does_not_erase_another_accepted_build(creation):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    projected = asyncio.Event()
    foreign = []

    async def accept_another_input(event):
        if event.get("type") == "application_created":
            row = event["application"]
            (Path(row["app_dir"]) / "index.html").write_text("<p>Accepted other build</p>")
            receipt = await manager.start_message(row["session_id"], "An independently accepted build", source="human")
            foreign.append(receipt)
            await asyncio.wait_for(receipt.wait(), 5)
            await asyncio.wait_for(projected.wait(), 5)
            manager.close_admission()
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            projected.set()

    manager.on_broadcast("other-original-input", accept_another_input)
    row = await asyncio.wait_for(apps.create_application(
        name="Other accepted builder input", description="Preserve actual work", agent_id=builder["id"],
    ), 8)
    assert row["status"] == "ready" and row["error"] is None
    assert manager.get_session(row["session_id"]) is not None
    assert len(starts) == 1 and len(foreign) == 1
    assert (await database.get_submission_for_request(row["session_id"], foreign[0].request_id))["receipt"]["accepted"]


async def test_refused_creation_cannot_take_projection_from_an_already_committed_other_input(creation):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    observer_entered, observer_release, observer_finished = (asyncio.Event() for _ in range(3))
    foreign = []

    async def delayed_completion(sid, completion):
        observer_entered.set()
        await observer_release.wait()
        try:
            await apps._on_completion(sid, completion)
        finally:
            observer_finished.set()

    async def accept_other_input(event):
        if event.get("type") == "application_created":
            row = event["application"]
            Path(row["app_dir"], "index.html").write_text("<p>Other committed build</p>")
            receipt = await manager.start_message(row["session_id"], "Other accepted build", source="human")
            completion = await asyncio.wait_for(receipt.wait(), 4)
            foreign.append(completion)
            assert completion.recording_state == "committed"
            await asyncio.wait_for(observer_entered.wait(), 2)
            manager.close_admission()  # Refuse the still-unadmitted initial build.

    manager.on_request_complete(apps.BROADCAST_KEY, delayed_completion)
    manager.on_broadcast("other-committed-app-input", accept_other_input)
    try:
        row = await apps.create_application(name="Committed before creation refusal", description="Keep original projection ownership",
                                            agent_id=builder["id"])
        assert foreign[0].outcome.model_dump(mode="json") == await database.get_committed_terminal(row["session_id"], foreign[0].request_id)
        assert row["status"] == "building" and row["error"] is None and row["last_built_at"] is None
        observer_release.set()
        await asyncio.wait_for(observer_finished.wait(), 3)
        current = await apps.get_application(row["id"])
        assert current["status"] == "ready" and current["error"] is None and current["last_built_at"]
        assert len(starts) == 1
    finally:
        observer_release.set()
        await asyncio.wait_for(observer_finished.wait(), 3)
        manager.remove_broadcast("other-committed-app-input")


async def test_late_preparation_cleanup_error_cannot_pollute_a_new_admitted_build(creation, monkeypatch):
    from server.sessions.submission import SubmissionReceipt

    runtime, apps, builder = creation
    manager, database, _, mode, _, _ = runtime
    mode[0] = "control_output"
    start, read = manager.start_message, database.get_application
    checked, proceed = asyncio.Event(), asyncio.Event()
    cleanup_started, held_rows = False, []

    async def refuse_initial(sid, prompt, **kwargs):
        if kwargs.get("source") == "application":
            return SubmissionReceipt.closed(kwargs["request_id"], "withdrawn")
        return await start(sid, prompt, **kwargs)

    async def failed_cleanup(*args, **kwargs):
        nonlocal cleanup_started
        cleanup_started = True
        raise OSError("Controlled empty-session cleanup failure")

    async def hold_original_snapshot(*args, **kwargs):
        row = await read(*args, **kwargs)
        if cleanup_started and row is not None and row["status"] == "failed" and not checked.is_set():
            held_rows.append(row)
            checked.set()
            await proceed.wait()
        return row

    monkeypatch.setattr(manager, "start_message", refuse_initial)
    monkeypatch.setattr(manager, "delete_session", failed_cleanup)
    monkeypatch.setattr(database, "get_application", hold_original_snapshot)
    creating = asyncio.create_task(apps.create_application(name="Cleanup racing new input", description="Preserve new build facts",
                                                           agent_id=builder["id"]))
    session_id = None
    try:
        await asyncio.wait_for(checked.wait(), 3)
        session_id = held_rows[0]["session_id"]
        assert (await manager.start_message(session_id, "Independent accepted build", source="human")).accepted
        proceed.set()
        row = await asyncio.wait_for(creating, 3)
        assert row["status"] == "building" and row["error"] is None
    finally:
        proceed.set()
        await asyncio.gather(creating, return_exceptions=True)
        if session_id is not None:
            await manager.interrupt(session_id)


@pytest.mark.parametrize("cleanup_error", [False, True], ids=["not-empty", "cleanup-error"])
async def test_late_empty_cleanup_cannot_pollute_a_newer_completed_build(creation, monkeypatch, tmp_path, cleanup_error):
    from server.sessions.submission import SubmissionReceipt

    runtime, apps, builder = creation
    manager, _, _, _, starts, _ = runtime
    entered, release, projected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    start, delete = manager.start_message, manager.delete_session
    refused = False

    async def refuse_first_input(session_id, prompt, **kwargs):
        nonlocal refused
        if not refused:
            refused = True
            return SubmissionReceipt.closed(kwargs["request_id"], "withdrawn")
        return await start(session_id, prompt, **kwargs)

    async def hold_cleanup(session_id, **kwargs):
        if kwargs.get("only_empty_application"):
            entered.set()
            await release.wait()
            if cleanup_error:
                raise OSError("Old empty-session cleanup failed")
        return await delete(session_id, **kwargs)

    async def observe(event):
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            projected.set()

    monkeypatch.setattr(manager, "start_message", refuse_first_input)
    monkeypatch.setattr(manager, "delete_session", hold_cleanup)
    manager.on_broadcast("observe-build-during-cleanup", observe)
    creating = asyncio.create_task(apps.create_application(
        name="Build during old cleanup", description="Keep the later completion authoritative", agent_id=builder["id"],
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        row, = await apps.list_applications(user_id=builder["user_id"])
        assert row["status"] == "failed"
        (Path(row["app_dir"]) / "index.html").write_text("<p>New accepted build</p>")
        await apps.request_build(row["id"], "An explicit new build")
        await asyncio.wait_for(projected.wait(), 5)
        release.set()
        current = await asyncio.wait_for(creating, 5)
        assert current["status"] == "ready" and current["error"] is None
        assert current["session_id"] == row["session_id"]
        assert manager.get_session(current["session_id"]) is not None
        assert len(starts) == 1
        assert sum(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path)) == 1
    finally:
        release.set()
        await asyncio.gather(creating, return_exceptions=True)


async def test_empty_cleanup_error_stays_with_its_original_failed_creation(creation, monkeypatch):
    runtime, apps, builder = creation
    manager, _, _, _, starts, _ = runtime
    manager.close_admission()

    async def fail_cleanup(*args, **kwargs):
        raise OSError("Original empty-session cleanup failed")

    monkeypatch.setattr(manager, "delete_session", fail_cleanup)
    row = await apps.create_application(
        name="Failed empty cleanup", description="Retain the failed relationship", agent_id=builder["id"],
    )
    assert row["status"] == "failed"
    assert row["session_id"] in row["error"]
    assert "Original empty-session cleanup failed" in row["error"]
    assert manager.get_session(row["session_id"]) is not None
    assert starts == []


async def test_unconfirmed_admission_keeps_its_identity_until_the_original_owner_finishes(creation, monkeypatch, tmp_path):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    admit, start = database.admit_client_submission, manager.start_message
    submissions, identifiers = [], []
    projected = asyncio.Event()

    async def hold_admission(*args, **kwargs):
        entered.set()
        await release.wait()
        return await admit(*args, **kwargs)

    async def lose_response(session_id, prompt, **kwargs):
        identifiers.append(kwargs["request_id"])
        (Path(manager.get_session(session_id).working_dir) / "index.html").write_text("<p>Original output</p>")
        submissions.append(asyncio.create_task(start(session_id, prompt, **kwargs)))
        await entered.wait()
        raise RuntimeError("response lost while original Submit is still owned")

    async def observe(event):
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            projected.set()

    monkeypatch.setattr(database, "admit_client_submission", hold_admission)
    monkeypatch.setattr(manager, "start_message", lose_response)
    manager.on_broadcast("observe-delayed-admission", observe)
    try:
        row = await asyncio.wait_for(apps.create_application(
            name="Unconfirmed original input", description="Do not replay", agent_id=builder["id"],
        ), 3)
        assert row["status"] == "failed" and "unconfirmed" in row["error"]
        assert identifiers[0] in row["error"]
        assert manager.get_session(row["session_id"]) is not None
        assert await database.get_submission_for_request(row["session_id"], identifiers[0]) is None
        assert starts == []
        release.set()
        receipt = await asyncio.wait_for(submissions[0], 3)
        await asyncio.wait_for(receipt.wait(), 5)
        await asyncio.wait_for(projected.wait(), 5)
        assert (await database.get_application(row["id"]))["status"] == "ready"
        assert receipt.request_id == identifiers[0]
        assert sum(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path)) == 1
    finally:
        release.set()
        await asyncio.gather(*submissions, return_exceptions=True)


async def test_accepted_input_with_pending_completion_keeps_its_original_build(creation, monkeypatch, tmp_path):
    runtime, apps, builder = creation
    manager, _, _, mode, starts, _ = runtime
    mode[0] = "control_output"
    start = manager.start_message
    receipts = []
    projected = asyncio.Event()

    async def lose_response(session_id, prompt, **kwargs):
        (Path(manager.get_session(session_id).working_dir) / "index.html").write_text("<p>Original accepted build</p>")
        receipts.append(await start(session_id, prompt, **kwargs))
        raise RuntimeError("Response lost while original completion is still pending")

    async def observe(event):
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            projected.set()

    monkeypatch.setattr(manager, "start_message", lose_response)
    manager.on_broadcast("observe-pending-original-completion", observe)
    row = await asyncio.wait_for(apps.create_application(
        name="Accepted pending completion", description="Keep the live original observer", agent_id=builder["id"],
    ), 5)
    assert row["status"] == "building" and row["error"] is None
    assert receipts[0].accepted and receipts[0].completion is None
    await until(lambda: any(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path)))
    inject(tmp_path, item(ROOT, "turn-1", "original-final", "Original build completed"), terminal())
    await asyncio.wait_for(receipts[0].wait(), 5)
    await asyncio.wait_for(projected.wait(), 5)
    current = await apps.get_application(row["id"])
    assert current["status"] == "ready" and current["error"] is None
    assert len(starts) == 1
    assert sum(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path)) == 1


async def test_accepted_input_without_completion_owner_retains_a_failed_diagnostic(creation, monkeypatch, tmp_path):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    admit = database.admit_client_submission
    identifiers = []

    async def end_owner_after_commit(*args, **kwargs):
        await admit(*args, **kwargs)
        identifiers.append(kwargs.get("request_id") or args[3])
        raise asyncio.CancelledError("Owner ended after durable acceptance, before receipt registration")

    monkeypatch.setattr(database, "admit_client_submission", end_owner_after_commit)
    row = await asyncio.wait_for(apps.create_application(
        name="Accepted without completion", description="Retain the original admitted input", agent_id=builder["id"],
    ), 5)
    assert row["status"] == "failed"
    assert identifiers[0] in row["error"]
    assert "accepted" in row["error"].lower() and "unavailable" in row["error"].lower()
    original = await database.get_submission_for_request(row["session_id"], identifiers[0])
    assert original["receipt"]["accepted"] and original["terminal"] is None
    assert manager.get_session(row["session_id"]) is not None
    assert Path(row["app_dir"]).is_dir()
    assert starts == [] and not any(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path))


async def test_primary_and_steer_completions_retire_only_the_original_backend(
    creation, monkeypatch, tmp_path, short_process_shutdown,
):
    from server import app_backends
    from server.sessions.client_submissions import submit_client_message
    from tests.app_proxy import write_backend
    from tests.native_app_server import wait_until

    runtime, apps, builder = creation
    manager, database, _, mode, _, _ = runtime
    supervisor = app_backends.BackendSupervisor()
    monkeypatch.setattr(app_backends, "backend_supervisor", supervisor)
    mode[0] = "steer_wait"
    carried_entered, carried_release, carried_finished, ready = (asyncio.Event() for _ in range(4))

    async def complete(sid, completion):
        carried = completion.input_request_id != completion.request_id
        if carried:
            carried_entered.set()
            await carried_release.wait()
        try:
            await apps._on_completion(sid, completion)
        finally:
            if carried:
                carried_finished.set()

    async def observe(event):
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            ready.set()

    manager.on_request_complete(apps.BROADCAST_KEY, complete)
    manager.on_broadcast("steered-build-ready", observe)
    row = await apps.create_application(name="Steered build", description="One root completion", agent_id=builder["id"])
    session = manager.get_session(row["session_id"])
    try:
        await until(lambda: session._run.backend is not None and session._run.backend.steering_open)
        Path(row["app_dir"], "index.html").write_text("<p>Built</p>")
        write_backend(Path(row["app_dir"]),
                      "import http.server,os\nhttp.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),"
                      "http.server.BaseHTTPRequestHandler).serve_forever()\n")
        original = (await supervisor.ensure_running(row["id"], row["app_dir"])).process
        correction = await submit_client_message(manager, session.id, "Adjust the build", [], "app-correction", wiki_mode=None)
        assert correction["disposition"] == "steered"

        async def steer_accepted():
            saved = await database.get_client_submission(session.id, "app-correction")
            return any(entry["state"] == "accepted" for entry in saved["native_inputs"])

        await wait_until(steer_accepted)
        (tmp_path / "native-finish").touch()
        await asyncio.wait_for(carried_entered.wait(), 4)
        await asyncio.wait_for(ready.wait(), 4)
        await until(lambda: original.returncode is not None and not supervisor.get(row["id"])._retiring)
        replacement = (await supervisor.ensure_running(row["id"], row["app_dir"])).process
        assert replacement is not original and replacement.returncode is None
        carried_release.set()
        await asyncio.wait_for(carried_finished.wait(), 2)
        assert supervisor.get(row["id"]).process is replacement and replacement.returncode is None
        assert (await apps.get_application(row["id"]))["status"] == "ready"
        saved = await database.get_client_submission(session.id, "app-correction")
        assert saved["terminal"]["request_id"] == saved["execution_owner"]
    finally:
        carried_release.set()
        await manager.interrupt(session.id)
        await supervisor.shutdown()
        manager.remove_broadcast("steered-build-ready")


async def test_a_later_steer_does_not_take_projection_from_an_independent_queued_build(creation, tmp_path):
    from server.sessions.client_submissions import submit_client_message
    from tests.native_app_server import wait_until

    runtime, apps, builder = creation
    manager, database, _, mode, _, _ = runtime
    mode[0] = "steer_wait"
    projected = asyncio.Event()
    queued_id = None

    async def complete(sid, completion):
        await apps._on_completion(sid, completion)
        if completion.input_request_id == queued_id:
            projected.set()

    manager.on_request_complete(apps.BROADCAST_KEY, complete)
    row = await apps.create_application(name="Queued then steered", description="Preserve independent build ownership", agent_id=builder["id"])
    session = manager.get_session(row["session_id"])
    try:
        await until(lambda: any(event.get("session_id") == session.id and event.get("type") == "tool_use" for event in runtime.events))
        Path(row["app_dir"], "index.html").write_text("<p>Built</p>")
        queued = await manager.start_message(session.id, "Independent later build", source="bg")
        queued_id = queued.request_id
        correction = await submit_client_message(manager, session.id, "Steer the original build", [], "after-queue-steer", wiki_mode=None)
        assert correction["disposition"] == "steered"

        async def accepted():
            saved = await database.get_client_submission(session.id, "after-queue-steer")
            return any(entry["state"] == "accepted" for entry in saved["native_inputs"])

        await wait_until(accepted)
        (tmp_path / "native-finish").touch()
        await until(lambda: manager.dispatch_snapshot(session.id)["active_request_id"] == queued_id
                    and session._run.backend is not None and session._run.backend.active_turn is not None)
        (tmp_path / "native-finish").touch()
        assert (await asyncio.wait_for(queued.wait(), 4)).recording_state == "committed"
        await asyncio.wait_for(projected.wait(), 2)
        assert (await apps.get_application(row["id"]))["status"] == "ready"
    finally:
        await manager.interrupt(session.id)


@pytest.mark.parametrize(("independent_queued", "outside_handoff"), [
    (False, None), (True, None), (True, "independent"), (True, "carried"), (True, "later"),
])
async def test_failed_owner_with_confirmed_steer_projects_its_unavailable_build(
    creation, monkeypatch, independent_queued, outside_handoff,
):
    from server.app_backends import backend_supervisor
    from server.sessions.client_submissions import submit_client_message
    from tests.native_app_server import wait_until
    from tests.session_runtime import fail_destination_owner

    runtime, apps, builder = creation
    manager, database, _, mode, _, _ = runtime
    mode[0] = "steer_wait"
    consumed = asyncio.Event()
    completions, hooks = [], []

    async def complete(sid, completion):
        await apps._on_completion(sid, completion)
        completions.append(completion)
        if len(completions) == (3 if independent_queued else 2):
            consumed.set()

    async def rebuilt(*args, **kwargs):
        hooks.append(args)

    manager.on_request_complete(apps.BROADCAST_KEY, complete)
    monkeypatch.setattr(backend_supervisor, "on_rebuild", rebuilt)
    row = await apps.create_application(name="Failed steered builder", description="Retain original failure", agent_id=builder["id"])
    session = manager.get_session(row["session_id"])
    await until(lambda: any(event.get("session_id") == session.id and event.get("type") == "tool_use"
                            for event in runtime.events))
    if outside_handoff in {"independent", "carried"}:
        # The durable commit can precede receipt registration. The ended
        # Owner cannot claim this input merely because it shares a session.
        await database.admit_client_submission(session.id, "unregistered-input", "unregistered-digest", "unregistered-request",
            payload={"operation": {"kind": "message", "content": "Unregistered input", "attachment_ids": []}},
            receipt={"accepted": True, "disposition": "accepted"})
    queued = await manager.start_message(session.id, "Independent queued build", source="bg") if independent_queued else None
    if outside_handoff == "carried":
        assert await database.claim_client_submission(session.id, "unregistered-request", queued.request_id, "unregistered-generation")
        await database.set_steer_delivery(session.id, "unregistered-request", confirmed=True)
    correction = await submit_client_message(manager, session.id, "Adjust this build", [], "failed-app-steer", wiki_mode=None)
    assert correction["disposition"] == "steered"

    async def accepted():
        saved = await database.get_client_submission(session.id, "failed-app-steer")
        return any(entry["state"] == "accepted" for entry in saved["native_inputs"])

    await wait_until(accepted)
    if outside_handoff == "later":
        project = database.project_application_terminal
        admitted_later = False

        async def admit_before_projection(*args, **kwargs):
            nonlocal admitted_later
            if not admitted_later:
                admitted_later = True
                # This commit occurs after the fixed failure handoff, just
                # before the transaction that would publish its diagnostic.
                await database.admit_client_submission(session.id, "later-input", "later-digest", "later-request",
                    payload={"operation": {"kind": "message", "content": "Later input", "attachment_ids": []}},
                    receipt={"accepted": True, "disposition": "accepted"})
            return await project(*args, **kwargs)

        monkeypatch.setattr(database, "project_application_terminal", admit_before_projection)
    owner = await fail_destination_owner(manager, session, monkeypatch)
    try:
        await asyncio.wait_for(consumed.wait(), 3)
        assert all(completion.state == "unavailable" for completion in completions)
        original_ids = {completion.input_request_id for completion in completions}
        assert all(set(completion.ended_owner_inputs) == original_ids for completion in completions)
        current = await apps.get_application(row["id"])
        if outside_handoff:
            assert current["status"] == "building" and current["error"] is None
        else:
            assert current["status"] == "failed" and "owner failed" in current["error"].lower()
        assert current["last_built_at"] is None and hooks == []
        steer = await database.get_client_submission(session.id, "failed-app-steer")
        carrier = next(completion for completion in completions if completion.input_request_id != completion.request_id)
        assert steer["execution_owner"] == carrier.request_id and steer["terminal"] is None
        if queued is not None:
            assert (await queued.wait()).state == "unavailable"
    finally:
        await asyncio.wait_for(asyncio.shield(owner.resource_cleanup), 4)


@pytest.mark.parametrize("boundary", ["row", "session", "link"])
async def test_preparation_failures_close_the_resources_the_original_operation_created(creation, monkeypatch, boundary):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    target, method = (database, "save_application") if boundary == "row" else (
        manager, "create_session") if boundary == "session" else (database, "update_application")
    original = getattr(target, method)
    failed = False

    async def refuse(*args, **kwargs):
        nonlocal failed
        if not failed and (boundary != "link" or kwargs.get("session_id")):
            failed = True
            raise RuntimeError(f"{boundary} preparation refused before writing")
        return await original(*args, **kwargs)

    monkeypatch.setattr(target, method, refuse)
    operation = apps.create_application(name="Preparation failure", description="Retain known responsibility", agent_id=builder["id"])
    if boundary == "row":
        with pytest.raises(RuntimeError, match="row preparation refused"):
            await operation
        assert await apps.list_applications(user_id=builder["user_id"]) == []
        assert list(paths_for(builder["user_id"]).applications.iterdir()) == []
    else:
        row = await operation
        assert row["status"] == "failed" and f"{boundary} preparation refused" in row["error"]
        assert row["session_id"] is None
        assert await database.application_session_ids(row["id"]) == []
        assert Path(row["app_dir"]).is_dir()
    assert starts == []


async def test_preexisting_application_directory_is_never_claimed_or_removed(creation, monkeypatch):
    from server import applications

    runtime, apps, builder = creation
    directory = paths_for(builder["user_id"]).applications / "existing"
    directory.mkdir(parents=True)
    sentinel = directory / "existing.txt"
    sentinel.write_text("Existing user content")
    monkeypatch.setattr(applications, "allocate_app_dir", lambda *args: str(directory))
    with pytest.raises(applications.ApplicationError, match="resource already exists"):
        await apps.create_application(name="Existing destination", description="Refuse without cleanup", agent_id=builder["id"])
    assert sentinel.read_text() == "Existing user content"
    assert await apps.list_applications(user_id=builder["user_id"]) == []
    assert runtime.starts == []


async def test_followup_build_waits_for_the_original_preparation_binding(creation, monkeypatch):
    runtime, apps, builder = creation
    manager, database, _, _, _, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    create = manager.create_session
    first = True

    async def held_creation(*args, **kwargs):
        nonlocal first
        session = await create(*args, **kwargs)
        if first:
            first = False
            entered.set()
            await release.wait()
        return session

    monkeypatch.setattr(manager, "create_session", held_creation)
    creating = asyncio.create_task(apps.create_application(
        name="Preparation and followup", description="Keep one original builder", agent_id=builder["id"],
    ))
    followup = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        row, = await apps.list_applications(user_id=builder["user_id"])
        assert row["session_id"] is None
        followup = asyncio.create_task(apps.request_build(row["id"], "Another explicit build"))
        completed, _ = await asyncio.wait({followup}, timeout=.1)
        assert not completed, "Followup crossed the incomplete original preparation"
        release.set()
        original_row, followup_row = await asyncio.wait_for(asyncio.gather(creating, followup), 5)
        assert original_row["session_id"] == followup_row["session_id"]
        assert await database.application_session_ids(row["id"]) == [original_row["session_id"]]
    finally:
        release.set()
        await asyncio.gather(*(task for task in (creating, followup) if task is not None), return_exceptions=True)


async def test_conditional_cleanup_does_not_replace_an_explicit_session_delete(creation, monkeypatch):
    from server.sessions.owner_runtime import CloseExecution

    runtime, apps, builder = creation
    manager, database, _, _, _, _ = runtime
    row = await apps.create_application(name="Delete race", description="Retain explicit delete authority", agent_id=builder["id"])
    await manager.await_session_execution(row["session_id"], timeout=5)
    entered, release = asyncio.Event(), asyncio.Event()
    close = manager._close_original_execution

    async def hold_conditional(owner, command):
        if isinstance(command, CloseExecution) and command.only_empty_application is not None:
            entered.set()
            await release.wait()
        return await close(owner, command)

    monkeypatch.setattr(manager, "_close_original_execution", hold_conditional)
    cleanup = asyncio.create_task(manager.delete_session(row["session_id"], only_empty_application=row["id"]))
    deleting = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        deleting = asyncio.create_task(manager.delete_session(row["session_id"]))
        await asyncio.sleep(0)  # Explicit deletion has entered its original API.
        release.set()
        assert await asyncio.wait_for(cleanup, 5) is False
        assert await asyncio.wait_for(deleting, 5) is True
        assert manager.get_session(row["session_id"]) is None
        assert await database.get_session_binding(row["session_id"]) is None
    finally:
        release.set()
        await asyncio.gather(*(task for task in (cleanup, deleting) if task is not None), return_exceptions=True)


async def test_successful_empty_proof_joins_the_original_shared_deletion(creation, monkeypatch, tmp_path):
    runtime, _, builder = creation
    manager, database, _, _, _, _ = runtime
    directory = tmp_path / "empty-builder"
    directory.mkdir()
    await database.save_application(app_id="empty-delete-race", name="Empty delete race", app_dir=str(directory),
        created_at="now", updated_at="now", agent_id=builder["id"], user_id=builder["user_id"])
    session = await manager.create_session(builder["id"], working_dir=str(directory), origin="application", app_id="empty-delete-race")
    await database.update_application("empty-delete-race", session_id=session.id)
    entered, release = asyncio.Event(), asyncio.Event()
    close = manager._close_original_execution

    async def hold_explicit(owner, command):
        if command.only_empty_application is None:
            entered.set()
            await release.wait()
        return await close(owner, command)

    monkeypatch.setattr(manager, "_close_original_execution", hold_explicit)
    deleting = asyncio.create_task(manager.delete_session(session.id))
    cleanup = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        cleanup = asyncio.create_task(manager.delete_session(session.id, only_empty_application="empty-delete-race"))
        completed, _ = await asyncio.wait({cleanup}, timeout=.1)
        assert not completed, "Empty proof started a second physical deletion"
        assert manager.get_session(session.id) is session
        release.set()
        assert await asyncio.wait_for(deleting, 3) is True
        assert await asyncio.wait_for(cleanup, 3) is True
        assert await database.get_session_binding(session.id) is None
    finally:
        release.set()
        await asyncio.gather(*(task for task in (deleting, cleanup) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("boundary", ["before-row", "after-row", "after-session", "before-input", "after-input"])
async def test_waiter_cancellation_does_not_cancel_original_creation(creation, monkeypatch, tmp_path, boundary):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    save, create, start = database.save_application, manager.create_session, manager.start_message

    async def hold():
        entered.set()
        await release.wait()

    async def save_row(**kwargs):
        if boundary == "before-row":
            await hold()
        result = await save(**kwargs)
        if boundary == "after-row":
            await hold()
        return result

    async def create_session(*args, **kwargs):
        session = await create(*args, **kwargs)
        if boundary == "after-session":
            await hold()
        return session

    async def start_input(*args, **kwargs):
        if boundary == "before-input":
            await hold()
        receipt = await start(*args, **kwargs)
        if boundary == "after-input":
            await hold()
        return receipt

    monkeypatch.setattr(database, "save_application", save_row)
    monkeypatch.setattr(manager, "create_session", create_session)
    monkeypatch.setattr(manager, "start_message", start_input)
    waiter = asyncio.create_task(apps.create_application(
        name="Cancelled caller", description="Original owner completes", agent_id=builder["id"],
    ))
    original = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        original = next(task for task in manager._owned_tasks if task.get_name() == "create-application")
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not original.done()
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)
    row = await asyncio.wait_for(asyncio.shield(original), 5)
    await manager.await_session_execution(row["session_id"], timeout=5)
    async with asyncio.timeout(3):
        while (await database.get_application(row["id"]))["status"] == "building":
            await asyncio.sleep(0)
    assert len(starts) == 1
    assert sum(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path)) == 1


@pytest.mark.parametrize("entrypoint", [False, True], ids=["missing-entrypoint", "real-entrypoint"])
async def test_accepted_and_completed_input_keeps_its_result_after_a_lost_response(creation, monkeypatch, tmp_path, entrypoint):
    runtime, apps, builder = creation
    manager, database, _, _, starts, _ = runtime
    original = manager.start_message
    projected = asyncio.Event()
    request_ids = []

    async def observe(event):
        if event.get("type") == "application_updated" and event["application"]["status"] in {"ready", "failed"}:
            projected.set()

    async def lose_response(session_id, prompt, **kwargs):
        session = manager.get_session(session_id)
        if entrypoint:
            (Path(session.working_dir) / "index.html").write_text("<p>Real build output</p>")
        receipt = await original(session_id, prompt, **kwargs)
        request_ids.append(receipt.request_id)
        await asyncio.wait_for(receipt.wait(), 5)
        await asyncio.wait_for(projected.wait(), 5)
        raise RuntimeError("lost response after real completion")

    manager.on_broadcast("observe-creation-projection", observe)
    monkeypatch.setattr(manager, "start_message", lose_response)
    row = await asyncio.wait_for(apps.create_application(
        name="Completed before response", description="One original input", agent_id=builder["id"],
    ), 8)
    assert row["status"] == ("ready" if entrypoint else "failed")
    if entrypoint:
        assert row["error"] is None
    else:
        assert "index.html" in row["error"] and "lost response" not in row["error"]
    assert len(starts) == 1 and len(request_ids) == 1
    assert sum(frame.get("method") == "turn/start" for frame in wire_frames(tmp_path)) == 1
    original_input = await database.get_submission_for_request(row["session_id"], request_ids[0])
    assert original_input["receipt"]["accepted"]
