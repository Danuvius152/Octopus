"""Recovery through the original Owner, native process, input ledger and results."""

import asyncio
import json

import pytest

from server.config import settings
from tests.native_app_server import ROOT, inject, item, start_output, terminal, until
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


@pytest.mark.parametrize("info", ["serverOverloaded", "other", None])
@pytest.mark.parametrize("scope", ["root", "child"])
async def test_repeated_native_reconnect_activity_can_finish_in_the_original_attempt(native_runtime, tmp_path, monkeypatch, info, scope):
    from tests.native_app_server import child_end, child_start
    manager, database, session, _, starts, _ = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 3)
    receipt = await start_output(native_runtime)
    if scope == "child":
        inject(tmp_path, child_start())
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .2)
    error = {"message": "The selected model is at capacity. Reconnecting..."}
    if info:
        error["codexErrorInfo"] = info
    for _ in range(12):
        inject(tmp_path, {"method": "error", "params": {
            "threadId": ROOT if scope == "root" else "child", "turnId": "turn-1" if scope == "root" else "child-turn",
            "willRetry": True, "error": error}})
        await asyncio.sleep(.04)
    if scope == "child":
        inject(tmp_path, child_end())
    inject(tmp_path, item(ROOT, "turn-1", "final", "Recovered without new input"), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success"
    assert len(starts) == 1 and len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    assert any(row["type"] == "recovery" for row in await database.load_messages(session.id))


@pytest.mark.parametrize("error", [
    {"codexErrorInfo": "serverOverloaded", "message": "Provider at capacity"},
    {"codexErrorInfo": "other", "message": "The selected model is at capacity. Please try again later."},
    {"message": "The selected model is at capacity. Please try again later."},
])
async def test_transient_root_failure_continues_only_after_the_original_writer_retires(native_runtime, tmp_path, monkeypatch, error):
    manager, database, session, mode, starts, events = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .01)
    receipt = await start_output(native_runtime)
    original = starts[0].prompt
    (tmp_path / "external-effect.txt").write_text("The original effect must survive")
    old_run = session._run.backend
    mode[0] = "normal"
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = error
    inject(tmp_path, failure)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success"
    assert result.outcome.recovery["transient"] == 1 and len(starts) == 2
    assert old_run.retirement_state == "proven"
    assert starts[1].resume_id == ROOT and starts[1].prompt != original
    assert "Check existing files and tool outcomes" in starts[1].prompt
    assert "delegations and background jobs" in starts[1].prompt
    assert (tmp_path / "external-effect.txt").read_text() == "The original effect must survive"
    saved = await database.get_submission_for_request(session.id, receipt["request_id"])
    assert len(saved["native_inputs"]) == 2
    assert len({entry["attempt_id"] for entry in saved["native_inputs"]}) == 2
    assert all(entry["state"] == "accepted" for entry in saved["native_inputs"])
    assert len([event for event in events if event.get("type") == "result"]) == 1
    assert result.outcome.final_answer.turn_id == "turn-2"


@pytest.mark.parametrize("message", [
    "selected model is at capacity", "overloaded", "error 500", "error 502", "error 503", "error 504",
    "internal server error", "service unavailable", "bad gateway", "gateway timeout", "temporarily unavailable",
    "connection error", "connection reset", "connection refused", "stream error", "stream disconnected",
    "request timed out", "timed out", "temporarily limiting requests", "not your usage limit",
])
async def test_every_legacy_provider_failure_category_can_resume(native_runtime, tmp_path, monkeypatch, message):
    manager, database, session, mode, starts, _ = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    receipt = await start_output(native_runtime)
    mode[0] = "normal"
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"message": message, "codexErrorInfo": "other"}
    inject(tmp_path, failure)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success" and result.outcome.recovery["transient"] == 1
    assert len(starts) == 2


@pytest.mark.parametrize("info", ["internalServerError", {"httpConnectionFailed": {"httpStatusCode": 503}},
    {"responseStreamConnectionFailed": {"httpStatusCode": None}}, {"responseStreamDisconnected": {"httpStatusCode": None}},
    {"responseTooManyFailedAttempts": {"httpStatusCode": 504}}])
async def test_structured_provider_failures_can_resume_without_matching_text(native_runtime, tmp_path, monkeypatch, info):
    manager, database, session, mode, starts, _ = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    receipt = await start_output(native_runtime)
    mode[0] = "normal"
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"message": "Provider failure", "codexErrorInfo": info}
    inject(tmp_path, failure)
    await wait_dispatch_idle(manager, session.id)
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"
    assert len(starts) == 2


