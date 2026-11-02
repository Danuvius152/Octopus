"""Native errors keep their original source and never authorize another send."""

import asyncio

import pytest

from server.config import settings
from tests import native_app_server as native_tests
from tests.native_app_server import (
    ROOT,
    child_end,
    child_start,
    inject,
    item,
    terminal,
    until,
)
from tests.test_session_manager import _bind_credential

native_runtime = native_tests.native_runtime


def failed(info, message="Provider refused the request"):
    frame = terminal("failed")
    frame["params"]["turn"]["error"] = {"message": message, "codexErrorInfo": info}
    return frame


async def test_unrelated_401_in_failed_root_does_not_revoke_credential(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    credential = await _bind_credential(manager, "codex")
    await manager.update_session_configuration(session.id, {"credential_id": credential}, session.user_id)
    mode[0] = "control_output"
    receipt = await manager.start_message(session.id, "Use the connector")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, failed("other", "Connector error 401 Unauthorized"))
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "failed"
    assert not (await database.get_credential(credential))["needs_reconnect"]
    assert not any(event.get("code") == "auth_expired" for event in events)
    assert len(starts) == 1


@pytest.mark.parametrize("info", ["unauthorized", {"httpConnectionFailed": {"httpStatusCode": 401}},
                                  {"responseStreamConnectionFailed": {"httpStatusCode": 401}}])
async def test_only_bound_provider_auth_revokes_the_selected_credential(native_runtime, tmp_path, info):
    manager, database, session, mode, starts, events = native_runtime
    credential = await _bind_credential(manager, "codex")
    await manager.update_session_configuration(session.id, {"credential_id": credential}, session.user_id)
    mode[0] = "control_output"
    receipt = await manager.start_message(session.id, "Original request")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, failed(info))
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.code == "auth_expired"
    assert (await database.get_credential(credential))["needs_reconnect"]
    assert len([event for event in events if event.get("code") == "auth_expired"]) == 1
    assert len(starts) == 1 and not any(completion.outcome.recovery.values())


@pytest.mark.parametrize("last_status", ["completed", "failed"])
async def test_native_retry_and_child_errors_are_diagnostics_even_if_root_later_fails(native_runtime, tmp_path, last_status):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "control_output"
    receipt = await manager.start_message(session.id, "Keep the original attempt")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    retry = {"method": "error", "params": {"threadId": ROOT, "turnId": "turn-1", "willRetry": True,
        "error": {"message": "Temporary auth rejection", "codexErrorInfo": "unauthorized"}}}
    child_error = {"method": "error", "params": {"threadId": "child", "turnId": "child-turn", "willRetry": False,
        "error": {"message": "Child error 401 Unauthorized", "codexErrorInfo": "unauthorized"}}}
    inject(tmp_path, retry, child_start(), child_error, item(ROOT, "turn-1", "final", "Root answer"),
           terminal() if last_status == "completed" else failed("other", "Service unavailable"))
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == ("success" if last_status == "completed" else "failed")
    assert completion.outcome.code != "auth_expired"
    assert len(starts) == 1 and not any(completion.outcome.recovery.values())
    diagnostics = [row for row in await database.load_messages(session.id)
                   if row["type"] == "recovery" and row["tool_input"].get("provider_error")]
    assert len(diagnostics) >= 2
    retry_row = next(row for row in diagnostics if row["tool_input"]["provider_error"].get("willRetry"))
    child_row = next(row for row in diagnostics if row["content"]["message"] == "Child error 401 Unauthorized")
    assert retry_row["runtime_metadata"]["native"]["turn_id"] == "turn-1"
    assert child_row["runtime_metadata"]["native"]["thread_id"] == "child"
    assert retry_row["runtime_metadata"]["request_id"] == receipt.request_id


