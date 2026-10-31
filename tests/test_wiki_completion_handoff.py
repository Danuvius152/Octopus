"""Wiki effect verification follows a committed receipt after its writer ends."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.harness import get_harness
from server.harness.profile import StdinMode
from server.mcp_identity import verify
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiWorker
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


@pytest.fixture
async def completion_worker(jobs, client, tmp_path, monkeypatch):
    manager = wiki_api.session_manager
    await manager.db.update_agent(jobs.registration["curator_agent_id"], backend="codex")
    model = tmp_path / "wiki_completion_model.py"
    model.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "sid,gate=sys.argv[1],Path(sys.argv[2])\n"
        "gate.with_suffix('.input').write_text(sys.stdin.readline())\n"
        "print(json.dumps({'type':'thread.started','thread_id':sid}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'original','type':'agent_message',"
        "'text':'Original curator body for '+sid}}),flush=True)\n"
        "while not gate.exists(): time.sleep(.005)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    runtime = SimpleNamespace(
        manager=manager, client=client, jobs=jobs, root=tmp_path,
        starts=[], processes=[], backends=[], receipts=[], frames=[], operations=[],
        target=None, committed=None, failed=False,
        projection_entered=asyncio.Event(), projection_release=asyncio.Event(),
    )
    worker = WikiWorker()
    worker.manager = manager
    runtime.worker = worker
    harness = get_harness("codex")

    def argv(context):
        scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "wiki"))
        assert scope is not None
        gate = tmp_path / f"{scope.run_lease_id}.result"
        runtime.starts.append((scope, context, gate))
        return [sys.executable, "-u", str(model), scope.session_id, str(gate)], {"cwd": context.working_dir}

    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        stdin_mode=StdinMode.STREAM_JSON, can_steer=True, reusable=True, build_turn_argv=argv,
    ))
    spawn, create, submit = asyncio.create_subprocess_exec, harness.create_run, manager.start_message
    sequence = manager.db.message_sequence_state

    async def observe_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            runtime.processes.append(process)
        return process

    def observe_backend(*args, **kwargs):
        backend = create(*args, **kwargs)
        runtime.backends.append(backend)
        return backend

    async def observe_submission(session_id, prompt, **kwargs):
        receipt = await submit(session_id, prompt, **kwargs)
        runtime.receipts.append((session_id, receipt))
        if runtime.target is None and kwargs.get("source") == "wiki":
            runtime.target = (session_id, receipt)
        return receipt

    async def fail_projection_after_original_commit(session_id):
        value = await sequence(session_id)
        if runtime.target is not None and session_id == runtime.target[0] and not runtime.failed:
            original = await manager.db.get_committed_terminal(session_id, runtime.target[1].request_id)
            if original is not None:
                assert original["status"] == "success"
                runtime.committed = original
                runtime.projection_entered.set()
                await runtime.projection_release.wait()
                runtime.failed = True
                raise RuntimeError("Wiki sequence projection failed after the original terminal commit returned")
        return value

    async def observe_frame(event):
        runtime.frames.append(event)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(harness, "create_run", observe_backend)
    monkeypatch.setattr(manager, "start_message", observe_submission)
    monkeypatch.setattr(manager.db, "message_sequence_state", fail_projection_after_original_commit)
    manager.on_broadcast("wiki-completion-proof", observe_frame)
    try:
        yield runtime
    finally:
        runtime.projection_release.set()
        for _scope, _context, gate in runtime.starts:
            gate.touch()
        for operation in runtime.operations:
            if not operation.done():
                operation.cancel()
        await asyncio.gather(*runtime.operations, return_exceptions=True)
        try:
            await asyncio.wait_for(manager.drain_turns(timeout=4), 5)
            await manager.stop_all_held_processes()
        finally:
            manager.remove_broadcast("wiki-completion-proof")
            for process in runtime.processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 3)
            assert all(not Path(f"/proc/{process.pid}").exists() for process in runtime.processes)


def original_job(runtime, suffix):
    path = f"Library/{suffix}.md"
    raw = f"# Original {suffix}\nKeep this argument and its exact condition.\n"
    WikiHistory(runtime.jobs.store).write(path, raw.encode(), None, "human", "Create original maintenance input")
    job = runtime.jobs.maintain(suffix, "", "Review this original note", [path], None, [], "human")
    assert runtime.jobs.next_queued() == job["id"]
    return job, raw.encode()


async def publish_original(runtime, job):
    operation = asyncio.create_task(runtime.worker.process(runtime.jobs, job["id"]))
    runtime.operations.append(operation)
    async with asyncio.timeout(3):
        while True:
            session_id = runtime.jobs.get(job["id"])["session_id"]
            if session_id is not None and any(event.get("type") == "assistant_text" and event.get("session_id") == session_id
                                              for event in runtime.frames):
                break
            if operation.done():
                operation.result()
                raise AssertionError("Maintenance completed without starting its original model")
            await asyncio.sleep(.005)
    scope, context, gate = next(start for start in runtime.starts if start[0].session_id == session_id)
    headers = {"Authorization": "Bearer " + next(entry.credential for entry in context.mcp_servers if entry.key == "wiki")}
    item = runtime.jobs.get(job["id"])["items"][0]
    read = await runtime.client.get("/api/wiki/read", params={"doc": item["doc"]}, headers=headers)
    assert read.status_code == 200, read.text
    published = await runtime.client.post("/api/wiki/organize", headers=headers, json={
        "path": item["path"], "expected_revision": read.json()["revision"],
        "key_note": "The original argument applies only under its exact condition.",
        "reason": "Verify the assigned original before completing this maintenance request",
    })
    assert published.status_code == 200, published.text
    output = runtime.jobs.store.path(item["path"]).read_bytes()
    assert runtime.jobs.publication(job["id"], item["doc"]) is not None
    return operation, scope, gate, output


async def require_original_facts(runtime, job, original, output):
    session_id, receipt = runtime.target
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.input_request_id == completion.request_id == receipt.request_id
    assert completion.state == "completed" and completion.recording_state == "committed"
    assert completion.outcome.status == "success" and completion.notification_state == "unavailable"
    assert completion.outcome.model_dump() == runtime.committed
    assert await runtime.manager.db.get_committed_terminal(session_id, receipt.request_id) == runtime.committed
    rows = await runtime.manager.db.load_committed_request_messages(session_id, receipt.request_id)
    assert [row["content"] for row in rows if row["type"] == "text"] == ["Original curator body for " + session_id]
    assert len([row for row in rows if row["type"] == "result"]) == 1
    item = runtime.jobs.get(job["id"])["items"][0]
    assert runtime.jobs.store.path(item["path"]).read_bytes() == output
    title, original_body = original.split(b"\n", 1)
    assert output.startswith(title + b"\n") and output.endswith(original_body)
    assert session_id not in tasks.assignments
    assert not any(task.job_id == job["id"] for task in tasks.active.values())
    assert runtime.failed and len(runtime.starts) == 1
    assert all(process.returncode is not None and not Path(f"/proc/{process.pid}").exists()
               for process in runtime.processes)
    assert all(backend._stdout_task is None and backend._stderr_task is None for backend in runtime.backends)


async def require_following_job(runtime):
    """The same worker and account can claim and verify the next actual job."""
    before = len(runtime.starts)
    following, original = original_job(runtime, "following-job")
    operation, scope, gate, output = await publish_original(runtime, following)
    gate.touch()
    await asyncio.wait_for(operation, 4)
    receipt = next(receipt for session_id, receipt in runtime.receipts if session_id == scope.session_id)
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.state == "completed" and completion.recording_state == "committed"
    assert completion.outcome.status == "success"
    assert await runtime.manager.db.get_committed_terminal(scope.session_id, receipt.request_id) == completion.outcome.model_dump()
    result = runtime.jobs.get(following["id"])
    assert result["status"] == "completed"
    assert result["items"][0]["status"] == "done" and result["items"][0]["index_complete"] is True
    assert runtime.jobs.store.path(result["items"][0]["path"]).read_bytes() == output
    title, original_body = original.split(b"\n", 1)
    assert output.startswith(title + b"\n") and output.endswith(original_body)
    assert len(runtime.starts) == before + 1
    assert runtime.manager.get_session(scope.session_id) is None
    assert all(process.returncode is not None for process in runtime.processes)


@pytest.mark.parametrize("later_input", [False, True])
async def test_worker_verifies_original_effect_after_committed_owner_failure(completion_worker, later_input):
    runtime = completion_worker
    job, original = original_job(runtime, "completed-owner")
    operation, scope, gate, output = await publish_original(runtime, job)
    correction = None
    if later_input:
        correction = await runtime.manager.start_message(scope.session_id, "Keep this later human condition.", request_id="later-human")
        assert correction.accepted
    gate.touch()
    await asyncio.wait_for(runtime.projection_entered.wait(), 3)
    runtime.projection_release.set()
    await asyncio.wait_for(operation, 4)
    await require_original_facts(runtime, job, original, output)
    finished = runtime.jobs.get(job["id"])
    assert finished["status"] == "completed"
    assert finished["items"][0]["status"] == "done" and finished["items"][0]["index_complete"] is True
    assert finished["items"][0]["worker_request"]["recording_state"] == "committed"
    rows = await runtime.manager.db.load_sessions(include_archived=True)
    original_session = next(row for row in rows if row["id"] == scope.session_id)
    if correction is None:
        assert original_session["archived"] and runtime.manager.get_session(scope.session_id) is None
    else:
        assert not original_session["archived"] and runtime.manager.get_session(scope.session_id) is not None
        assert [item["request_id"] for item in runtime.manager.dispatch_snapshot(scope.session_id)["items"]] == [correction.request_id]
        assert (await correction.wait()).outcome is None
        assert await runtime.manager.db.get_committed_terminal(scope.session_id, correction.request_id) is None
    await require_following_job(runtime)


async def test_cancelled_worker_preserves_the_original_commit_and_finishes_cleanup(completion_worker):
    runtime = completion_worker
    job, original = original_job(runtime, "cancelled-owner")
    operation, scope, gate, output = await publish_original(runtime, job)
    gate.touch()
    await asyncio.wait_for(runtime.projection_entered.wait(), 3)
    runtime.jobs.mark_cancelled(job["id"])
    operation.cancel()
    runtime.projection_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 4)
    await require_original_facts(runtime, job, original, output)
    cancelled = runtime.jobs.get(job["id"])
    assert cancelled["status"] == "cancelled" and cancelled["settlement_phase"] == "confirmed"
    assert cancelled["items"][0]["status"] == "done" and cancelled["items"][0]["index_complete"] is True
    assert runtime.manager.get_session(scope.session_id) is None
    await require_following_job(runtime)
