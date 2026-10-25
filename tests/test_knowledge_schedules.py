"""Both schedule PATCH adapters obey one immutable knowledge definition."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from server.agent_manager import AgentManager
from server.deps import get_session_manager
from server.knowledge import decode_context
from server.routers import schedules
from server.scheduler import ScheduleRunner
from server.users import UserManager
from tests import test_schedule_tool as schedule_api
from tests.test_delegation_work import work_runtime as work_runtime

client = schedule_api.client


@pytest.mark.parametrize("scoped", [False, True])
async def test_schedule_prompt_edit_preserves_read_and_null_cannot_clear_it(client, scoped):
    sid = await schedule_api._session(client)
    response = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "/wiki Explain X", "interval_seconds": 600, "wiki_mode": "read"},
        headers=client.headers,
    )
    assert response.status_code == 201
    schedule_id = response.json()["id"]
    url = f"/api/sessions/{sid}/schedules/{schedule_id}" if scoped else f"/api/schedules/{schedule_id}"

    changed = await client.patch(url, json={"prompt": "/wiki maintain Library/Y.md"}, headers=client.headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["wiki_mode"] == "read"
    rejected = await client.patch(url, json={"wiki_mode": None}, headers=client.headers)
    assert rejected.status_code == 422
    stored = (await client.get("/api/schedules", headers=client.headers)).json()[0]
    assert stored["wiki_mode"] == "read"


async def test_human_can_explicitly_reauthorize_definition_without_editing_prompt(client):
    sid = await schedule_api._session(client)
    created = await client.post(f"/api/sessions/{sid}/schedules", headers=client.headers,
                                json={"prompt": "Explain X", "interval_seconds": 600, "wiki_mode": "read"})
    changed = await client.patch(f"/api/schedules/{created.json()['id']}", headers=client.headers,
                                json={"wiki_mode": "auto"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["wiki_mode"] == "auto"


@pytest.fixture
async def plan_runtime(work_runtime, monkeypatch):
    manager, _, caller, starts = work_runtime
    runner = ScheduleRunner(manager, manager.db)
    monkeypatch.setattr(schedules, "_db", manager.db)
    monkeypatch.setattr(schedules, "_runner", runner)
    app = FastAPI()
    app.include_router(schedules.router)
    app.include_router(schedules.session_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    agent = await AgentManager(manager.db).create_agent(name="Planner", backend="codex", user_id=caller.user_id)
    parent = await manager.create_session(agent["id"], backend="codex")
    try:
        token = await UserManager(manager.db).issue_token(parent.user_id)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                    headers={"Authorization": "Bearer " + token}) as api:
            yield manager, runner, parent, starts, api
    finally:
        await runner.shutdown()


async def live_caller(manager, parent, starts, *, work_id=None, prompt="/wiki controlled source question", mode=None):
    from server.mcp_identity import scope_is_live, verify

    receipt = await manager.start_message(parent.id, prompt, work_id=work_id, wiki_mode=mode)
    context = await asyncio.wait_for(starts.get(), 5)
    bearer = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    scope = verify(bearer)
    async with asyncio.timeout(5):
        while not scope_is_live(scope):
            await asyncio.sleep(.01)
    return receipt, {"Authorization": f"Bearer {bearer}"}


async def test_mcp_creation_inherits_read_even_when_the_scheduled_body_requests_maintenance(plan_runtime, tmp_path):
    manager, runner, parent, starts, api = plan_runtime
    receipt, headers = await live_caller(manager, parent, starts, mode="read")
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=headers, json={
        "prompt": "/wiki maintain Library/Y.md", "interval_seconds": 3600, "in_session": False,
    })
    assert created.status_code == 201, created.text
    assert created.json()["wiki_mode"] == "read"
    row = (await manager.db.load_schedules())[0]
    assert decode_context(row["knowledge_context"]).trusted_request == "/wiki controlled source question"
    (tmp_path / "release").touch()
    assert (await receipt.wait()).outcome.status == "success"
    await runner._fire(row["id"])
    fired = (await manager.db.load_schedules())[0]
    messages = await manager.db.load_messages(fired["last_run_session_id"])
    knowledge = next(m["tool_input"]["request_context"]["knowledge"]["value"] for m in messages if m["role"] == "user")
    assert knowledge["mode"] == "read" and knowledge["maintenance"]["may_submit"] is False
    assert knowledge["sources"] == ["wiki", "web"]


@pytest.mark.parametrize("scoped", [False, True])
async def test_mcp_edit_intersects_saved_and_callers_limits_in_both_patch_adapters(plan_runtime, scoped):
    manager, _runner, parent, starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers,
                             json={"prompt": "Human task", "interval_seconds": 3600})
    schedule_id = created.json()["id"]
    _, headers = await live_caller(manager, parent, starts, mode="read")
    url = f"/api/sessions/{parent.id}/schedules/{schedule_id}" if scoped else f"/api/schedules/{schedule_id}"
    edited = await api.patch(url, headers=headers, json={"prompt": "/wiki maintain Library/Y.md", "wiki_mode": "auto"})
    assert edited.status_code == 200, edited.text
    saved = decode_context((await manager.db.load_schedules())[0]["knowledge_context"])
    assert saved.mode == "read" and saved.maintenance.may_submit is False
    assert saved.trusted_request == "Human task"


async def test_legacy_missing_contract_stays_visible_without_starting_a_turn(plan_runtime):
    manager, runner, parent, starts, _api = plan_runtime
    await manager.db.save_schedule(schedule_id="legacy", agent_id=parent.agent_id, name="old", prompt="/wiki maintain X",
                                   interval_seconds=3600, created_at="2026-10-19")
    await runner._fire("legacy")
    row = (await manager.db.load_schedules())[0]
    assert row["prompt"] == "/wiki maintain X" and not row["enabled"]
    assert row["invalid_reason"] == "knowledge_contract_missing"
    assert starts.empty() and await manager.db.load_messages(parent.id) == []


async def test_human_reauthorization_clears_missing_contract_without_enabling_the_plan(plan_runtime):
    manager, runner, parent, _starts, api = plan_runtime
    await manager.db.save_schedule(schedule_id="legacy", agent_id=parent.agent_id, name="old", prompt="Explain X",
                                   interval_seconds=3600, created_at="2026-10-19")
    await runner._fire("legacy")
    response = await api.patch("/api/schedules/legacy", headers=api.headers, json={"wiki_mode": "read"})
    assert response.status_code == 200, response.text
    assert response.json()["invalid_reason"] is None
    assert response.json()["enabled"] is False


async def test_definition_edit_does_not_change_a_fire_already_captured_by_its_owner(plan_runtime, tmp_path):
    manager, runner, parent, starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers, json={
        "prompt": "/wiki controlled first definition", "interval_seconds": 3600, "in_session": False, "wiki_mode": "read",
    })
    schedule_id = created.json()["id"]
    first = asyncio.create_task(runner._fire(schedule_id))
    try:
        await asyncio.wait_for(starts.get(), 5)
        changed = await api.patch(f"/api/schedules/{schedule_id}", headers=api.headers,
                                 json={"prompt": "Second definition", "wiki_mode": "auto"})
        assert changed.status_code == 200, changed.text
        first_sid = (await manager.db.load_schedules())[0]["last_run_session_id"]
        (tmp_path / "release").touch()
        await first
        await runner._fire(schedule_id)
        second_sid = (await manager.db.load_schedules())[0]["last_run_session_id"]
        assert first_sid != second_sid
        contexts = []
        for sid in (first_sid, second_sid):
            contexts.append(next(m["tool_input"]["request_context"]["knowledge"]["value"]
                                 for m in await manager.db.load_messages(sid) if m["role"] == "user"))
        assert contexts[0]["mode"] == "read" and contexts[0]["trusted_request"] == "/wiki controlled first definition"
        assert contexts[1]["mode"] == "auto" and contexts[1]["trusted_request"] == "Second definition"
    finally:
        (tmp_path / "release").touch()
        await first


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("narrowing", [{"wiki_mode": "read"}, {"wiki_mode": "maintain", "prompt": "/wiki maintain Library/X.md"}])
async def test_concurrent_name_edit_cannot_restore_the_previous_maintenance_contract(plan_runtime, monkeypatch, scoped, narrowing):
    manager, _runner, parent, _starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers,
                             json={"prompt": "Human task", "interval_seconds": 3600})
    schedule_id = created.json()["id"]
    url = f"/api/sessions/{parent.id}/schedules/{schedule_id}" if scoped else f"/api/schedules/{schedule_id}"
    prepared, release = asyncio.Event(), asyncio.Event()
    validate = schedules.validate_schedule_enabling

    async def pause_name_edit(existing, updates):
        if updates.get("name") == "paused name edit":
            prepared.set()
            await release.wait()
        await validate(existing, updates)

    monkeypatch.setattr(schedules, "validate_schedule_enabling", pause_name_edit)
    pending = asyncio.create_task(api.patch(url, headers=api.headers, json={"name": "paused name edit"}))
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        narrowed = await api.patch(url, headers=api.headers, json=narrowing)
        assert narrowed.status_code == 200
    finally:
        release.set()
    stale = await pending
    assert stale.status_code in {200, 409}, stale.text
    saved = (await manager.db.load_schedules())[0]
    limit = decode_context(saved["knowledge_context"]).maintenance
    assert saved["wiki_mode"] == narrowing["wiki_mode"]
    if narrowing["wiki_mode"] == "read":
        assert limit.may_submit is False
    else:
        assert limit.required_selections == ("/wiki maintain Library/X.md",)


@pytest.mark.parametrize("scoped", [False, True])
async def test_mcp_target_refinement_survives_definition_and_fire(plan_runtime, tmp_path, scoped):
    manager, runner, parent, starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers,
                             json={"prompt": "Human task", "interval_seconds": 3600, "in_session": False})
    schedule_id = created.json()["id"]
    receipt, headers = await live_caller(manager, parent, starts, prompt="controlled ordinary task")
    url = f"/api/sessions/{parent.id}/schedules/{schedule_id}" if scoped else f"/api/schedules/{schedule_id}"
    edited = await api.patch(url, headers=headers, json={"prompt": "/wiki maintain Library/X.md", "wiki_mode": "maintain"})
    assert edited.status_code == 200, edited.text
    (tmp_path / "release").touch()
    await receipt.wait()
    await runner._fire(schedule_id)
    row = (await manager.db.load_schedules())[0]
    context = next(message["tool_input"]["request_context"]["knowledge"]["value"]
                   for message in await manager.db.load_messages(row["last_run_session_id"]) if message["role"] == "user")
    assert context["maintenance"]["required_selections"] == ["/wiki maintain Library/X.md"]


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("changes", [{"wiki_mode": "read"}, {"name": "Further restricted plan"}])
async def test_mcp_edit_without_body_keeps_new_source_limits_in_the_actual_fire(plan_runtime, tmp_path, scoped, changes):
    manager, runner, parent, starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers,
                             json={"prompt": "/wiki Compare X", "interval_seconds": 3600, "in_session": False})
    schedule_id = created.json()["id"]
    request = "controlled: this plan must use local notes only; do not make new web requests."
    receipt, headers = await live_caller(manager, parent, starts, prompt=request)
    url = f"/api/sessions/{parent.id}/schedules/{schedule_id}" if scoped else f"/api/schedules/{schedule_id}"
    changed = await api.patch(url, headers=headers, json=changes)
    assert changed.status_code == 200, changed.text
    (tmp_path / "release").touch()
    await receipt.wait()
    await runner._fire(schedule_id)
    row = (await manager.db.load_schedules())[0]
    context = next(message["tool_input"]["request_context"]["knowledge"]["value"]
                   for message in await manager.db.load_messages(row["last_run_session_id"]) if message["role"] == "user")
    assert request in context["task_requirements"]
    assert context["sources"] == ["wiki", "web"]  # The specific limit is preserved verbatim for interpretation.


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("boundary", ["prepare", "write"])
async def test_work_closed_during_schedule_preparation_cannot_publish_a_definition(plan_runtime, monkeypatch, operation, boundary):
    import uuid

    from server.mcp_identity import close_work

    manager, runner, parent, starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers,
                             json={"prompt": "Original task", "interval_seconds": 3600})
    schedule_id = created.json()["id"]
    before = (await manager.db.load_schedules())[0]
    work_id = uuid.uuid4().hex
    _, headers = await live_caller(manager, parent, starts, work_id=work_id)
    prepared, release = asyncio.Event(), asyncio.Event()
    validate = schedules.validate_schedule_target

    async def pause_target(*args, **kwargs):
        await validate(*args, **kwargs)
        prepared.set()
        await release.wait()

    if boundary == "prepare":
        monkeypatch.setattr(schedules, "validate_schedule_target", pause_target)
    else:
        from tests.test_runtime_review_races import gate_sql

        verb = "INSERT INTO schedules" if operation == "create" else "UPDATE schedules SET"
        prepared, release = gate_sql(monkeypatch, manager.db, lambda sql: sql.startswith(verb))
    if operation == "create":
        call = api.post(f"/api/sessions/{parent.id}/schedules", headers=headers,
                        json={"prompt": "New task", "interval_seconds": 3600, "in_session": False})
    else:
        call = api.patch(f"/api/schedules/{schedule_id}", headers=headers, json={"prompt": "Changed task", "enabled": True})
    pending = asyncio.create_task(call)
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        close_work(work_id)
    finally:
        release.set()
    rejected = await pending
    assert rejected.status_code == 409, rejected.text
    assert await manager.db.load_schedules() == [before]
    assert runner.next_run_at(schedule_id) == created.json()["next_run_at"]


@pytest.mark.parametrize("scoped", [False, True])
async def test_maintenance_only_edit_keeps_the_definitions_source_contract(plan_runtime, scoped):
    manager, _runner, parent, _starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers, json={
        "prompt": "/wiki Explain X; do not make new web requests", "interval_seconds": 3600,
    })
    schedule_id = created.json()["id"]
    url = f"/api/sessions/{parent.id}/schedules/{schedule_id}" if scoped else f"/api/schedules/{schedule_id}"
    assert (await api.patch(url, headers=api.headers, json={"prompt": "Current scheduled task"})).status_code == 200
    changed = await api.patch(url, headers=api.headers, json={"wiki_mode": "auto"})
    assert changed.status_code == 200, changed.text
    contract = decode_context((await manager.db.load_schedules())[0]["knowledge_context"])
    assert contract.mode == "auto" and contract.sources == ("wiki", "web")
    assert "do not make new web requests" in contract.trusted_request


async def test_delayed_registration_cannot_restore_an_already_disabled_plan(plan_runtime, monkeypatch):
    manager, runner, parent, _starts, api = plan_runtime
    created = await api.post(f"/api/sessions/{parent.id}/schedules", headers=api.headers,
                             json={"prompt": "Human task", "interval_seconds": 3600})
    schedule_id = created.json()["id"]
    url = f"/api/schedules/{schedule_id}"
    entered, release = asyncio.Event(), asyncio.Event()
    reschedule = runner.reschedule

    async def delayed_registration(row):
        if row["name"] == "delayed registration":
            entered.set()
            await release.wait()
        await reschedule(row)

    monkeypatch.setattr(runner, "reschedule", delayed_registration)
    old = asyncio.create_task(api.patch(url, headers=api.headers, json={"name": "delayed registration"}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        disabled = await api.patch(url, headers=api.headers, json={"enabled": False, "wiki_mode": "read", "name": "New definition"})
        assert disabled.status_code == 200
    finally:
        release.set()
    assert (await old).status_code == 200
    assert (await manager.db.load_schedules())[0]["enabled"] is False
    assert runner.next_run_at(schedule_id) is None


@pytest.mark.parametrize("in_session", [False, True])
@pytest.mark.parametrize("abort", ["close", "cancel"])
async def test_fire_reads_the_confirmed_definition_after_update_interruption(plan_runtime, monkeypatch, tmp_path, in_session, abort):
    import uuid

    from server.mcp_identity import close_work

    manager, runner, parent, starts, api = plan_runtime
    destination = await manager.create_session(parent.agent_id, "Schedule destination", str(tmp_path))
    created = await api.post(f"/api/sessions/{destination.id}/schedules", headers=api.headers, json={
        "prompt": "Original scheduled task", "interval_seconds": 3600, "in_session": in_session,
    })
    schedule_id = created.json()["id"]
    work_id = uuid.uuid4().hex
    receipt, headers = await live_caller(manager, parent, starts, work_id=work_id)
    scheduled = []
    start_message = manager.start_message

    async def capture_fire(*args, **kwargs):
        accepted = await start_message(*args, **kwargs)
        if kwargs.get("source") == "schedule":
            scheduled.append(accepted)
        return accepted

    monkeypatch.setattr(manager, "start_message", capture_fire)
    written, release, snapshot = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = manager.db.conn._execute
    schedule_row = runner._schedule_row

    async def pause_after_sql(function, *args, **kwargs):
        result = await execute(function, *args, **kwargs)
        if args and isinstance(args[0], str) and args[0].startswith("UPDATE schedules SET") and not written.is_set():
            written.set()
            await release.wait()
        return result

    async def observe_snapshot(identifier):
        result = await schedule_row(identifier)
        snapshot.set()
        return result

    monkeypatch.setattr(manager.db.conn, "_execute", pause_after_sql)
    monkeypatch.setattr(runner, "_schedule_row", observe_snapshot)
    patch = asyncio.create_task(api.patch(f"/api/schedules/{schedule_id}", headers=headers,
                                          json={"prompt": "Updated definition"}))
    fire = None
    try:
        await asyncio.wait_for(written.wait(), 5)
        fire = asyncio.create_task(runner._fire(schedule_id))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(snapshot.wait(), .1)
        assert not snapshot.is_set(), "A definition reader crossed the unconfirmed write"
        if abort == "close":
            close_work(work_id)
        else:
            patch.cancel()
        release.set()
        (tmp_path / "release").touch()
        if abort == "close":
            assert (await patch).status_code == 409
        else:
            with pytest.raises(asyncio.CancelledError):
                await patch
        await receipt.wait()
        await asyncio.wait_for(fire, 5)
        # In-session fire returns on admission. Shutdown can cancel that input
        # before recording; wait for its original completion instead.
        assert len(scheduled) == 1 and scheduled[0].accepted
        completion = await asyncio.wait_for(scheduled[0].wait(), 5)
        assert completion.recording_state == "committed" and completion.outcome.status == "success"
        row = (await manager.db.load_schedules())[0]
        # Closing the original work refuses and rolls back the definition.
        # Cancelling only its waiter preserves the acknowledged transaction.
        expected = "Original scheduled task" if abort == "close" else "Updated definition"
        assert row["prompt"] == expected
        inputs = [message for message in await manager.db.load_messages(row["last_run_session_id"])
                  if message["role"] == "user" and message["tool_input"]["request_context"]["source"] == "schedule"]
        assert len(inputs) == 1
        assert inputs[0]["content"].endswith("\n\n" + expected)
        if abort == "close":
            assert "Updated definition" not in inputs[0]["content"]
    finally:
        release.set()
        (tmp_path / "release").touch()
        await asyncio.gather(patch, *([fire] if fire is not None else []), return_exceptions=True)