@pytest.mark.parametrize("error,retries", [
    ({"message": "Provider failed after reconnecting", "codexErrorInfo": "other",
      "additionalDetails": "The selected model is at capacity. Please try again later."}, 1),
    ({"message": "The selected model is at capacity",
      "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 503}},
      "additionalDetails": "insufficient_quota: account usage limit exceeded"}, 0),
])
async def test_bound_root_additional_details_participate_in_failure_classification(
    native_runtime, tmp_path, monkeypatch, error, retries,
):
    manager, database, session, mode, starts, _ = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    receipt = await start_output(native_runtime)
    mode[0] = "normal"
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = error
    inject(tmp_path, failure)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == ("success" if retries else "failed")
    assert result.outcome.recovery["transient"] == retries and len(starts) == retries + 1


@pytest.mark.parametrize("effort", ["low", None])
async def test_recovery_keeps_the_original_agent_and_native_default_model_snapshot(native_runtime, tmp_path, monkeypatch, effort):
    from server.agent_manager import AgentManager

    manager, database, session, mode, starts, _ = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    (tmp_path / "native-defaults.json").write_text(json.dumps({"model": "fixture", "effort": effort}))
    receipt = await start_output(native_runtime)
    (tmp_path / "native-defaults.json").write_text(json.dumps({"model": "new-native-default", "effort": "high"}))
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id,
        model="new-agent-default", reasoning_effort="high")
    mode[0] = "normal"
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": "serverOverloaded", "message": "Capacity"}
    inject(tmp_path, failure)
    await wait_dispatch_idle(manager, session.id)
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"
    selected = json.loads((tmp_path / "native-selected.json").read_text())
    assert selected == {"model": "fixture", "effort": effort}
    assert len(starts) == 2 and starts[1].model == starts[0].model


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("effort", ["low", None])
async def test_transient_preparation_refusal_keeps_the_first_resolved_native_controls(
    native_runtime, tmp_path, monkeypatch, resume, effort,
):
    from server.harness import codex_app_server

    manager, database, session, _, starts, _ = native_runtime
    if resume:
        seed = await manager.start_message(session.id, "Create the original history")
        assert (await seed.wait()).outcome.status == "success"
        await wait_dispatch_idle(manager, session.id)
        starts.clear()
    defaults = tmp_path / "native-defaults.json"
    defaults.write_text(json.dumps({"model": "first-native-model", "effort": effort}))
    (tmp_path / "native-preparation-error.json").write_text(json.dumps({
        "code": -32603, "message": "Selected model is at capacity. Please try a different model.",
        "data": {"codexErrorInfo": "serverOverloaded"},
    }))
    build = codex_app_server.build_argv

    def changed_defaults(context):
        if len(starts) == 1:
            defaults.write_text(json.dumps({"model": "changed-native-model", "effort": "high"}))
        return build(context)

    monkeypatch.setattr(codex_app_server, "build_argv", changed_defaults)
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    receipt = await manager.start_message(session.id, "Retain this request's first native selection")
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "success" and completion.outcome.recovery["transient"] == 1
    assert await receipt.execution.wait_resources() == ("proven", "proven")
    assert len(starts) == 2
    assert [context.resume_id for context in starts] == [ROOT if resume else None] * 2
    assert json.loads((tmp_path / "native-selected.json").read_text()) == {
        "model": "first-native-model", "effort": effort,
    }
    saved = await database.get_submission_for_request(session.id, receipt.request_id)
    assert [entry["state"] for entry in saved["native_inputs"]] == ["not_sent", "accepted"]
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == (2 if resume else 1)


@pytest.mark.parametrize("info,message", [
    ("usageLimitExceeded", "Selected model is at capacity"),
    ("unauthorized", "Selected model is at capacity"),
    ("other", "insufficient_quota: selected model is at capacity"),
    ("other", "insufficient credits: overloaded"),
    ("rateLimitExceeded", "Rate limit 429"),
    ({"httpConnectionFailed": {"httpStatusCode": 429}}, "Rate limit 429"),
])
async def test_quota_and_auth_facts_override_coincident_retry_words(native_runtime, tmp_path, info, message):
    manager, database, session, _, starts, _ = native_runtime
    receipt = await start_output(native_runtime)
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": info, "message": message}
    inject(tmp_path, failure)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and not any(result.outcome.recovery.values())
    assert len(starts) == 1


