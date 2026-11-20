"""Ticket handoff through the original SessionOwner and two native conversations."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.bg_tasks import BgTaskManager
from server.database import Database
from server.deps import get_session_manager
from server.fork_helpers import build_lost_history_replay
from server.knowledge import capture, decode_context, derive
from server.mcp_identity import McpScope, mint
from server.models import MessageContent
from server.routers.sessions import get_submission_status
from server.sessions import SessionManager
from server.sessions.base import Session
from server.sessions.client_submissions import (
    input_digest,
    submission_status,
    submit_client_message,
)
from server.sessions.handoff import (
    HandoffPointers,
    begin_ticket_batch,
    finish_ticket_batch,
    limit_ticket_batch,
    ticket_handoff,
)
from server.sessions.submission import (
    ReturnBody,
    ReturnRefused,
    ReturnTarget,
    SubmissionReceipt,
)
from server.storage_verification import verify_main
from server.submission_models import SubmissionConflict
from server.wiki import job_events, job_notifications
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from tests.callback_api import start_callback_api
from tests.claude_runtime import SID
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inject as claude_inject
from tests.claude_runtime import inputs as claude_inputs
from tests.native_app_server import (
    ROOT,
    active_root,
    inject,
    item,
    until,
    wait_until,
    wire_frames,
)
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle

POINTERS = HandoffPointers(spec="spec.md", accepted_ticket="issues/A.md", candidate="fixed-A",
                           next_work="issues/B.md")


def live_scope(chat):
    run = chat._run
    return McpScope(chat.id, None, chat.user_id, run.wiki_task_id, run.backend.run_lease_id, run.work_id)


async def handoff_state(manager, chat, key="batch-A"):
    return await get_submission_status(manager, chat.id, f"handoff:{key}", user_id=chat.user_id)


def with_human_scope(pointers, decisions):
    return HandoffPointers.model_validate(pointers.model_dump() | {"human_scope": [
        {"message": message, "carry": [{"scope": scope, "text": text} for scope, text in clauses]}
        for message, clauses in decisions.items()
    ]})


def prompt_json_after(prompt: str, marker: str) -> str:
    _, separator, original = prompt.partition(marker)
    assert separator
    value = json.loads(original.splitlines()[0])
    assert isinstance(value, str)
    return value


def scope_instruction_from(prompt: str) -> str:
    return prompt_json_after(prompt, "The exact human message for scope confirmation follows as a JSON string.\n")


def presented_request(prompt: str) -> str:
    return prompt_json_after(prompt, "可信原请求及其约束：")


def codex_input_text(start: dict[str, Any]) -> str:
    return next(part["text"] for part in start["input"] if part["type"] == "text")


async def confirm_human_steer(manager: SessionManager, chat: Session, client_id: str, text: str) -> None:
    receipt = await submit_client_message(manager, chat.id, text, [], client_id, wiki_mode=None)
    assert receipt["disposition"] == "steered"

    async def confirmed():
        return (await submission_status(manager, chat.id, client_id))["execution"]["delivery_state"] == "confirmed"

    await wait_until(confirmed)


def copy_product_database(root, label):
    path = root / f"{label}.db"
    with closing(sqlite3.connect(root / "product.db")) as source, closing(sqlite3.connect(path)) as dest:
        source.backup(dest)
    return path


async def call_batch_tool(manager, chat, name, arguments, *, token=None):
    token = mint(**asdict(live_scope(chat))) if token is None else token
    api = await start_callback_api(overrides={get_session_manager: lambda: manager}, readiness_token=token)
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http, streamable_http_client(
            f"http://127.0.0.1:{api.port}/mcp/ask_agent/mcp", http_client=http,
        ) as (read, write, _), ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool(name, arguments)
            assert not result.isError
            return result.structuredContent
    finally:
        await api.stop()


async def test_accepted_ticket_starts_next_in_new_thread_with_original_scope(native_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr("server.harness.codex_rpc.RPC_TIMEOUT", 3)
    manager, database, chat, mode, _, _ = native_runtime
    sentinel = "PRIVATE-CONTEXT-OF-TICKET-A"
    trusted = "Implement the authorized batch from spec.md. Keep final release for acceptance."
    assert (await (await manager.start_message(chat.id, sentinel)).wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    api = await start_callback_api(overrides={get_session_manager: lambda: manager},
                                   readiness_token=mint(chat.id, user_id=chat.user_id))
    try:
        mode[0] = "control_output"
        active = await manager.start_message(chat.id, trusted)
        await until(lambda: chat._run.backend is not None and chat._run.backend.active_turn is not None
                    and chat._run.backend.active_turn.turn_id == "turn-2")
        scope = live_scope(chat)
        token = mint(**asdict(scope))
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http, streamable_http_client(
            f"http://127.0.0.1:{api.port}/mcp/ask_agent/mcp", http_client=http,
        ) as (read, write, _), ClientSession(read, write) as client:
            await client.initialize()
            begin = await client.call_tool("begin_ticket_batch", {})
            assert not begin.isError
            assert begin.structuredContent == {"state": "registered", "original_request_id": active.request_id}
            assert (await client.call_tool("begin_ticket_batch", {})).structuredContent == begin.structuredContent
            result = await client.call_tool("handoff", {"handoff_id": "batch-A", "pointers": POINTERS.model_dump()})
            assert not result.isError
            registered = result.structuredContent
            original = await client.call_tool("handoff", {"handoff_id": "batch-A"})
            assert original.structuredContent == registered
    finally:
        await api.stop()
    assert registered["state"] == "registered"
    assert active.completion is None
    assert (await ticket_handoff(manager, scope, "batch-A", POINTERS)) == registered
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-2", "status": "completed"}}})
    assert (await active.wait()).outcome.status == "success"
    async def second_started():
        return "batch-A: started" in (await handoff_state(manager, chat))["message"]
    await wait_until(second_started)
    frames = wire_frames(tmp_path)
    assert sum(frame.get("method") == "thread/start" for frame in frames) == 2
    starts = [frame["params"] for frame in frames if frame.get("method") == "turn/start"]
    assert len(starts) == 3
    assert starts[-1]["threadId"] != starts[-2]["threadId"]
    prompts = str(starts[-1]["input"])
    assert trusted in prompts and "issues/B.md" in prompts and sentinel not in prompts
    assert "Read the accepted ticket for its checks and review" not in prompts
    assert manager.get_session(chat.id) is chat and Path(chat.working_dir) == tmp_path
    assert sentinel in [m["content"] for m in await database.load_messages(chat.id)]
    next_boundary = HandoffPointers(spec="spec.md", accepted_ticket="issues/B.md", candidate="fixed-B", next_work="final acceptance")
    assert (await ticket_handoff(manager, live_scope(chat), "batch-B", next_boundary))["state"] == "registered"
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": starts[-1]["threadId"],
                                                              "turn": {"id": "turn-3", "status": "completed"}}})
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-B: started" in (await handoff_state(manager, chat, "batch-B"))["message"]
    final = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"][-1]
    assert final["threadId"] != starts[-1]["threadId"]
    assert "final acceptance" in str(final["input"]) and trusted in str(final["input"]) and "fixed-A" not in str(final["input"])


async def test_handoffs_and_returns_include_only_the_requirements_for_the_receiving_ticket(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    root = "Complete the authorized batch."
    local = "/wiki Check where this ticket gets its configuration."
    progress = "现在进展怎么样？卡在哪儿了？"
    instruction = "继续，这是我个人项目"
    question = "，为什么会有安全风险"
    batch = ("For the whole batch, if the snapshot is unlocked,\n"
             "  do not rename the shared fixture. Leave the baseline untouched.\n"
             "Except for the copy used by tests.")
    next_only = "Use the reviewed fixture for ticket B."
    inline_local, inline_batch, inline_next = "本票补本机说明。", "全批次保留两个验收入口。", "下一票只核对沙盒快照。"
    inline = inline_local + inline_batch + inline_next
    short_local, short_next = "本票核对共享清单", "下一票核对备用输入"
    short_mixed = short_local + "\n" + short_next
    list_local, list_next = "- 可以发布。", "- Do not update the snapshots."
    current_list = f"本票：\n- 检查回归。\n{list_local}"
    next_list = f"For the next ticket:\n- Do not rename the fixture.\n{list_next}"
    batch_exception = "全批次允许使用测试样本。全批次的一项例外是未签字的样本不使用。"
    batch_condition = "整个批次都暂停发布。下一票如果 CI 转绿，这项限制仍然有效。"
    conditioned = "如果候选环境允许\n下一票可以重建夹具。\n前提是上游快照已锁定。\n仍须先取得复审批准。"
    conditioned_mixed = conditioned + "\n发布夹具维持原样。"
    repeated = "Keep the fixture intentionally small."
    repeated_mixed = f"本票记录这段原文 {repeated}\n{repeated}"
    hold_local = "/wiki 只在本票查发布记录并说明回滚日志中的例外。"
    held_operation = "夹具发布"
    hold_mixed = hold_local + "\n下一票按上面的例外判定是否允许" + held_operation + "。"
    release = "先别发布。"
    mixed = instruction + question + "？\n" + batch + "\n" + next_only
    original = await manager.start_message(chat.id, root)
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    authority = capture(chat, scope, prompt="").context

    def starts():
        return [frame["params"] for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"]

    def projection(start):
        return presented_request(codex_input_text(start))

    def finish(turn_number):
        inject(tmp_path, {"method": "turn/completed", "params": {
            "threadId": starts()[-1]["threadId"], "turn": {"id": f"turn-{turn_number}", "status": "completed"},
        }})

    async def admit_review(target):
        async def material():
            return ReturnBody(f"Completed review {target.delivery_id}")

        returned = await manager.admit_return(target, prepare=material)
        assert isinstance(returned, SubmissionReceipt)
        await until(lambda: chat._steer_open)
        return returned

    review_a = ReturnTarget(chat.id, "delegation", "review-a", scope.work_id,
                           derive(capture(chat, scope, prompt="Review A"), "Review A"))
    for client_id, text in (("local", local), ("progress", progress), ("mixed", mixed), ("inline", inline),
                            ("conditioned", conditioned_mixed), ("repeated", repeated_mixed), ("hold", hold_mixed),
                            ("release", release), ("short", short_mixed), ("batch-exception", batch_exception),
                            ("current-list", current_list), ("next-list", next_list), ("batch-condition", batch_condition)):
        await confirm_human_steer(manager, chat, client_id, text)
    inject(tmp_path, item(ROOT, "turn-1", "progress-answer", "The review found a gap which is now fixed."))
    assert (await ticket_handoff(manager, scope, "batch-A"))["state"] == "unregistered"
    first = with_human_scope(POINTERS, {
        mixed: (("batch", batch), ("next", next_only)),
        inline: (("next", inline_next), ("batch", inline_batch)),
        conditioned_mixed: (("next", conditioned),),
        short_mixed: (("next", short_next),),
        batch_exception: (("batch", batch_exception),),
        batch_condition: (("batch", batch_condition),),
        next_list: (("next", list_next),),
    })
    withdrawn = await ticket_handoff(manager, scope, "withdrawn", POINTERS)
    assert withdrawn["state"] == "registered"
    assert await manager.cancel_request(chat.id, withdrawn["request_id"])
    finish(1)
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    review_a_return = await admit_review(review_a)
    assert all(text in projection(starts()[-1]) for text in (progress, instruction, question, batch, next_only, release))
    scope = live_scope(chat)
    with pytest.raises(ValueError, match="exactly match one confirmed human steer"):
        await ticket_handoff(manager, scope, "batch-A", POINTERS.model_copy(update={"answered_progress_questions": (root,)}))
    invalid = with_human_scope(POINTERS, {mixed: (("batch", "Apply this invented requirement"),)})
    with pytest.raises(ValueError, match="exactly once in its original human update"):
        await ticket_handoff(manager, scope, "batch-A", invalid)
    overlapping = with_human_scope(POINTERS, {mixed: (("batch", batch), ("next", batch + "\n" + next_only))})
    with pytest.raises(ValueError, match="must not overlap"):
        await ticket_handoff(manager, scope, "batch-A", overlapping)
    ambiguous = with_human_scope(POINTERS, {repeated_mixed: (("batch", repeated),)})
    with pytest.raises(ValueError, match="exactly once"):
        await ticket_handoff(manager, scope, "batch-A", ambiguous)
    wrong_offset = ambiguous.model_dump()
    wrong_offset["human_scope"][0]["carry"][0]["start"] = repeated_mixed.rindex(repeated) + 1
    with pytest.raises(ValueError, match="given start offset"):
        await ticket_handoff(manager, scope, "batch-A", HandoffPointers.model_validate(wrong_offset))
    scoped = first.model_dump()
    scoped["human_scope"] = [*scoped["human_scope"],
        {"message": repeated_mixed, "carry": [{"scope": "batch", "text": repeated, "start": repeated_mixed.rindex(repeated)}]},
        {"message": hold_mixed, "carry": [{"scope": "next", "text": held_operation, "hold": True}]}]
    first = HandoffPointers.model_validate(scoped)
    assert (await ticket_handoff(manager, scope, "batch-A"))["state"] == "unregistered"
    registered = await call_batch_tool(manager, chat, "handoff", {"handoff_id": "batch-A", "pointers": scoped})
    assert registered["state"] == "registered"
    assert registered == await ticket_handoff(manager, scope, "batch-A", first)
    with pytest.raises(SubmissionConflict):
        await ticket_handoff(manager, scope, "batch-A", POINTERS)
    finish(2)
    assert (await review_a_return.wait()).outcome.status == "success"
    await until(lambda: chat._steer_open)
    assert "batch-A: started" in (await handoff_state(manager, chat))["message"]
    assert "issues/B.md" in str(starts()[-1]["input"])

    scope = live_scope(chat)
    review_b = ReturnTarget(chat.id, "delegation", "review-b", scope.work_id,
                           derive(capture(chat, scope, prompt="Review B"), "Review B"))
    finish(3)
    await wait_dispatch_idle(manager, chat.id)
    review_b_return = await admit_review(review_b)
    next_pointers = HandoffPointers(spec="spec.md", accepted_ticket="issues/B.md", candidate="fixed-B", next_work="issues/C.md")
    scope = live_scope(chat)
    with pytest.raises(ValueError, match="exactly match one confirmed human steer"):
        await ticket_handoff(manager, scope, "batch-B", next_pointers.model_copy(update={"answered_progress_questions": (progress,)}))
    with pytest.raises(ValueError, match="unreviewed, confirmed human message"):
        await ticket_handoff(manager, scope, "batch-B", with_human_scope(next_pointers, {release: (("batch", release),)}))
    await ticket_handoff(manager, scope, "batch-B", next_pointers)
    mode[0] = "normal"
    finish(4)
    assert (await review_b_return.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-B: started" in (await handoff_state(manager, chat, "batch-B"))["message"]

    _, _, ticket_b, callback_b, ticket_c = starts()
    omitted = (local, progress, instruction, question, release, inline_local, "发布夹具维持原样。",
               "本票记录这段原文", hold_local, "按上面的例外", short_local, list_local)
    enduring = (root, batch, inline_batch, repeated, batch_exception, batch_condition)
    expiring = (next_only, inline_next, conditioned, held_operation, short_next, list_next)
    for phase in (ticket_b, callback_b, ticket_c):
        assert all(text not in projection(phase) for text in omitted)
        assert all(text in projection(phase) for text in enduring)
    assert all(text in projection(phase) for phase in (ticket_b, callback_b) for text in expiring)
    for phase in (ticket_b, callback_b):
        prompt = projection(phase)
        assert "须暂缓的操作" in prompt and json.dumps(held_operation, ensure_ascii=False) in prompt
        assert "继续其余已授权工作" in prompt
        assert "本轮知识来源要求（需实际取得的依据，不是维护权限）：[]" in codex_input_text(phase)
    assert all(text not in projection(ticket_c) for text in expiring)
    assert "issues/C.md" in str(ticket_c["input"])
    visible = await database.load_messages(chat.id)
    assert any(message["role"] == "user" and message["content"] == progress for message in visible)
    assert any(message["role"] == "user" and message["content"] == mixed for message in visible)
    assert any(message["role"] == "user" and message["content"] == hold_mixed for message in visible)
    for key in ("batch-A", "batch-B"):
        row = await database.get_client_submission(chat.id, f"handoff:{key}")
        stored_context = decode_context(row["payload"]["knowledge"])
        assert mixed in stored_context.trusted_request and question not in stored_context.model_request
        assert not stored_context.sources
        assert stored_context.origin == authority.origin
        assert (stored_context.mode, stored_context.maintenance) == (authority.mode, authority.maintenance)
    fork_point = "Continue C before forking"
    assert (await (await manager.start_message(chat.id, fork_point)).wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    cut = next(message["seq"] for message in await database.load_messages(chat.id) if message["content"] == fork_point)
    branch = await manager.fork_session(chat.id, cut, user_id=chat.user_id)
    assert (await (await manager.start_message(branch.id, "Continue the fork")).wait()).outcome.status == "success"
    replay = codex_input_text(starts()[-1])
    assert "<fork-history" in replay and "issues/C.md" in replay
    assert all(json.dumps(text, ensure_ascii=False)[1:-1] not in replay for text in (*omitted, *expiring))
    assert all(json.dumps(text, ensure_ascii=False)[1:-1] in replay for text in enduring)


async def test_legacy_handoff_retries_with_the_same_pointers(native_runtime, tmp_path):
    manager, database, chat, _, _, _ = native_runtime
    await active_root(native_runtime, tmp_path)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    registered = await ticket_handoff(manager, scope, "batch-A", POINTERS)
    old_prompt = ("[ticket-handoff batch-A] Call the handoff tool with this ID and no pointers in this turn. "
                  "The tool waits briefly for the original first input receipt. "
                  "When its state is started, record confirmation in the accepted ticket and continue within the "
                  "original authorization. If unconfirmed, leave the ticket handoff pending. "
                  "Read the accepted ticket for its checks and review. These references are working material.\n"
                  + POINTERS.model_dump_json(exclude_defaults=True))
    old_digest = input_digest({"content": old_prompt, "attachment_ids": [], "wiki_mode": None})
    async with database.transaction(write=True) as tx:
        await tx.execute("UPDATE client_submissions SET payload_sha256=?, payload=json_remove("
                         "json_set(payload,'$.operation.content',?),'$.answered_progress_request_ids','$.handoff_human_scope') "
                         "WHERE session_id=? AND client_id='handoff:batch-A'", (old_digest, old_prompt, chat.id))
    assert await ticket_handoff(manager, scope, "batch-A", POINTERS) == registered
    assert await ticket_handoff(manager, scope, "batch-A") == registered
    with closing(sqlite3.connect(tmp_path / "product.db")) as reader:
        verify_main(reader)
    changed = POINTERS.model_copy(update={"next_work": "issues/other.md"})
    with pytest.raises(SubmissionConflict):
        await ticket_handoff(manager, scope, "batch-A", changed)


async def test_legacy_scope_is_repaired_from_confirmed_input_before_the_next_ticket(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    active = await manager.start_message(chat.id, "Implement the authorized batch.")
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)

    source_control = "/wiki"
    local = "本票先核查本机记录。"
    valid = "全批次核对共有审查输入。"
    negated = "For the whole batch, do not rename the sample fixture."
    truncated = "fixture."
    expired = "For the next ticket, check the ticket B fixture only."
    original = "\n".join((source_control, local, valid, negated, expired))
    await confirm_human_steer(manager, chat, "old-steer", original)
    source = await database.get_client_submission(chat.id, "old-steer")
    existing = with_human_scope(POINTERS, {original: (
        ("batch", source_control), ("batch", valid), ("batch", negated), ("next", expired),
    )})
    registered = await ticket_handoff(manager, scope, "batch-A", existing)

    def complete(number):
        start = [frame["params"] for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"][-1]
        inject(tmp_path, {"method": "turn/completed", "params": {
            "threadId": start["threadId"], "turn": {"id": f"turn-{number}", "status": "completed"}}})

    complete(1)
    assert (await active.wait()).outcome.status == "success"
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    assert (await ticket_handoff(manager, scope, "batch-A"))["state"] == "started"
    # Seed the journal and visible boundary that an older writer would have saved.
    legacy_clauses = [{"scope": "batch", "text": source_control}, {"scope": "batch", "text": valid},
                      {"scope": "next", "text": truncated},
                      {"scope": "batch", "text": expired}]
    old_pointers = POINTERS.model_dump(exclude_defaults=True) | {
        "human_scope": [{"message": original, "carry": legacy_clauses}]}
    old_digest = hashlib.sha256(json.dumps(old_pointers, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    boundary = await database.get_submission_for_request(chat.id, registered["request_id"])
    knowledge = json.loads(boundary["payload"]["knowledge"])
    presented = knowledge["value"]["presented_request"]
    assert negated in presented
    next_prefix = "这条要求只适用于当前票。\n"
    knowledge["value"]["presented_request"] = (
        presented.replace(negated, next_prefix + truncated).replace(next_prefix + expired, expired)
    )
    old_knowledge = json.dumps(knowledge, ensure_ascii=False)
    async with database.transaction(write=True) as tx:
        await tx.execute("UPDATE client_submissions SET payload_sha256=?, "
                         "payload=json_set(payload,'$.handoff_human_scope',json(?),'$.knowledge',?) "
                         "WHERE session_id=? AND client_id=?",
                         (old_digest.hexdigest(), json.dumps({source["request_id"]: legacy_clauses}), old_knowledge,
                          chat.id, "handoff:batch-A"))
        await tx.execute("UPDATE messages SET tool_input=json_remove(json_set(tool_input,'$.request_context.knowledge',json(?)),"
                         "'$.request_context.handoff_scope_verified') WHERE session_id=? "
                         "AND json_extract(tool_input,'$.request_context.request_id')=?",
                         (old_knowledge, chat.id, registered["request_id"]))
    retried = await ticket_handoff(manager, scope, "batch-A", HandoffPointers.model_validate(old_pointers))
    assert retried["request_id"] == registered["request_id"] and retried["state"] == "started"
    target = ReturnTarget(chat.id, "delegation", "legacy-review", scope.work_id,
                          derive(capture(chat, scope, prompt="Review B"), "Review B"))
    complete(2)
    await wait_dispatch_idle(manager, chat.id)

    async def material():
        return ReturnBody("Ticket B review completed.")

    callback = await manager.admit_return(target, prepare=material)
    assert isinstance(callback, SubmissionReceipt)
    await until(lambda: chat._steer_open)
    frame = [f["params"] for f in wire_frames(tmp_path) if f.get("method") == "turn/start"][-1]
    callback_projection = presented_request(codex_input_text(frame))
    assert all(text not in callback_projection for text in (valid, expired, local, truncated))
    assert capture(chat, live_scope(chat), prompt="").context.sources == ()
    assert "请核对原聊天消息 #" in callback_projection and "暂缓相关操作" in callback_projection
    assert "继续其余已授权工作" in callback_projection

    next_pointers = HandoffPointers(spec="spec.md", accepted_ticket="issues/B.md", candidate="fixed-B", next_work="issues/C.md")
    scope = live_scope(chat)
    pending = await call_batch_tool(manager, chat, "handoff", {"handoff_id": "batch-B", "pointers": next_pointers.model_dump()})
    assert pending["state"] == "unregistered"
    correction, = pending["human_scope_corrections"]
    assert correction["request_id"] == source["request_id"] and correction["message"] == original
    assert correction["carry"] == legacy_clauses
    assert (await ticket_handoff(manager, scope, "batch-B"))["state"] == "unregistered"

    corrected = {"request_id": correction["request_id"], "message": original, "carry": [
        {"scope": "batch", "text": clause} for clause in (source_control, valid, negated)
    ]}
    with pytest.raises(ValueError, match="unreviewed, confirmed human message"):
        wrong = corrected | {"request_id": "different-request"}
        await ticket_handoff(manager, scope, "batch-B",
                             HandoffPointers.model_validate(next_pointers.model_dump() | {"human_scope": [wrong]}))
    assert (await ticket_handoff(manager, scope, "batch-B"))["state"] == "unregistered"
    updated = await ticket_handoff(manager, scope, "batch-B",
                                   HandoffPointers.model_validate(next_pointers.model_dump() | {"human_scope": [corrected]}))
    assert updated["state"] == "registered"
    mode[0] = "normal"
    complete(3)
    assert (await callback.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-B: started" in (await handoff_state(manager, chat, "batch-B"))["message"]
    frame = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"][-1]
    projection = presented_request(codex_input_text(frame))
    assert valid in projection and negated in projection and expired not in projection and "暂缓" not in projection
    assert '["wiki", "web"]' in codex_input_text(frame)
    visible = await database.load_messages(chat.id)
    cut = next(row["seq"] for row in visible
               if (row["tool_input"] or {}).get("request_context", {}).get("request_id") == callback.request_id)
    branch = await manager.fork_session(chat.id, cut, user_id=chat.user_id)
    assert (await (await manager.start_message(branch.id, "Continue the older fork")).wait()).outcome.status == "success"
    frame = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"][-1]
    replay = codex_input_text(frame)
    assert "<fork-history" in replay and "issues/B.md" in replay and "issues/C.md" not in replay
    assert prompt_json_after(replay, "human scope at handoff: ") == callback_projection
    prior_message = next(row for row in await database.load_messages(chat.id)
                         if (row["tool_input"] or {}).get("handoff_id") == "batch-A")
    assert prior_message["tool_input"]["request_context"]["knowledge"] == knowledge

    # An archived original can be removed while its copied boundary remains.
    await wait_dispatch_idle(manager, branch.id)
    assert await manager.delete_session(chat.id)
    copied_opening = next(row for row in await database.load_messages(branch.id)
                          if row["content"] == "Continue the older fork")
    grandchild = await manager.fork_session(branch.id, copied_opening["seq"], user_id=chat.user_id)
    assert (await (await manager.start_message(grandchild.id, "Continue with the ticket materials")).wait()).outcome.status == "success"
    frame = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"][-1]
    replay = codex_input_text(frame)
    assert "issues/B.md" in replay and "Confirm the original ticket restrictions" in replay
    assert "human scope at handoff" not in replay and truncated not in replay
    copied_boundary = next(row for row in await database.load_messages(grandchild.id)
                           if (row["tool_input"] or {}).get("handoff_id") == "batch-A")
    assert copied_boundary["tool_input"]["request_context"] == prior_message["tool_input"]["request_context"]


@pytest.mark.parametrize("root", [
    "Implement the authorized tickets and keep release fixtures untouched.",
    "Implement the authorized tickets.\n\n人类更新任务正文（保留已有控制与限制）：\nKeep release fixtures untouched.",
], ids=["plain-root", "root-quotes-framework-heading"])
async def test_legacy_fork_replays_the_batch_request_when_the_old_projection_only_omitted_a_message(native_runtime, tmp_path, root):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    local = "本票核对本机草稿。"
    active = await manager.start_message(chat.id, root)
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    await confirm_human_steer(manager, chat, "local-note", local)
    registered = await ticket_handoff(manager, scope, "batch-A", POINTERS)
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {
        "threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"},
    }})
    assert (await active.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    row = await database.get_submission_for_request(chat.id, registered["request_id"])
    assert decode_context(row["payload"]["knowledge"]).presented_request == root
    async with database.transaction(write=True) as tx:
        await tx.execute("UPDATE messages SET tool_input=json_remove(tool_input,'$.request_context.handoff_scope_verified') "
                         "WHERE session_id=? AND json_extract(tool_input,'$.request_context.request_id')=?",
                         (chat.id, registered["request_id"]))
    fork_point = "Continue ticket B before the fork"
    assert (await (await manager.start_message(chat.id, fork_point)).wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    cut = next(row["seq"] for row in await database.load_messages(chat.id) if row["content"] == fork_point)
    branch = await manager.fork_session(chat.id, cut, user_id=chat.user_id)
    assert await manager.delete_session(chat.id)
    assert (await (await manager.start_message(branch.id, "Continue after deletion")).wait()).outcome.status == "success"
    frame = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"][-1]
    replay = codex_input_text(frame)
    assert prompt_json_after(replay, "human scope at handoff: ") == root
    assert "issues/B.md" in replay and local not in replay and "Confirm the original ticket restrictions" not in replay


async def test_claude_receipt_proves_new_native_history_received_the_exact_first_input(claude_runtime, tmp_path, monkeypatch):
    manager, database, chat, _, events = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "fresh-claude-B", "fresh-claude-C", "recovered-C", "fork-C"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    active = await manager.start_message(chat.id, "Use only spec.md and stop before release.")
    first, = await claude_inputs(tmp_path, 1)
    sentinel = "PRIVATE-CLAUDE-NOTES-OF-A"
    claude_inject(tmp_path, {"type": "assistant", "session_id": SID, "user_message_uuid": first["input"]["uuid"],
        "message": {"id": "ticket-a-note", "model": "deepseek-flash", "content": [{"type": "text", "text": sentinel}]}})
    await until(lambda: any(e.get("content") == sentinel for e in events))
    await begin_ticket_batch(manager, live_scope(chat))
    await until(lambda: chat._steer_open)
    progress = "What is the current progress?"
    current_a = "本票补本机发布清单。"
    batch = "全批次只使用已锁定的快照。\n除非测试只读生成的副本。"
    next_b = "下一票先核对备用清单。"
    held_operation = "替换真实快照"
    mixed = "\n".join((current_a, batch, next_b, f"下一票按前述例外再决定能否{held_operation}。"))
    for client_id, text in (("progress-a", progress), ("scope-a", mixed)):
        await confirm_human_steer(manager, chat, client_id, text)
    claude_inject(tmp_path, {"type": "assistant", "session_id": SID, "user_message_uuid": first["input"]["uuid"],
        "message": {"id": "progress-a-answer", "model": "deepseek-flash",
                    "content": [{"type": "text", "text": "Ticket A passed its checks."}]}})
    scoped = POINTERS.model_dump() | {"answered_progress_questions": [progress], "human_scope": [
        {"message": mixed, "carry": [{"scope": "batch", "text": batch}, {"scope": "next", "text": next_b},
                                     {"scope": "next", "text": held_operation, "hold": True}]}]}
    registered = await call_batch_tool(manager, chat, "handoff", {"handoff_id": "batch-A", "pointers": scoped})
    assert registered["state"] == "registered"
    assert await ticket_handoff(manager, live_scope(chat), "batch-A", HandoffPointers.model_validate(scoped)) == registered
    plan.write_text(json.dumps({"save_input_history": True, "hold_before_history": True, "hold_after_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await active.wait()).outcome.status == "success"
    first, _, _, second = await claude_inputs(tmp_path, 4)
    backend = chat._run.backend
    record_input = backend.record_installed_input
    checked = asyncio.Event()

    async def observe_first_check():
        await record_input()
        checked.set()

    monkeypatch.setattr(backend, "record_installed_input", observe_first_check)
    token = mint(**asdict(live_scope(chat)))
    api = await start_callback_api(overrides={get_session_manager: lambda: manager}, readiness_token=token)
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http, streamable_http_client(
            f"http://127.0.0.1:{api.port}/mcp/ask_agent/mcp", http_client=http,
        ) as (read, write, _), ClientSession(read, write) as client:
            await client.initialize()
            query = asyncio.create_task(client.call_tool("handoff", {"handoff_id": "batch-A"}))
            await asyncio.wait_for(checked.wait(), 2)
            assert "batch-A: unknown" in (await handoff_state(manager, chat))["message"]
            (tmp_path / "claude-save-input").touch()
            confirmed = await asyncio.wait_for(query, 5)
            assert not confirmed.isError
            assert confirmed.structuredContent["request_id"] == registered["request_id"]
            assert confirmed.structuredContent["state"] == "started"
    finally:
        (tmp_path / "claude-save-input").touch()
        await api.stop()
    assert len(await claude_inputs(tmp_path, 4)) == 4
    scope = live_scope(chat)
    target = ReturnTarget(chat.id, "delegation", "claude-review-b", scope.work_id,
                          derive(capture(chat, scope, prompt="Review B"), "Review B"))
    # An untagged result can carry a contradictory provider ID. It cannot
    # supersede the ID matched to the opening input in the saved history.
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False})
    await wait_dispatch_idle(manager, chat.id)
    assert second["pid"] != first["pid"] and "--resume" not in second["argv"]
    prompt = second["input"]["message"]["content"]
    assert "issues/B.md" in prompt and "stop before release" in prompt and sentinel not in prompt and progress not in prompt
    open_b = presented_request(prompt)
    assert all(text in open_b for text in (batch, next_b, held_operation, "须暂缓的操作"))
    assert current_a not in open_b
    assert chat.claude_session_id == "fresh-claude-B"
    assert "batch-A: started" in (await handoff_state(manager, chat))["message"]

    async def material():
        return ReturnBody("Ticket B review completed.")

    plan.write_text(json.dumps({"save_input_history": True, "hold_after_history": True}))
    callback = await manager.admit_return(target, prepare=material)
    assert isinstance(callback, SubmissionReceipt)
    return_input = (await claude_inputs(tmp_path, 5))[-1]["input"]
    assert open_b == presented_request(return_input["message"]["content"])
    await until(lambda: chat._steer_open)
    next_pointers = POINTERS.model_copy(update={"accepted_ticket": "issues/B.md", "candidate": "fixed-B", "next_work": "issues/C.md"})
    assert (await ticket_handoff(manager, live_scope(chat), "batch-B", next_pointers))["state"] == "registered"
    plan.write_text(json.dumps({"save_input_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": "fresh-claude-B", "subtype": "success", "is_error": False,
                             "user_message_uuid": return_input["uuid"]})
    assert (await callback.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    final = (await claude_inputs(tmp_path, 6))[-1]["input"]["message"]["content"]
    assert "batch-B: started" in (await handoff_state(manager, chat, "batch-B"))["message"]
    assert chat.claude_session_id == "fresh-claude-C" and "issues/C.md" in final
    assert batch in presented_request(final)
    assert all(text not in final for text in (current_a, next_b, held_operation))

    await manager.stop_all_held_processes()
    from server.harness.claude_code import ClaudeDriver

    prepare = ClaudeDriver.prepare
    async def lost_history(driver, io, ctx, argv, kwargs):
        if ctx.resume_id == "fresh-claude-C":
            raise ValueError(f"No conversation found with session ID: {ctx.resume_id}")
        await prepare(driver, io, ctx, argv, kwargs)
    monkeypatch.setattr(ClaudeDriver, "prepare", lost_history)
    continued = await manager.start_message(chat.id, "Continue on C after history loss")
    assert (await continued.wait()).outcome.recovery["stale_session"] == 1
    await wait_dispatch_idle(manager, chat.id)
    recovered = (await claude_inputs(tmp_path, 7))[-1]["input"]["message"]["content"]
    assert "<session-history" in recovered and "issues/C.md" in recovered and sentinel not in recovered
    assert "stop before release" in recovered and progress not in recovered
    assert json.dumps(batch, ensure_ascii=False)[1:-1] in recovered
    assert all(text not in recovered for text in (current_a, next_b, held_operation))
    visible = await database.load_messages(chat.id)
    assert {progress, mixed, sentinel} <= {m["content"] for m in visible if isinstance(m["content"], str)}
    point = next(m["seq"] for m in visible if m.get("content") == "Continue on C after history loss")
    child = await manager.fork_session(chat.id, point, user_id=chat.user_id)
    assert sentinel in [m["content"] for m in await database.load_messages(child.id)]
    assert (await (await manager.start_message(child.id, "Continue the fork")).wait()).outcome.status == "success"
    forked = (await claude_inputs(tmp_path, 8))[-1]["input"]["message"]["content"]
    assert "<fork-history" in forked and "issues/C.md" in forked and sentinel not in forked
    assert "stop before release" in forked and progress not in forked
    assert json.dumps(batch, ensure_ascii=False)[1:-1] in forked
    assert all(text not in forked for text in (current_a, next_b, held_operation))


@pytest.mark.parametrize("control", ["stop", "human", "finish-ticket"])
async def test_concurrent_ids_get_one_handoff_and_human_control_withdraws_it(native_runtime, tmp_path, monkeypatch, control):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    active = await manager.start_message(chat.id, "Finish ticket A")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    with pytest.raises(ValueError, match="Register the authorized ticket batch"):
        await ticket_handoff(manager, scope, "unregistered", POINTERS)
    await begin_ticket_batch(manager, scope)
    results = await asyncio.gather(*(ticket_handoff(manager, scope, key, POINTERS) for key in ["id-one", "id-two"]),
                                   return_exceptions=True)
    accepted, = [r for r in results if isinstance(r, dict)]
    refused, = [r for r in results if isinstance(r, ValueError)]
    key = accepted["handoff_id"]
    assert key in str(refused)
    if control == "stop":
        persist_stop = database.stop_ticket_batch

        async def stop_after_interrupt(session_id, request_id):
            assert manager._session_owner(chat).runtime.current.interrupted
            assert request_id == active.request_id
            await persist_stop(session_id, request_id)

        monkeypatch.setattr(database, "stop_ticket_batch", stop_after_interrupt)
        await manager.interrupt(chat.id)
        assert (await active.wait()).outcome.status == "interrupted"
    else:
        if control != "finish-ticket":
            mode[0] = "normal"
        human_text = "把手上这项验掉，审查过了就到这里。" if control == "finish-ticket" else "Pause ticket B and read my new instructions"
        human = await manager.start_message(chat.id, human_text)
        inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
        assert (await active.wait()).outcome.status == "success"
        if control == "finish-ticket":
            await until(lambda: chat._steer_open and chat._run.settlement.request_id == human.request_id)
            assert (await limit_ticket_batch(manager, live_scope(chat), human_text))["state"] == "finishing"
            inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
                "id": "turn-2", "status": "completed"}}})
        assert (await human.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    status = await handoff_state(manager, chat, key)
    assert status["execution"]["state"] == "withdrawn" and not status["execution"]["recoverable_input"]
    if control == "finish-ticket":
        assert await database.ticket_batch_state(chat.id, active.request_id) == "finishing"
    starts = [f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
    assert len(starts) == (1 if control == "stop" else 2)
    assert all("issues/B.md" not in str(f["params"]["input"]) for f in starts)


async def test_claude_success_without_saved_first_input_leaves_original_handoff_unknown(claude_runtime, tmp_path, monkeypatch):
    manager, _, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "unconfirmed-B"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    original = await manager.start_message(chat.id, "Implement ticket B when A is accepted")
    first, = await claude_inputs(tmp_path, 1)
    await begin_ticket_batch(manager, live_scope(chat))
    await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await original.wait()).outcome.status == "success"
    _, second = await claude_inputs(tmp_path, 2)
    monkeypatch.setattr("server.sessions.handoff._CLAUDE_FIRST_INPUT_WAIT_SECONDS", 0.1)
    observed = await asyncio.wait_for(ticket_handoff(manager, live_scope(chat), "batch-A"), 2)
    assert observed["state"] == "unknown"
    claude_inject(tmp_path, {"type": "result", "session_id": "unconfirmed-B", "subtype": "success", "is_error": False,
                             "user_message_uuid": second["input"]["uuid"]})
    await wait_dispatch_idle(manager, chat.id)
    receipt = await handoff_state(manager, chat)
    assert observed["request_id"] == receipt["request_id"] and "batch-A: unknown" in receipt["message"]
    assert not receipt["execution"]["recoverable_input"] and len(await claude_inputs(tmp_path, 2)) == 2


async def test_retired_unknown_handoff_does_not_block_a_new_batch(claude_runtime, tmp_path, monkeypatch):
    manager, database, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "unconfirmed-B", "new-C", "fresh-D"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    original = await manager.start_message(chat.id, "Implement A and B")
    first, = await claude_inputs(tmp_path, 1)
    await until(lambda: chat.claude_session_id == SID)
    await begin_ticket_batch(manager, live_scope(chat))
    old = await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    plan.write_text(json.dumps({"failure": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await original.wait()).outcome.status == "success"
    await claude_inputs(tmp_path, 2)
    await wait_dispatch_idle(manager, chat.id)
    assert chat._run.backend is None and chat.claude_session_id is None
    assert (await handoff_state(manager, chat))["execution"]["state"] == "unknown"
    assert await database.ticket_batch_state(chat.id, original.request_id) == "registered"

    plan.write_text(json.dumps({"hold": True}))
    new = await manager.start_message(chat.id, "I reviewed the failed A/B batch. Start a separate batch for C and D.")
    _, _, third = await claude_inputs(tmp_path, 3)
    await until(lambda: chat.claude_session_id == "new-C")
    scope = live_scope(chat)
    assert (await begin_ticket_batch(manager, scope))["original_request_id"] == new.request_id
    assert await database.conflicting_handoff(chat.id, new.request_id, new.request_id, SID) == "batch-A"
    pointers = HandoffPointers(spec="new-spec.md", accepted_ticket="issues/C.md", candidate="fixed-C", next_work="issues/D.md")
    assert (await ticket_handoff(manager, scope, "batch-C", pointers))["state"] == "registered"
    assert (await ticket_handoff(manager, scope, "batch-A"))["state"] == "unknown"
    plan.write_text(json.dumps({"save_input_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": "new-C", "subtype": "success", "is_error": False,
                             "user_message_uuid": third["input"]["uuid"]})
    assert (await new.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    past = await handoff_state(manager, chat)
    assert past["request_id"] == old["request_id"] and past["execution"]["state"] == "unknown"
    assert "batch-C: started" in (await handoff_state(manager, chat, "batch-C"))["message"]
    inputs = await claude_inputs(tmp_path, 4)
    assert len(inputs) == 4 and "issues/D.md" in inputs[-1]["input"]["message"]["content"]


async def test_new_codex_batch_cannot_handoff_from_the_old_unknown_target_thread(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement A and B")
    await until(lambda: chat._steer_open)
    await begin_ticket_batch(manager, live_scope(chat))
    old = await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    mode[0] = "start_internal_error"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    stored = await database.get_submission_for_request(chat.id, old["request_id"])
    assert stored["terminal"] is not None and stored["delivery_state"] == "unknown"
    assert not stored["payload"].get("fresh_native_id")
    thread = stored["native_inputs"][-1]["thread_id"]
    assert thread != ROOT and thread == chat.claude_session_id and chat._run.backend is None

    mode[0] = "control_output"
    new = await manager.start_message(chat.id, "Start a separate batch for C and D")
    await until(lambda: chat._steer_open)
    assert [f for f in wire_frames(tmp_path) if f.get("method") == "thread/resume"][-1]["params"]["threadId"] == thread
    scope = live_scope(chat)
    assert (await begin_ticket_batch(manager, scope))["original_request_id"] == new.request_id
    with pytest.raises(ValueError, match="Existing handoff batch-A blocks this decision"):
        await ticket_handoff(manager, scope, "batch-C", POINTERS)
    observed = await ticket_handoff(manager, scope, "batch-A")
    assert observed["request_id"] == old["request_id"] and observed["state"] == "unknown"


@pytest.mark.parametrize("handoff_order", ["before-compact", "after-compact", "no-handoff"])
async def test_manual_compact_preserves_the_batch_and_waits_for_its_active_return(native_runtime, tmp_path, handoff_order):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Complete the A/B batch")
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    review = ReturnTarget(chat.id, "delegation", "review-before-compact", scope.work_id, owned)

    async def material():
        return ReturnBody("Finished review of A")

    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    callback = await manager.admit_return(review, prepare=material)
    assert isinstance(callback, SubmissionReceipt)
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    if handoff_order == "before-compact":
        assert (await ticket_handoff(manager, scope, "batch-A", POINTERS))["state"] == "registered"
    compact = await manager.start_compact(chat.id, client_request_id="keep-batch-compact")
    current = manager._session_owner(chat).runtime.current
    assert current is not None and current.request.request_id == callback.request_id and not current.interrupted
    assert chat._ticket_batch_request_id == original.request_id
    assert await database.handoff_human_executions(chat.id, original.request_id) == [original.request_id]
    assert not (tmp_path / "native-compacts.jsonl").exists()
    if handoff_order == "after-compact":
        assert (await ticket_handoff(manager, scope, "batch-A", POINTERS))["state"] == "registered"

    mode[0] = "compact_ack_first"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-2", "status": "completed"}}})
    assert (await callback.wait()).outcome.status == "success"
    assert (await compact.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    assert chat._ticket_batch_request_id == original.request_id
    assert await database.handoff_human_executions(chat.id, original.request_id) == [original.request_id]
    if handoff_order != "no-handoff":
        assert "batch-A: started" in (await handoff_state(manager, chat))["message"]
        methods = [frame["method"] for frame in wire_frames(tmp_path)
                   if frame.get("method") in {"turn/start", "thread/compact/start"}]
        assert methods == ["turn/start", "turn/start", "thread/compact/start", "turn/start"]
        return

    mode[0] = "normal"
    late = ReturnTarget(chat.id, "delegation", "review-after-compact", review.work_id, owned)
    returned = await manager.admit_return(late, prepare=material)
    assert isinstance(returned, SubmissionReceipt)
    assert (await returned.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    stored = Database(str(copy_product_database(tmp_path, "batch-after-compact")))
    await stored.initialize()
    restored = SessionManager()
    try:
        await restored.initialize(stored, previous_writers_retired=True)
        restored_chat = restored.get_session(chat.id)
        assert restored_chat._ticket_batch_request_id == original.request_id
        await restored.archive_session(chat.id, replace=False)
        await restored.unarchive_session(chat.id, chat.user_id)
        await restored.interrupt(chat.id)
        assert await stored.ticket_batch_state(chat.id, original.request_id) == "stopped"
    finally:
        await restored.close_session_owners()
        await stored.close()


async def test_return_can_retry_if_a_registered_handoff_is_withdrawn(native_runtime, tmp_path):
    manager, _, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Complete ticket A")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    target = ReturnTarget(chat.id, "delegation", "review-a", scope.work_id, owned)
    registered = await ticket_handoff(manager, scope, "batch-A", POINTERS)

    async def material():
        return ReturnBody("The completed review of A")

    pending = await manager.admit_return(target, prepare=material)
    assert isinstance(pending, ReturnRefused) and pending.reason == "busy"
    assert await manager.cancel_request(chat.id, registered["request_id"])
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    returned = await manager.admit_return(target, prepare=material)
    assert isinstance(returned, SubmissionReceipt)
    assert (await returned.wait()).outcome.status == "success"
    assert (await handoff_state(manager, chat))["execution"]["state"] == "withdrawn"
    assert "The completed review of A" in str(wire_frames(tmp_path))


async def test_preparation_failure_cannot_be_mistaken_for_an_uncertain_handoff(claude_runtime, tmp_path, monkeypatch):
    from server.sessions.owner_runtime import OwnerRuntime

    manager, database, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "fresh-B"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    original = await manager.start_message(chat.id, "Complete ticket A")
    first, = await claude_inputs(tmp_path, 1)
    await until(lambda: chat.claude_session_id == SID)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)

    async def refused_preparation(*args, **kwargs):
        raise ValueError("The fixture credential is unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(OwnerRuntime, "_prepare_run", refused_preparation)
        claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                                 "user_message_uuid": first["input"]["uuid"]})
        assert (await original.wait()).outcome.status == "success"
        await wait_dispatch_idle(manager, chat.id)
    status = await handoff_state(manager, chat)
    assert "batch-A: failed" in status["message"]
    assert status["execution"]["delivery_state"] == "none"
    assert (await database.get_committed_terminal(chat.id, status["request_id"]))["status"] == "failed"
    assert chat.claude_session_id == SID
    assert len(await claude_inputs(tmp_path, 1)) == 1

    async def material():
        return ReturnBody("Review A remains available")

    target = ReturnTarget(chat.id, "delegation", "review-after-preparation-failure", scope.work_id, owned)
    review = await manager.admit_return(target, prepare=material)
    assert isinstance(review, SubmissionReceipt)
    _, returned = await claude_inputs(tmp_path, 2)
    assert "Review A remains available" in returned["input"]["message"]["content"]
    assert returned["argv"][returned["argv"].index("--resume") + 1] == SID
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": returned["input"]["uuid"]})
    assert (await review.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)

    resumed = await manager.start_message(chat.id, "The credential is ready. Start the next authorized ticket.")
    second = (await claude_inputs(tmp_path, 3))[-1]
    await until(lambda: chat.claude_session_id == SID)
    await begin_ticket_batch(manager, live_scope(chat))
    assert (await ticket_handoff(manager, live_scope(chat), "batch-B", POINTERS))["state"] == "registered"
    plan.write_text(json.dumps({"save_input_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": second["input"]["uuid"]})
    assert (await resumed.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-B: started" in (await handoff_state(manager, chat, "batch-B"))["message"]
    assert chat.claude_session_id == "fresh-B"


async def test_claude_unsent_handoff_retires_before_running_the_scope_instruction(claude_runtime, tmp_path, monkeypatch):
    from server.harness.claude_code import ClaudeDriver

    manager, _, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-plan.json").write_text(json.dumps({"hold": True}))
    original = await manager.start_message(chat.id, "Complete the ticket batch")
    first, = await claude_inputs(tmp_path, 1)
    await until(lambda: chat.claude_session_id == SID and chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    await ticket_handoff(manager, scope, "batch-A", POINTERS)
    entering, release = asyncio.Event(), asyncio.Event()
    start_input = ClaudeDriver.start_input

    async def hold_before_write(driver, io, context):
        if "[ticket-handoff batch-A]" in context.prompt:
            entering.set()
            await release.wait()
        await start_input(driver, io, context)

    monkeypatch.setattr(ClaudeDriver, "start_input", hold_before_write)
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await original.wait()).outcome.status == "success"
    text = "Accept only the current ticket and stop."
    try:
        await asyncio.wait_for(entering.wait(), 3)
        directive = await manager.start_message(chat.id, text)
        assert "batch-A: withdrawn" in (await handoff_state(manager, chat))["message"]
    finally:
        release.set()
    _, second = await claude_inputs(tmp_path, 2)
    await until(lambda: chat._steer_open and chat._run.settlement.request_id == directive.request_id)
    assert second["argv"][second["argv"].index("--resume") + 1] == SID
    assert (await limit_ticket_batch(manager, live_scope(chat), text))["original_request_id"] == original.request_id
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": second["input"]["uuid"]})
    assert (await directive.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)

    async def material():
        return ReturnBody("Review approved")

    returned = await manager.admit_return(ReturnTarget(chat.id, "delegation", "review-after-withdrawal", scope.work_id, owned),
                                          prepare=material)
    assert isinstance(returned, SubmissionReceipt)
    frames = await claude_inputs(tmp_path, 3)
    assert "Review approved" in frames[-1]["input"]["message"]["content"]
    assert all("issues/B.md" not in frame["input"]["message"]["content"] for frame in frames)
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": frames[-1]["input"]["uuid"]})
    assert (await returned.wait()).outcome.status == "success"


@pytest.mark.parametrize("control", ["finish-ticket", "stop"])
async def test_confirmed_unsent_handoff_keeps_the_original_history(native_runtime, tmp_path, monkeypatch, control):
    from server.sessions.owner_runtime import OwnerRuntime

    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    root = await manager.start_message(chat.id, "Complete the ticket batch")
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review the current ticket"), "Review the current ticket")
    handoff = await ticket_handoff(manager, scope, "batch-A", POINTERS)

    async def material():
        return ReturnBody("Review approved")

    target = ReturnTarget(chat.id, "delegation", "review-a", scope.work_id, owned)
    preparing, release = asyncio.Event(), asyncio.Event()
    prepare = OwnerRuntime._prepare_run

    async def hold_after_recording(runtime, configured, prompt, diagnostic, **kwargs):
        if kwargs["request_id"] == handoff["request_id"]:
            preparing.set()
            await release.wait()
        return await prepare(runtime, configured, prompt, diagnostic, **kwargs)

    monkeypatch.setattr(OwnerRuntime, "_prepare_run", hold_after_recording)
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-1", "status": "completed"}}})
    assert (await root.wait()).outcome.status == "success"
    text = "Complete acceptance of this ticket, then stop the batch."
    try:
        await asyncio.wait_for(preparing.wait(), 5)
        busy = await manager.admit_return(target, prepare=material)
        assert isinstance(busy, ReturnRefused) and busy.reason == "busy"
        if control == "stop":
            assert await manager.interrupt(chat.id)
        else:
            directive = await manager.start_message(chat.id, text)
    finally:
        release.set()
    if control == "stop":
        await wait_dispatch_idle(manager, chat.id)
        assert "batch-A: withdrawn" in (await handoff_state(manager, chat))["message"]
        assert chat.claude_session_id == ROOT
        assert await database.ticket_batch_state(chat.id, root.request_id) == "stopped"
        assert await database.request_in_model_history(chat.id, root.request_id, owned.return_binding.captured_after_seq)
        return
    await until(lambda: chat._steer_open and chat._run.settlement.request_id == directive.request_id)
    assert chat.claude_session_id == ROOT
    assert "batch-A: withdrawn" in (await handoff_state(manager, chat))["message"]
    assert (await limit_ticket_batch(manager, live_scope(chat), text))["original_request_id"] == root.request_id
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-2", "status": "completed"}}})
    assert (await directive.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)

    returned = await manager.admit_return(target, prepare=material)
    assert isinstance(returned, SubmissionReceipt)
    assert (await returned.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    starts = [frame["params"] for frame in wire_frames(tmp_path, finished=True) if frame.get("method") == "turn/start"]
    assert len(starts) == 3 and {start["threadId"] for start in starts} == {ROOT}
    assert all(part in str(starts[-1]["input"]) for part in (text, "Review approved"))
    replay, _, _ = build_lost_history_replay("Continue", [MessageContent(**row) for row in await database.load_messages(chat.id)])
    assert "Complete the ticket batch" in replay and "[ticket-handoff batch-A]" not in replay


async def test_cancel_handoff_restores_history_when_the_writer_confirms_no_send(native_runtime, tmp_path, monkeypatch):
    from server.harness.run import HarnessRun

    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    root = await manager.start_message(chat.id, "Complete the ticket batch")
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    # Preserve the original rollout when the peer prepares another thread.
    (tmp_path / f"{ROOT}.jsonl").write_bytes((tmp_path / "native-history.jsonl").read_bytes())
    handoff = await ticket_handoff(manager, scope, "batch-A", POINTERS)
    waiting, release = asyncio.Event(), asyncio.Event()
    write_input = HarnessRun.write_input

    async def hold_before_send(run, data, **kwargs):
        request_id, _ = run.input_identity
        if request_id == handoff["request_id"]:
            waiting.set()
            await release.wait()
        await write_input(run, data, **kwargs)

    monkeypatch.setattr(HarnessRun, "write_input", hold_before_send)
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-1", "status": "completed"}}})
    assert (await root.wait()).outcome.status == "success"
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        status = await handoff_state(manager, chat)
        assert status["request_id"] == handoff["request_id"] and "batch-A: unknown" in status["message"]
        assert await manager.cancel_request(chat.id, handoff["request_id"])
    finally:
        release.set()
    await wait_dispatch_idle(manager, chat.id)
    status = await handoff_state(manager, chat)
    assert status["request_id"] == handoff["request_id"] and "batch-A: withdrawn" in status["message"]
    assert status["execution"]["native_inputs"][-1]["state"] == "rejected"
    assert status["outcome"]["status"] == "interrupted"
    assert chat.claude_session_id == await database.get_session_resume_id(chat.id) == ROOT
    assert await database.ticket_batch_state(chat.id, root.request_id) == "registered"
    replay, _, _ = build_lost_history_replay("Continue", [MessageContent(**row) for row in await database.load_messages(chat.id)])
    assert "Complete the ticket batch" in replay and "[ticket-handoff batch-A]" not in replay

    async def material():
        return ReturnBody("Review approved")

    mode[0] = "normal"
    target = ReturnTarget(chat.id, "delegation", "review-after-cancelling-handoff", scope.work_id, owned)
    returned = await manager.admit_return(target, prepare=material)
    assert isinstance(returned, SubmissionReceipt)
    assert (await returned.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    starts = [frame["params"] for frame in wire_frames(tmp_path, finished=True) if frame.get("method") == "turn/start"]
    assert len(starts) == 2 and {start["threadId"] for start in starts} == {ROOT}
    assert "Review approved" in str(starts[-1]["input"])


async def test_restart_observes_the_original_handoff_without_replaying_uncertain_work(native_runtime, tmp_path):
    manager, _, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    active = await manager.start_message(chat.id, "Complete the authorized batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    await begin_ticket_batch(manager, live_scope(chat))
    receipt = await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)

    pending = copy_product_database(tmp_path, "registered")
    mode[0] = "stop_late_accepted"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await active.wait()).outcome.status == "success"
    await until(lambda: len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]) == 2)
    uncertain = copy_product_database(tmp_path, "sent")
    await manager.interrupt(chat.id)
    await wait_dispatch_idle(manager, chat.id)
    for path, expected in [(pending, "withdrawn"), (uncertain, "unknown")]:
        database = Database(str(path))
        await database.initialize()
        restored = SessionManager()
        try:
            await restored.initialize(database, previous_writers_retired=True)
            status = await handoff_state(restored, chat)
            assert status["request_id"] == receipt["request_id"]
            assert status["execution"]["state"] == expected and not status["execution"]["recoverable_input"]
            assert restored.dispatch_snapshot(chat.id)["items"] == []
        finally:
            await restored.close_session_owners()
            await database.close()
    assert sum(f.get("method") == "turn/start" for f in wire_frames(tmp_path)) == 2


async def test_claude_handoff_restart_distinguishes_claim_before_write_from_possible_delivery(
    claude_runtime, tmp_path, monkeypatch,
):
    manager, database, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "new-claude-B"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    active = await manager.start_message(chat.id, "Finish the authorized batch")
    first, = await claude_inputs(tmp_path, 1)
    await begin_ticket_batch(manager, live_scope(chat))
    registered = await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    clips = {}
    record = database.record_input_history

    async def capture_write_barrier(session_id, request_id, attempt_id, generation, backend, native_id, may_have_run):
        if request_id == registered["request_id"] and native_id is None and may_have_run:
            clips["withdrawn"] = copy_product_database(tmp_path, "claimed-before-write")
            await record(session_id, request_id, attempt_id, generation, backend, native_id, may_have_run)
            clips["unknown"] = copy_product_database(tmp_path, "possibly-sent")
        else:
            await record(session_id, request_id, attempt_id, generation, backend, native_id, may_have_run)

    monkeypatch.setattr(database, "record_input_history", capture_write_barrier)
    plan.write_text(json.dumps({"save_input_history": True, "hold_after_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await active.wait()).outcome.status == "success"
    await claude_inputs(tmp_path, 2)

    async def received():
        return (await ticket_handoff(manager, live_scope(chat), "batch-A"))["state"] == "started"

    await wait_until(received)
    clips["started"] = copy_product_database(tmp_path, "proven-sent")
    for expected, path in clips.items():
        stored = Database(str(path))
        await stored.initialize()
        restored = SessionManager()
        try:
            await restored.initialize(stored, previous_writers_retired=True)
            status = await handoff_state(restored, chat)
            assert status["request_id"] == registered["request_id"]
            assert f"batch-A: {expected}" in status["message"] and not status["execution"]["recoverable_input"]
            assert restored.dispatch_snapshot(chat.id)["items"] == []
            if expected != "started":
                conflict = await stored.conflicting_handoff(chat.id, active.request_id, "different-return", "different-history")
                assert conflict == ("batch-A" if expected == "unknown" else None)
                assert await stored.request_in_model_history(chat.id, active.request_id, None) == (expected == "withdrawn")
                if expected == "withdrawn":
                    assert restored.get_session(chat.id).claude_session_id == SID
            if expected == "unknown":
                original = await stored.get_submission_for_request(chat.id, registered["request_id"])
                assert original["terminal"] is None
                new_origin = "later-independent-origin"
                await stored.admit_client_submission(chat.id, new_origin, "digest", new_origin, payload={
                    "source": "human", "operation": {"kind": "message", "content": "C and D", "attachment_ids": []},
                }, receipt={"accepted": True, "disposition": "accepted"})
                assert await stored.register_ticket_batch(chat.id, new_origin)
                assert await stored.conflicting_handoff(chat.id, new_origin, new_origin, "different-history") == "batch-A"
        finally:
            await restored.close_session_owners()
            await stored.close()
    assert len(await claude_inputs(tmp_path, 2)) == 2


async def test_confirmed_steer_survives_handoff_and_late_background_output_stays_at_its_source(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    await active_root(native_runtime, tmp_path)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    background = BgTaskManager()
    release = asyncio.Event()

    async def return_after_boundary(record):
        await release.wait()
        return await manager.deliver_bg_result(record)

    background.bind(database, return_after_boundary, manager._broadcast)
    await background.start()
    try:
        material = "A-background-material"
        job = await background.start_task(session_id=chat.id, working_dir=str(tmp_path), command=f"echo {material}",
            scope=scope, owned_context=derive(capture(chat, scope, prompt="Read earlier evidence"), "Read evidence"))
        registered = await ticket_handoff(manager, scope, "before-steer", POINTERS)
        assert registered["state"] == "registered"
        correction = "Only handle ticket B. Leave deployment paused."
        accepted = await submit_client_message(manager, chat.id, correction, [], "narrow", wiki_mode=None)
        assert accepted["disposition"] == "steered"
        async def confirmed():
            observed = await submission_status(manager, chat.id, "narrow")
            return observed["execution"]["delivery_state"] == "confirmed"
        await wait_until(confirmed)
        withdrawn = await ticket_handoff(manager, live_scope(chat), "before-steer")
        assert withdrawn["request_id"] == registered["request_id"] and withdrawn["state"] == "withdrawn"
        assert await ticket_handoff(manager, live_scope(chat), "before-steer", POINTERS) == withdrawn
        pointers = with_human_scope(POINTERS, {correction: (("batch", correction),)})
        assert (await ticket_handoff(manager, live_scope(chat), "batch-A", pointers))["state"] == "registered"
        mode[0] = "normal"
        (tmp_path / "native-finish").touch()
        await wait_dispatch_idle(manager, chat.id)
        assert "batch-A: started" in (await handoff_state(manager, chat))["message"]
        assert "before-steer: withdrawn" in (await handoff_state(manager, chat, "before-steer"))["message"]
        release.set()
        async def retained():
            record = await background.get_task(job.id)
            return record and record.delivery_disposition == "stopped"
        await wait_until(retained)
        kept = await background.get_task(job.id)
        assert kept.exit_code == 0 and kept.stdout.strip() == material
        starts = [f["params"] for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
        assert len(starts) == 2 and correction in str(starts[-1]["input"]) and material not in str(starts[-1]["input"])
    finally:
        release.set()
        await background.shutdown()


async def test_late_wiki_job_stays_in_its_card_without_opening_a_turn_in_the_new_history(native_runtime, tmp_path, monkeypatch):
    from server.sessions.owner_runtime import OwnerRuntime

    manager, database, chat, mode, _, _ = native_runtime
    vault = tmp_path / "vault"
    vault.mkdir()
    registry.configure(chat.user_id, vault, chat.agent_id)
    jobs = WikiJobs(chat.user_id)
    path = "Library/old-ticket.md"
    WikiHistory(jobs.store).write(path, b"# Old ticket\nRetained material.\n", None, "human", "Create input")
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement the authorized batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    await begin_ticket_batch(manager, live_scope(chat))
    job = jobs.maintain("old-ticket", "", "Review old evidence", [path], None, [], "human",
                        requester=job_events.Requester(chat.user_id, "chat", chat.id, original.request_id))
    claimed = asyncio.Event()
    release = asyncio.Event()
    prepare_input = OwnerRuntime._prepare_input

    async def hold_before_boundary(runtime, configured, request, execution):
        if request.source == "handoff":
            claimed.set()
            await release.wait()
        return await prepare_input(runtime, configured, request, execution)

    monkeypatch.setattr(OwnerRuntime, "_prepare_input", hold_before_boundary)
    assert (await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS))["state"] == "registered"
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await asyncio.wait_for(claimed.wait(), 5)
    jobs.update(job["id"], status="completed", items=[item | {"status": "done"} for item in job["items"]])
    try:
        await job_notifications.dispatch(jobs, manager)
        with jobs.store.connect() as db:
            pending, = job_events.pending(db)
            assert pending["job_id"] == job["id"] and json.loads(pending["body"])["status"] == "completed"
    finally:
        release.set()
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-A: started" in (await handoff_state(manager, chat))["message"]

    await job_notifications.dispatch(jobs, manager)
    await wait_dispatch_idle(manager, chat.id)
    with jobs.store.connect() as db:
        assert not job_events.pending(db)
    cards = await database.wiki_job_cards(chat.id, chat.user_id)
    assert [(card["job_id"], card["status"]) for card in cards] == [(job["id"], "completed")]
    assert sum(f.get("method") == "turn/start" for f in wire_frames(tmp_path, finished=True)) == 2


@pytest.mark.parametrize("scope_update", [
    "none", "replacement", "replacement_reopened", "steers", "stop_restart", "restart_stop", "reopen_stop",
])
async def test_review_callback_can_continue_only_with_the_current_human_scope(native_runtime, tmp_path, scope_update):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement the ticket batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    updates = ["Keep the release paused.", "Run only the checks for B."]

    async def steer(text, client_id):
        await until(lambda: chat._steer_open)
        receipt = await submit_client_message(manager, chat.id, text, [], client_id, wiki_mode=None)
        assert receipt["disposition"] == "steered"

        async def confirmed():
            observed = await submission_status(manager, chat.id, client_id)
            return observed["execution"]["delivery_state"] == "confirmed"

        await wait_until(confirmed)

    if scope_update == "steers":
        await steer(updates[0], "first-change")
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    replaced = scope_update in {"replacement", "replacement_reopened"}
    if replaced:
        changed = await manager.start_message(chat.id, "Pause implementation and discuss the plan first")
        assert (await changed.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    if scope_update == "replacement_reopened":
        await manager.archive_session(chat.id, replace=False)
        await manager.unarchive_session(chat.id, chat.user_id)
        await manager.interrupt(chat.id)
        assert await database.ticket_batch_state(chat.id, original.request_id) == "registered"

    async def material():
        return ReturnBody("Earlier review of ticket A")

    target = ReturnTarget(chat.id, "delegation", "old-review", scope.work_id, owned)
    if scope_update in {"stop_restart", "restart_stop", "reopen_stop"}:
        if scope_update == "stop_restart":
            await manager.interrupt(chat.id)
        path = copy_product_database(tmp_path, "stopped-batch")
        stored = Database(str(path))
        await stored.initialize()
        restored = SessionManager()
        try:
            await restored.initialize(stored, previous_writers_retired=True)
            if scope_update == "reopen_stop":
                await restored.archive_session(chat.id, replace=False)
                await restored.unarchive_session(chat.id, chat.user_id)
            if scope_update != "stop_restart":
                await restored.interrupt(chat.id)
            assert await stored.ticket_batch_state(chat.id, original.request_id) == "stopped"
            stale = await restored.admit_return(target, prepare=material)
            assert isinstance(stale, ReturnRefused) and stale.reason == "admission_closed"
            assert await restored.source_receipt("delegation", "old-review", session_id=chat.id) is None
        finally:
            await restored.close_session_owners()
            await stored.close()
        return

    mode[0] = "control_output"
    callback = await manager.admit_return(target, prepare=material)
    if replaced:
        assert isinstance(callback, ReturnRefused) and callback.reason == "admission_closed"
        assert await manager.source_receipt("delegation", "old-review", session_id=chat.id) is None
        assert len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]) == 2
        return
    assert isinstance(callback, SubmissionReceipt)
    await until(lambda: len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]) == 2)
    pointers = POINTERS
    if scope_update == "steers":
        starts = [f["params"] for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
        assert updates[0] in str(starts[1]["input"]) and updates[1] not in str(starts[1]["input"])
        await steer(updates[1], "second-change")
        pointers = with_human_scope(POINTERS, {
            updates[0]: (("batch", updates[0]),), updates[1]: (("next", updates[1]),),
        })
    assert (await ticket_handoff(manager, live_scope(chat), "batch-A", pointers))["state"] == "registered"
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-2", "status": "completed"}}})
    assert (await callback.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    receipt = await handoff_state(manager, chat)
    assert "batch-A: started" in receipt["message"]
    starts = [f["params"] for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
    assert len(starts) == 3
    new_input = str(starts[-1]["input"])
    assert "issues/B.md" in new_input
    if scope_update == "steers":
        assert all(update in new_input for update in updates)
        assert new_input.index(updates[0]) < new_input.index(updates[1])


@pytest.mark.parametrize("control", ["human", "stop", "finish-ticket", "finish-ticket-during-prepare", "finish-ticket-before-write"])
async def test_control_after_review_admission_qualifies_its_unwritten_input(native_runtime, tmp_path, monkeypatch, control):
    from server.harness.codex_app_server import AppServerDriver
    from server.sessions.owner_runtime import OwnerRuntime

    manager, database, chat, mode, contexts, _ = native_runtime
    finishing = control.startswith("finish-ticket")
    if finishing:
        vault = tmp_path / "vault"
        vault.mkdir()
        registry.configure(chat.user_id, vault, chat.agent_id)
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement the ticket batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)

    async def material():
        return ReturnBody("Earlier review of ticket A")

    preparing = asyncio.Event()
    release = asyncio.Event()
    prepare_input = OwnerRuntime._prepare_input

    async def hold_before_input(runtime, configured, request, execution):
        if request.batch_return:
            preparing.set()
            await release.wait()
        return await prepare_input(runtime, configured, request, execution)

    prepare_run = OwnerRuntime._prepare_run

    async def hold_during_prepare(runtime, configured, prompt, diagnostic, **kwargs):
        if runtime.current.request.batch_return:
            preparing.set()
            await release.wait()
        return await prepare_run(runtime, configured, prompt, diagnostic, **kwargs)

    record_input = AppServerDriver._record

    async def hold_before_native_write(driver, **changes):
        execution = manager._session_owner(chat).runtime.current
        if changes.get("state") == "unknown" and execution is not None and execution.request.batch_return:
            preparing.set()
            await release.wait()
        await record_input(driver, **changes)

    if control == "finish-ticket-during-prepare":
        monkeypatch.setattr(OwnerRuntime, "_prepare_run", hold_during_prepare)
    elif control == "finish-ticket-before-write":
        monkeypatch.setattr(AppServerDriver, "_record", hold_before_native_write)
    else:
        monkeypatch.setattr(OwnerRuntime, "_prepare_input", hold_before_input)
    callback = await manager.admit_return(ReturnTarget(chat.id, "delegation", "old-review", scope.work_id, owned),
                                          prepare=material)
    assert isinstance(callback, SubmissionReceipt)
    human = None
    text = "/wiki Finish this review first, then leave the rest of the batch for later."
    try:
        await asyncio.wait_for(preparing.wait(), 5)
        if control == "stop":
            assert await manager.interrupt(chat.id)
        else:
            if finishing:
                mode[0] = "control_output"
            human = await manager.start_message(
                chat.id, text if finishing else "Pause implementation", wiki_mode="read" if finishing else None,
            )
    finally:
        release.set()
    if finishing:
        await until(lambda: chat._steer_open and chat._run.settlement.request_id == human.request_id)
        starts = [frame["params"] for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"]
        assert len(starts) == 2 and text.removeprefix("/wiki ") in str(starts[1]["input"])
        instruction = scope_instruction_from(starts[1]["input"][0]["text"])
        assert instruction == text
        await limit_ticket_batch(manager, live_scope(chat), instruction)
        mode[0] = "normal"
        inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
            "id": chat._run.backend.active_turn.turn_id, "status": "completed"}}})
        assert (await callback.wait()).outcome.status == "success"
    elif control == "human":
        assert (await callback.wait()).state == "withdrawn"
    else:
        assert (await callback.wait()).outcome.status == "interrupted"
    if human is not None:
        assert (await human.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    starts = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"]
    if finishing:
        assert len(starts) == 3
        assert text.removeprefix("/wiki ") in str(starts[1]["input"])
        prompt = str(starts[2]["input"])
        assert all(part in prompt for part in (text, "Earlier review of ticket A", "acceptance of the current ticket"))
        row = await database.get_submission_for_request(chat.id, callback.request_id)
        knowledge = decode_context(row["payload"]["knowledge"])
        assert knowledge.mode == "read" and not knowledge.maintenance.may_submit
        assert knowledge.sources == ("wiki", "web")
        assert "wiki" in {server.key for server in contexts[-1].mcp_servers}
        inputs = [message for message in await database.load_messages(chat.id)
                  if (message["tool_input"] or {}).get("request_context", {}).get("request_id") == callback.request_id]
        assert len(inputs) == 1
        assert inputs[0]["tool_input"]["request_context"]["knowledge"] == json.loads(row["payload"]["knowledge"])
    else:
        assert len(starts) == (1 if control == "stop" else 2)
        assert "Earlier review of ticket A" not in str(starts)


async def test_scope_instruction_waits_for_a_committed_review_input(native_runtime, tmp_path, monkeypatch):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    root = await manager.start_message(chat.id, "Complete the authorized batch")
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")

    async def complete_current(receipt):
        turn = chat._run.backend.active_turn
        inject(tmp_path, {"method": "turn/completed", "params": {"threadId": turn.thread_id, "turn": {
            "id": turn.turn_id, "status": "completed"}}})
        assert (await receipt.wait()).outcome.status == "success"
        await wait_dispatch_idle(manager, chat.id)

    await complete_current(root)

    async def material():
        return ReturnBody("Review approved")

    written, release = asyncio.Event(), asyncio.Event()
    record = database.record_native_input

    async def hold_delivery_marker(session_id, request_id, evidence, **kwargs):
        if evidence.kind == "start" and evidence.state == "unknown" and not written.is_set():
            written.set()
            await release.wait()
        await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", hold_delivery_marker)
    target = ReturnTarget(chat.id, "delegation", "committed-review", scope.work_id, owned)
    review = await manager.admit_return(target, prepare=material)
    assert isinstance(review, SubmissionReceipt)
    text = "Accept this item and stop the batch."
    try:
        await asyncio.wait_for(written.wait(), 3)
        assert any(
            (message["tool_input"] or {}).get("request_context", {}).get("request_id") == review.request_id
            for message in await database.load_messages(chat.id)
        )
        directive = await manager.start_message(chat.id, text)
    finally:
        release.set()
    await until(lambda: chat._steer_open and chat._run.settlement.request_id == review.request_id)
    turn = chat._run.backend.active_turn
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": turn.thread_id, "turn": {
        "id": turn.turn_id, "status": "completed"}}})
    assert (await review.wait()).outcome.status == "success"
    await until(lambda: chat._steer_open and chat._run.settlement.request_id == directive.request_id)
    limited = await limit_ticket_batch(manager, live_scope(chat), text)
    assert limited["original_request_id"] == root.request_id
    await complete_current(directive)
    starts = [frame["params"] for frame in wire_frames(tmp_path, finished=True) if frame.get("method") == "turn/start"]
    assert len(starts) == 3
    assert "Review approved" in str(starts[1]["input"]) and text in str(starts[2]["input"])
    session_id, saved = await manager.source_receipt("delegation", "committed-review", session_id=chat.id)
    assert session_id == chat.id and saved.request_id == review.request_id


@pytest.mark.parametrize("close", ["accepted", "accepted-after-supplement", "stop", "replacement", "stop-before-classification"])
async def test_finish_after_acceptance_keeps_the_current_ticket_review(native_runtime, tmp_path, close):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement the ticket batch")
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")

    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    text = "验收好后停止，不启动06票"
    mode[0] = "control_output"
    directive = await submit_client_message(manager, chat.id, text, [], "finish-after-acceptance", wiki_mode=None)
    assert directive["accepted"] and directive["disposition"] == "accepted"
    await until(lambda: chat._steer_open)

    async def material():
        return ReturnBody("Vera approved the current ticket")

    target = ReturnTarget(chat.id, "delegation", "review-a", scope.work_id, owned)
    busy = await manager.admit_return(target, prepare=material)
    assert isinstance(busy, ReturnRefused) and busy.reason == "busy"
    assert await manager.source_receipt("delegation", "review-a", session_id=chat.id) is None
    if close == "stop-before-classification":
        await manager.interrupt(chat.id)
        await wait_dispatch_idle(manager, chat.id)
        assert await database.ticket_batch_state(chat.id, original.request_id) == "stopped"
        refused = await manager.admit_return(target, prepare=material)
        assert isinstance(refused, ReturnRefused) and refused.reason == "admission_closed"
        return
    with pytest.raises(ValueError, match="match the current human input verbatim"):
        await limit_ticket_batch(manager, live_scope(chat), "Different instruction")
    supplement = "Please record the review result in the current ticket."
    if close == "accepted-after-supplement":
        inline = await submit_client_message(manager, chat.id, supplement, [], "acceptance-note", wiki_mode=None)
        assert inline["disposition"] == "steered"

        async def confirmed():
            observed = await submission_status(manager, chat.id, "acceptance-note")
            return observed["execution"]["delivery_state"] == "confirmed"

        await wait_until(confirmed)
    pre_adoption = derive(capture(chat, live_scope(chat), prompt="Work before scope decision"), "Work before scope decision")
    limited = await call_batch_tool(manager, chat, "limit_ticket_batch", {"message": text})
    assert limited["state"] == "finishing" and limited["original_request_id"] == original.request_id
    root = await database.get_submission_for_request(chat.id, original.request_id)
    assert root["payload"]["ticket_batch_limit_request_id"] == directive["request_id"]
    if close == "accepted":
        path = copy_product_database(tmp_path, "legacy-acceptance")
        with closing(sqlite3.connect(path)) as reader, reader:
            reader.execute("UPDATE client_submissions SET native_inputs=(SELECT json_group_array(json_remove(value,'$.kind')) "
                           "FROM json_each(native_inputs)),payload=json_remove(payload,'$.finish_ticket_batch_instruction_id') "
                           "WHERE request_id=?", (directive["request_id"],))
            verify_main(reader)
    assert (await limit_ticket_batch(manager, live_scope(chat), text)) == limited
    assert capture(chat, live_scope(chat), prompt="Work after scope decision").context.origin.request_id == original.request_id
    stale = await manager.admit_return(ReturnTarget(chat.id, "delegation", "unbound-review", scope.work_id, pre_adoption),
                                       prepare=material)
    assert isinstance(stale, ReturnRefused) and stale.reason == "admission_closed"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-2", "status": "completed"}}})
    await wait_dispatch_idle(manager, chat.id)
    assert await database.ticket_batch_state(chat.id, original.request_id) == "finishing"

    if close == "stop":
        await manager.interrupt(chat.id)
    if close == "replacement":
        mode[0] = "normal"
        unrelated = await manager.start_message(chat.id, "Discuss what 验收后停止 means before continuing")
        assert (await unrelated.wait()).outcome.status == "success"
        await wait_dispatch_idle(manager, chat.id)

    mode[0] = "control_output"
    review = await manager.admit_return(target, prepare=material)
    if close in {"stop", "replacement"}:
        assert isinstance(review, ReturnRefused) and review.reason == "admission_closed"
        assert await manager.source_receipt("delegation", "review-a", session_id=chat.id) is None
        return
    assert isinstance(review, SubmissionReceipt)
    await until(lambda: chat._steer_open)
    starts = [frame["params"] for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"]
    assert len(starts) == 3 and {start["threadId"] for start in starts} == {ROOT}
    assert text in str(starts[1]["input"])
    assert "Vera approved the current ticket" in str(starts[2]["input"])
    assert "acceptance of the current ticket" in str(starts[2]["input"])
    if close == "accepted-after-supplement":
        assert supplement in str(starts[2]["input"])
    scope = live_scope(chat)
    with pytest.raises(ValueError, match="limited this batch to acceptance"):
        await ticket_handoff(manager, scope, "batch-A", POINTERS)
    expected = {"state": "stopped", "original_request_id": original.request_id}
    assert await call_batch_tool(manager, chat, "finish_ticket_batch", {}) == expected
    assert await finish_ticket_batch(manager, scope) == expected
    assert await database.ticket_batch_state(chat.id, original.request_id) == "stopped"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-3", "status": "completed"}}})
    assert (await review.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    late = await manager.admit_return(ReturnTarget(chat.id, "delegation", "late-review", scope.work_id, owned), prepare=material)
    assert isinstance(late, ReturnRefused) and late.reason == "admission_closed"
    repeated = await submit_client_message(manager, chat.id, text, [], "finish-after-acceptance", wiki_mode=None)
    assert repeated["request_id"] == directive["request_id"] and repeated["outcome"]["status"] == "success"
    assert len([frame for frame in wire_frames(tmp_path, finished=True) if frame.get("method") == "turn/start"]) == 3


async def test_confirmed_inline_instruction_can_limit_the_current_batch(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    original = await active_root(native_runtime, tmp_path)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    text = "只把目前这张校验过就好，其他先搁着。"
    incoming = await submit_client_message(manager, chat.id, text, [], "close-current", wiki_mode=None)
    assert incoming["disposition"] == "steered"

    async def confirmed():
        observed = await submission_status(manager, chat.id, "close-current")
        return observed["execution"]["delivery_state"] == "confirmed"

    await wait_until(confirmed)
    assert (await limit_ticket_batch(manager, scope, text))["original_request_id"] == original["request_id"]
    root = await database.get_submission_for_request(chat.id, original["request_id"])
    assert root["payload"]["ticket_batch_limit_request_id"] == incoming["request_id"]
    with pytest.raises(ValueError, match="limited this batch to acceptance"):
        await ticket_handoff(manager, scope, "batch-A", POINTERS)
    mode[0] = "normal"
    (tmp_path / "native-finish").touch()
    await wait_dispatch_idle(manager, chat.id)
    assert (await submission_status(manager, chat.id, "root"))["outcome"]["status"] == "success"
    invalid = copy_product_database(tmp_path, "acceptance-without-confirmed-steer-context")
    with closing(sqlite3.connect(invalid)) as reader, reader:
        reader.execute("UPDATE messages SET tool_input=json_remove(tool_input,'$.request_context.knowledge') "
                       "WHERE json_extract(tool_input,'$.request_context.request_id')=?", (incoming["request_id"],))
        with pytest.raises(RuntimeError, match="invalid ticket acceptance limit"):
            verify_main(reader)


async def test_claude_acceptance_limit_survives_restart_and_archive(claude_runtime, tmp_path, monkeypatch):
    from server.db.portability import read_account

    manager, database, chat, starts, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    vault = tmp_path / "vault"
    vault.mkdir()
    registry.configure(chat.user_id, vault, chat.agent_id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"hold": True}))
    root = await manager.start_message(chat.id, "Complete the authorized tickets")
    first, = await claude_inputs(tmp_path, 1)
    await until(lambda: chat.claude_session_id == SID and chat._steer_open)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review the current ticket"), "Review the current ticket")
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await root.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    text = "/wiki Review only the current ticket, then stop."
    directive = await manager.start_message(chat.id, text, wiki_mode="read")
    _, second = await claude_inputs(tmp_path, 2)
    await until(lambda: chat._steer_open and chat._run.settlement.request_id == directive.request_id)
    instruction = scope_instruction_from(second["input"]["message"]["content"])
    assert instruction == text
    assert (await limit_ticket_batch(manager, live_scope(chat), instruction))["original_request_id"] == root.request_id
    amended = await database.get_submission_for_request(chat.id, directive.request_id)
    assert amended["delivery_state"] == "none" and amended["terminal"] is None
    path = copy_product_database(tmp_path, "acceptance-before-terminal")
    invalid = copy_product_database(tmp_path, "acceptance-without-human-instruction")
    with closing(sqlite3.connect(path)) as reader:
        verify_main(reader)
    with closing(sqlite3.connect(invalid)) as reader, reader:
        reader.execute("UPDATE client_submissions SET payload=json_remove(payload,'$.ticket_batch_limit_request_id') "
                       "WHERE request_id=?", (root.request_id,))
        with pytest.raises(RuntimeError, match="invalid ticket acceptance limit"):
            verify_main(reader)
        with pytest.raises(RuntimeError, match="invalid ticket acceptance limit"):
            read_account(reader, chat.user_id, key="")
    invalid_database = Database(str(invalid))
    await invalid_database.initialize()
    try:
        with pytest.raises(RuntimeError, match="invalid ticket acceptance limit"):
            await SessionManager().initialize(invalid_database, previous_writers_retired=True)
    finally:
        await invalid_database.close()
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": second["input"]["uuid"]})
    assert (await directive.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    await manager.stop_all_held_processes()

    async def material():
        return ReturnBody("Review approved")

    stored = Database(str(path))
    await stored.initialize()
    restored = SessionManager()
    try:
        await restored.initialize(stored, previous_writers_retired=True)
        assert await stored.ticket_batch_state(chat.id, root.request_id) == "finishing"
        await restored.archive_session(chat.id, replace=False)
        target = ReturnTarget(chat.id, "delegation", "review-after-restart", scope.work_id, owned)
        returned = await restored.admit_return(target, prepare=material)
        assert isinstance(returned, SubmissionReceipt)
        third = (await claude_inputs(tmp_path, 3))[-1]
        assert "wiki" in {server.key for server in starts[-1].mcp_servers}
        assert "acceptance of the current ticket" in third["input"]["message"]["content"]
        assert text in third["input"]["message"]["content"]
        resumed = restored.get_session(chat.id)
        await until(lambda: resumed._steer_open)
        context = capture(resumed, live_scope(resumed), prompt="Record the review").context
        assert context.mode == "read" and not context.maintenance.may_submit and context.sources == ("wiki", "web")
        with pytest.raises(ValueError, match="limited this batch to acceptance"):
            await ticket_handoff(restored, live_scope(resumed), "next-ticket", POINTERS)
        ask_agent = next(server for server in starts[-1].mcp_servers if server.key == "ask_agent")
        assert (await call_batch_tool(restored, resumed, "finish_ticket_batch", {}, token=ask_agent.credential))["state"] == "stopped"
        claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                                 "user_message_uuid": third["input"]["uuid"]})
        assert (await returned.wait()).outcome.status == "success"
        await wait_dispatch_idle(restored, chat.id)
        late = ReturnTarget(chat.id, "delegation", "late-review", scope.work_id, owned)
        assert (await restored.admit_return(late, prepare=material)).reason == "admission_closed"
        assert len(await claude_inputs(tmp_path, 3)) == 3
    finally:
        await restored.stop_all_held_processes()
        await stored.close()


async def test_ticket_acceptance_rejects_unproven_adoption_of_a_later_request(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"

    async def finish_current(receipt):
        turn = chat._run.backend.active_turn
        inject(tmp_path, {"method": "turn/completed", "params": {"threadId": turn.thread_id, "turn": {
            "id": turn.turn_id, "status": "completed"}}})
        assert (await receipt.wait()).outcome.status == "success"
        await wait_dispatch_idle(manager, chat.id)

    root = await manager.start_message(chat.id, "Implement the authorized tickets")
    await until(lambda: chat._steer_open)
    await begin_ticket_batch(manager, live_scope(chat))
    await finish_current(root)
    text = "Accept this ticket, then stop the batch."
    directive = await manager.start_message(chat.id, text)
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    await limit_ticket_batch(manager, scope, text)
    await finish_current(directive)

    carrier = await manager.start_message(chat.id, "Record the current acceptance evidence.")
    await until(lambda: chat._steer_open)
    inline = await submit_client_message(manager, chat.id, text, [], "confirmed-acceptance-instruction", wiki_mode=None)
    assert inline["disposition"] == "steered"

    async def delivered():
        observed = await submission_status(manager, chat.id, "confirmed-acceptance-instruction")
        return observed["execution"]["delivery_state"] == "confirmed"

    await wait_until(delivered)
    scope = live_scope(chat)
    await limit_ticket_batch(manager, scope, text)
    batch = await database.get_submission_for_request(chat.id, root.request_id)
    adopted = await database.get_submission_for_request(chat.id, carrier.request_id)
    assert batch["payload"]["ticket_batch_limit_request_id"] == inline["request_id"]
    assert adopted["payload"]["finish_ticket_batch_instruction_id"] == inline["request_id"]
    await finish_ticket_batch(manager, scope)
    await finish_current(carrier)

    later = await manager.start_message(chat.id, "Independently summarize the completed review.")
    await until(lambda: chat._steer_open)
    scope = live_scope(chat)
    owned = derive(capture(chat, scope, prompt="Read the report"), "Read the report")
    assert owned.return_binding.context.origin.request_id == later.request_id
    await finish_current(later)
    await manager.stop_all_held_processes()
    valid = copy_product_database(tmp_path, "independent-after-acceptance")
    with closing(sqlite3.connect(valid)) as reader:
        verify_main(reader)
    for label, batch_id in (("orphan-adoption", "absent-batch"), ("late-adoption", root.request_id)):
        damaged = copy_product_database(tmp_path, label)
        with closing(sqlite3.connect(damaged)) as reader, reader:
            reader.execute("UPDATE client_submissions SET payload=json_set(payload,'$.ticket_batch_candidate_for',?, "
                           "'$.finish_ticket_batch_for',?) WHERE request_id=?", (batch_id, batch_id, later.request_id))
            with pytest.raises(RuntimeError, match="invalid ticket acceptance limit"):
                verify_main(reader)

    async def material():
        return ReturnBody("Report checked")

    stored = Database(str(valid))
    await stored.initialize()
    restored = SessionManager()
    try:
        await restored.initialize(stored, previous_writers_retired=True)
        mode[0] = "normal"
        target = ReturnTarget(chat.id, "delegation", "independent-review-return", scope.work_id, owned)
        returned = await restored.admit_return(target, prepare=material)
        assert isinstance(returned, SubmissionReceipt)
        assert (await returned.wait()).outcome.status == "success"
    finally:
        await restored.close_session_owners()
        await stored.close()


async def test_a_queued_scope_instruction_cannot_reopen_a_stopped_batch(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    original = await active_root(native_runtime, tmp_path)
    await begin_ticket_batch(manager, live_scope(chat))
    text = "Once this review is in, wrap up the current item and leave the rest."
    directive = await manager.start_message(chat.id, text)
    assert await manager.interrupt(chat.id)
    await until(lambda: manager.dispatch_snapshot(chat.id)["active_request_id"] is None)
    assert await database.ticket_batch_state(chat.id, original["request_id"]) == "stopped"

    mode[0] = "control_output"
    assert await manager.resume_dispatch(chat.id)
    await until(lambda: chat._steer_open and chat._run.settlement.request_id == directive.request_id)
    with pytest.raises(ValueError, match="original ticket batch is no longer current"):
        await limit_ticket_batch(manager, live_scope(chat), text)
    row = await database.get_submission_for_request(chat.id, directive.request_id)
    assert "finish_ticket_batch_for" not in row["payload"]
    await manager.interrupt(chat.id)
