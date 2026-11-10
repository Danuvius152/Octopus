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