async def test_unowned_stderr_and_child_tool_errors_do_not_authorize_recovery(native_runtime, tmp_path):
    from tests.native_app_server import child_end, child_start

    manager, database, session, _, starts, _ = native_runtime
    receipt = await start_output(native_runtime)
    failed_child = child_end()
    failed_child["params"]["turn"].update(status="failed", error={"message": "selected model is at capacity"})
    inject(tmp_path, {"__stderr": "selected model is at capacity 401 Unauthorized"}, child_start(), failed_child,
        {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
            "id": "tool", "type": "commandExecution", "command": "false", "status": "failed", "exitCode": 1,
            "aggregatedOutput": "selected model is at capacity 401 Unauthorized"}}}, terminal("failed"))
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and not any(result.outcome.recovery.values())
    assert len(starts) == 1


async def test_transient_budget_is_not_reset_by_a_new_host(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _, starts, events = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .01)
    receipt = await start_output(native_runtime)
    for index in range(1, 4):
        await until(lambda index=index: session._run.backend is not None and session._run.backend.active_turn is not None
            and session._run.backend.active_turn.turn_id == f"turn-{index}")
        failure = terminal("failed")
        failure["params"]["turn"].update(id=f"turn-{index}", error={"message": "Selected model is at capacity"})
        inject(tmp_path, failure)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and result.outcome.recovery["transient"] == 2
    assert len(starts) == 3
    rows = await database.load_messages(session.id)
    retries = [row["content"] for row in rows if row["type"] == "recovery" and row["content"].get("phase") == "retrying"]
    assert [row["delay"] for row in retries] == [.01, .02]
    assert len([event for event in events if event.get("type") == "result"]) == 1


@pytest.mark.parametrize("action", ["stop", "revoke", "deadline"])
async def test_stop_authorization_and_overall_deadline_prevent_another_business_send(native_runtime, tmp_path, monkeypatch, action):
    from tests.test_session_manager import _bind_credential

    manager, database, session, _, starts, events = native_runtime
    credential = await _bind_credential(manager, "codex")
    await manager.update_session_configuration(session.id, {"credential_id": credential}, session.user_id)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .3)
    receipt = await start_output(native_runtime)
    if action == "deadline":
        monkeypatch.setattr(settings, "turn_max_seconds", .2)
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": "serverOverloaded", "message": "Capacity"}
    inject(tmp_path, failure)
    if action != "deadline":
        await until(lambda: any(event.get("retrying") for event in events))
        if action == "stop":
            await manager.interrupt(session.id)
        else:
            await database.delete_credential(credential)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == ("interrupted" if action == "stop" else "failed")
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    assert len(starts) <= (2 if action == "revoke" else 1)


async def test_unknown_input_cannot_use_an_old_thread_to_authorize_continuation(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, _ = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    # Establish a genuine prior history, then lose this input's acceptance.
    first = await manager.start_message(session.id, "Prior successful request")
    assert (await first.wait()).outcome.status == "success"
    mode[0] = "unknown"
    request = await manager.start_message(session.id, "Possibly effected request")
    await until(lambda: len(starts) == 2 and (tmp_path / "native-business.jsonl").read_text().count("Possibly effected request") == 1)
    inject(tmp_path, {"__stderr": "selected model is at capacity"})
    completion = await asyncio.wait_for(request.wait(), 3)
    saved = await database.get_submission_for_request(session.id, request.request_id)
    assert saved["native_inputs"][0]["state"] == "unknown"
    assert completion.outcome.status == "failed" and len(starts) == 2


async def test_a_single_retry_notice_does_not_disable_idle_timeout(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _, starts, _ = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 0)
    receipt = await start_output(native_runtime)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .1)
    inject(tmp_path, {"method": "error", "params": {
        "threadId": ROOT, "turnId": "turn-1", "willRetry": True, "error": {"message": "Reconnecting..."}}})
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.code == "turn_timeout" and len(starts) == 1