async def test_native_idle_timeout_never_continues_an_accepted_input(native_runtime, tmp_path, monkeypatch):
    manager, _, session, mode, starts, _ = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .08)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 2)
    mode[0] = "control_output"
    receipt = await manager.start_message(session.id, "One execution")
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status != "success"
    assert len(starts) == 1 and not any(completion.outcome.recovery.values())
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("noise", ["root_retry", "child_error", "child_terminal"])
async def test_repeated_native_errors_cannot_extend_idle_budget(native_runtime, tmp_path, monkeypatch, noise):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 2)
    monkeypatch.setattr(settings, "turn_max_seconds", 3)
    receipt = await manager.start_message(session.id, "Keep the original idle deadline")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, child_start())
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .2)
    error = {"message": "Still retrying without progress", "codexErrorInfo": "serverOverloaded"}
    if noise == "child_terminal":
        repeated = child_end()
        repeated["params"]["turn"]["status"] = "failed"
        repeated["params"]["turn"]["error"] = error
    else:
        repeated = {"method": "error", "params": {"error": error, "willRetry": noise == "root_retry",
            "threadId": ROOT if noise == "root_retry" else "child",
            "turnId": "turn-1" if noise == "root_retry" else "child-turn"}}
    for _ in range(12):
        inject(tmp_path, repeated)
        await asyncio.sleep(.04)
    assert not session._run.backend or not session._run.backend.question_admission_open
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.outcome.status == "failed" and completion.outcome.code == "turn_timeout"
    assert not any(completion.outcome.recovery.values())
    assert len(starts) == 1
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    assert any(row["type"] == "recovery" and (row["tool_input"] or {}).get("provider_error")
               for row in await database.load_messages(session.id))


@pytest.mark.parametrize("info,code", [("usageLimitExceeded", "usage_limit_exceeded"),
                                     ("rateLimitExceeded", "rate_limit_exceeded"),
                                     ("serverOverloaded", "server_overloaded")])
async def test_quota_and_exhausted_provider_retry_keep_one_attempt(native_runtime, tmp_path, info, code):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "control_output"
    receipt = await manager.start_message(session.id, "One request")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, {"__stderr": "Connector error 401 Unauthorized"}, failed(info))
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "failed" and not any(completion.outcome.recovery.values())
    assert completion.outcome.provider_code == code
    assert not any(event.get("code") == "auth_expired" for event in events)
    diagnostics = [row for row in await database.load_messages(session.id) if row["type"] == "recovery"]
    assert diagnostics[0]["content"]["code"] == code
    assert len(starts) == 1


async def test_generic_start_rpc_error_preserves_unknown_input(native_runtime, tmp_path):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "start_internal_error"
    receipt = await manager.start_message(session.id, "An uncertain effect")
    completion = await asyncio.wait_for(receipt.wait(), 4)
    saved = await database.get_submission_for_request(session.id, receipt.request_id)
    assert saved["native_inputs"][0]["state"] == "unknown"
    assert completion.outcome.status == "failed" and len(starts) == 1
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


async def test_retry_notification_before_ack_is_bound_and_retained_without_becoming_failure(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "stop_late_accepted"
    receipt = await manager.start_message(session.id, "One original root")
    await until(lambda: (tmp_path / "native-business.jsonl").exists())
    inject(tmp_path, {"method": "error", "params": {"threadId": ROOT, "turnId": "turn-1", "willRetry": True,
               "error": {"message": "Recovering response stream", "codexErrorInfo": "serverOverloaded"}}},
           {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1",
               "item": {"type": "userMessage", "id": "original", "clientId": receipt.request_id}}},
           item(ROOT, "turn-1", "final", "Recovered in this turn"), terminal())
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "success" and not any(completion.outcome.recovery.values())
    diagnostics = [row for row in await database.load_messages(session.id) if row["type"] == "recovery"]
    assert diagnostics[0]["runtime_metadata"]["native"]["turn_id"] == "turn-1"
    assert diagnostics[0]["tool_input"]["provider_error"]["willRetry"] is True
    assert (await database.get_submission_for_request(session.id, receipt.request_id))["native_inputs"][0]["state"] == "accepted"
    assert len(starts) == 1