async def test_stop_while_the_failed_writer_is_retiring_prevents_continuation(native_runtime, tmp_path, monkeypatch):
    from server.harness import run as run_module

    manager, database, session, mode, starts, _ = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    entered, release = asyncio.Event(), asyncio.Event()
    original = run_module.wait_group_exit

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(run_module, "wait_group_exit", held)
    mode[0] = "control_group"
    receipt = await manager.start_message(session.id, "Stop while original resources retire")
    await until(lambda: (tmp_path / "native-group-ready").exists())
    old = session._run.backend
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": "serverOverloaded", "message": "Capacity"}
    try:
        inject(tmp_path, failure)
        await asyncio.wait_for(entered.wait(), 3)
        assert old.retirement_state == "pending" and len(starts) == 1
        await manager.interrupt(session.id)
    finally:
        (tmp_path / "native-group-release").touch()
        release.set()
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt.request_id)
    assert result.outcome.status == "interrupted" and len(starts) == 1
    assert old.retirement_state == "proven"


async def test_native_activity_cannot_extend_the_overall_logical_deadline(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _, starts, _ = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 0)
    receipt = await start_output(native_runtime)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .2)
    monkeypatch.setattr(settings, "turn_max_seconds", .4)
    for _ in range(15):
        inject(tmp_path, {"method": "error", "params": {
            "threadId": ROOT, "turnId": "turn-1", "willRetry": True, "error": {"message": "Reconnecting..."}}})
        await asyncio.sleep(.04)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and result.outcome.code == "turn_timeout"
    assert len(starts) == 1


@pytest.mark.parametrize("info", ["unauthorized", "usageLimitExceeded", "rateLimitExceeded"])
async def test_nonrecoverable_provider_fact_in_the_idle_tail_prevents_continuation(native_runtime, tmp_path, monkeypatch, info):
    manager, database, session, mode, starts, events = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 2)
    mode[0] = "control_stop_tail"
    receipt = await manager.start_message(session.id, "Retain the original effects")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": info, "message": "Provider refused this request"}
    (tmp_path / "native-stop-tail.json").write_text(json.dumps([failure]))
    mode[0] = "normal"  # A mistaken continuation would succeed and hide the refusal.
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .1)
    inject(tmp_path, item(ROOT, "turn-1", "prefix", "Known effect before idle"))
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "failed" and completion.outcome.code == "turn_timeout"
    assert completion.outcome.recovery["idle"] == 0 and len(starts) == 1
    assert len([event for event in events if event.get("type") == "result"]) == 1


@pytest.mark.parametrize("message,info,expected_code", [
    ("insufficient credits: overloaded", None, "usage_limit_exceeded"),
    ("selected model is at capacity", "usageLimitExceeded", "usage_limit_exceeded"),
    ("selected model is at capacity", "unauthorized", "permission_denied"),
    ("overloaded", {"httpConnectionFailed": {"httpStatusCode": 401}}, "permission_denied"),
])
@pytest.mark.parametrize("expire_idle", [False, True])
async def test_preparation_quota_failure_preserves_classification_before_transient_words(native_runtime, tmp_path, monkeypatch, message, info, expected_code, expire_idle):
    import os
    import sys

    from server.harness import HarnessRun, codex_app_server
    from tests.test_session_manager import _bind_credential

    manager, database, session, _, starts, _ = native_runtime
    credential = await _bind_credential(manager, "codex")
    await manager.update_session_configuration(session.id, {"credential_id": credential}, session.user_id)
    starts.clear()
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    if expire_idle:
        retire = HarnessRun.retire
        def expired_close(run, reason=None):
            # Apply the short budget at the refusal's real admission closure;
            # do not spend it on process startup or replace the cleanup owner.
            monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .001)
            return retire(run, reason)
        monkeypatch.setattr(HarnessRun, "retire", expired_close)
    error = {"code": -32603, "message": message, **({"data": {"codexErrorInfo": info}} if info else {})}
    peer = '''import json,sys
for line in sys.stdin:
 frame=json.loads(line)
 if 'id' not in frame: continue
 if frame.get('method')=='config/read': print(json.dumps({'id':frame['id'],'error':json.loads(sys.argv[1])}),flush=True)
 else: print(json.dumps({'id':frame['id'],'result':{}}),flush=True)
'''
    def argv(context):
        starts.append(context)
        return [sys.executable, "-u", "-c", peer, json.dumps(error)], {
            "cwd": context.working_dir, "env": {**os.environ, "CODEX_HOME": str(tmp_path / "codex-home")}}
    monkeypatch.setattr(codex_app_server, "build_argv", argv)
    receipt = await manager.start_message(session.id, "Never submit business input")
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "failed" and completion.outcome.provider_code == expected_code
    assert completion.outcome.recovery["transient"] == 0 and len(starts) == 1
    assert completion.outcome.recovery["idle"] == 0
    assert not (await database.get_credential(credential))["needs_reconnect"]
