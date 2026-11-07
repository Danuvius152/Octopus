"""Deterministic stdio peer for public Codex submission tests (no provider)."""

import json
import os
import runpy
import select
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Peers may be embedded with runpy from a different script directory.
read_frames = runpy.run_path(str(Path(__file__).with_name("jsonl_frames.py")))["read_frames"]

root, mode = Path(sys.argv[1]), sys.argv[2]
history = root / "native-history.jsonl"
metrics_enabled = (root / "native-metrics-enabled").exists()
metrics_root = root / "codex-home" / "sessions"
if metrics_enabled:
    metrics_root.mkdir(parents=True, exist_ok=True)
    history = metrics_root / "rollout-exact-native-thread.jsonl"
business = root / "native-business.jsonl"
thread_id = json.loads(history.read_text().splitlines()[0])["payload"]["id"] if history.exists() else "exact-native-thread"
delayed = None
late_steer = None
gated_steer = None
created_fork = None
pending_clean = False
defaults_path = root / "native-defaults.json"
defaults = json.loads(defaults_path.read_text()) if defaults_path.exists() else {"model": "fixture", "effort": "low"}
selected_path = root / "native-selected.json"


def metric_record(thread, record_type, payload):
    path = metrics_root / f"rollout-{thread}.jsonl"
    if not path.exists():
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    with path.open("a") as handle:
        handle.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(),
                                "type": record_type, "payload": payload}) + "\n")


def send(frame):
    if "__rollout" in frame:
        record = frame["__rollout"]
        metric_record(record["thread"], record["type"], record["payload"])
        return
    if metrics_enabled:
        method, params = frame.get("method"), frame.get("params") or {}
        if method in {"turn/started", "turn/completed"}:
            metric_record(params["threadId"], "event_msg", {
                "type": "task_started" if method == "turn/started" else "task_complete",
                "turn_id": params["turn"]["id"], "thread_id": params["threadId"]})
        elif method == "item/completed" and params["item"].get("type") == "agentMessage":
            metric_record(params["threadId"], "response_item", {"type": "message", "role": "assistant"})
    if "__stderr" in frame:
        print(frame["__stderr"], file=sys.stderr, flush=True)
        return
    if "__raw_stdout" in frame:
        print(frame["__raw_stdout"], flush=True)
        return
    print(json.dumps(frame), flush=True)


def background_completed():
    send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
        "item": {"id": "background-command", "type": "commandExecution", "status": "failed",
            "command": "pytest fixture", "cwd": str(root), "commandActions": [], "processId": "45196", "aggregatedOutput": "1 passed, 1 teardown error",
            "exitCode": -1}}})


def frames():
    global gated_steer, pending_clean
    buffered = b""
    offset = root / "native-injection-offset"
    injected = int(offset.read_text()) if offset.exists() and (root / "native-injections.jsonl").exists() else 0

    def deliver_injections(*, finished=False):
        nonlocal injected
        values = read_frames(root / "native-injections.jsonl", finished=finished)
        for value in values[injected:]:
            send(value)
        injected = len(values)
        offset.write_text(str(injected))

    while True:
        if pending_clean and (root / "native-clean-release").exists():
            pending_clean = False
            background_completed()
        deliver_injections()
        if gated_steer is not None and (root / "native-release-ack").exists():
            identifier, target = gated_steer
            gated_steer = None
            send({"id": identifier, "result": {"turnId": target}})
        if mode.startswith("steer_") and (root / "native-finish").exists():
            (root / "native-finish").unlink()
            send({"method": "turn/completed", "params": {"threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed"}}})
        if b"\n" not in buffered:
            if not select.select([sys.stdin], [], [], .01)[0]:
                continue
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                # Closing the controlling input also ends the injection writer.
                # A live snapshot's suffix is legal; a final suffix is damage.
                deliver_injections(finished=True)
                if created_fork is not None and mode.startswith("fork_untrusted_"):
                    identifier, cwd = created_fork
                    (Path(cwd) / "unexpected-native-work.txt").write_text("Retain this unexpected effect\n")
                    fault = mode.removeprefix("fork_untrusted_")
                    if fault == "turn":
                        send({"method": "turn/started", "params": {"threadId": identifier, "turn": {"id": "unexpected"}}})
                    elif fault == "item":
                        send({"method": "item/started", "params": {"threadId": identifier, "turnId": "unexpected",
                            "item": {"id": "unexpected-work", "type": "commandExecution"}}})
                    elif fault == "request":
                        send({"id": "unexpected-request", "method": "item/tool/requestUserInput", "params": {"threadId": identifier}})
                    else:
                        send({"__raw_stdout": "{unreadable native frame"})
                if mode in {"control_stop_tail", "compact_stop_tail"}:
                    for frame in json.loads((root / "native-stop-tail.json").read_text()):
                        send(frame)
                return
            buffered += chunk
            continue
        line, buffered = buffered.split(b"\n", 1)
        yield line


for line in frames():
    frame = json.loads(line)
    with (root / "native-frames.jsonl").open("a") as handle:
        handle.write(json.dumps(frame) + "\n")
    method, params = frame.get("method"), frame.get("params", {})
    result = {}
    if method is None:
        continue  # A response to a server request is not a client RPC.
    if method == "initialized":
        continue
    if mode == f"unsupported:{method}":
        send({"id": frame["id"], "error": {"code": -32601, "message": f"Method not found: {method}"}})
        continue
    if method == "initialize":
        version_path = root / "native-version"
        result = {"userAgent": f"codex-cli/{version_path.read_text() if version_path.exists() else '0.167.0'}"}
    if method == "model/list":
        catalog_model = defaults["model"] or "catalog-default"
        metadata = {"id": catalog_model, "model": catalog_model, "isDefault": True,
                    "serviceTiers": [] if mode == "unsupported_fast_model" else [{"id": "priority"}]}
        result = {"data": [metadata], "nextCursor": None}
        if mode == "paged_fast_model" and params.get("cursor") is None:
            result = {"data": [{"id": "another-model", "model": "another-model", "isDefault": False}], "nextCursor": "second"}
    if method == "config/read":
        if mode == "early_root":
            send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": "unsolicited"}}})
            if metrics_enabled:
                metric_record(thread_id, "token_usage_record", {"thread_id": thread_id, "turn_id": "unsolicited",
                    "response_id": "unsolicited-response", "usage": {"output_tokens": 9}})
                send({"method": "turn/completed", "params": {"threadId": thread_id,
                    "turn": {"id": "unsolicited", "status": "completed"}}})
        result = {"config": {"model": defaults["model"], "model_reasoning_effort": defaults["effort"],
            "features": {"fast_mode": mode != "bad_fast_config", "wake_on_agent_messages": mode == "bad_config", "goals": False,
            "sleep_tool": False, "unbounded_connection_retries": False, "multi_agent": True, "multi_agent_v2": {"enabled": True, "wait_agent_enabled": True}}}}
        if (root / "native-config.json").exists():
            result["config"].update(json.loads((root / "native-config.json").read_text()))
        if (root / "native-config-layers.json").exists():
            result["layers"] = json.loads((root / "native-config-layers.json").read_text())
        if (root / "native-config-error.json").exists():
            send({"id": frame["id"], "error": json.loads((root / "native-config-error.json").read_text())})
            continue
    if method == "thread/fork":
        identifier = "actual-native-fork"
        copied = root / f"{identifier}.jsonl"
        records = [json.loads(line) for line in history.read_text().splitlines()]
        records[0]["payload"]["id"] = identifier
        copied.write_text("".join(json.dumps(record) + "\n" for record in records))
        copied.with_suffix(".cwd").write_text(params["cwd"])
        created_fork = identifier, params["cwd"]
        if mode == "fork_lost_ack":
            continue
        if mode == "fork_error_after_creation":
            send({"id": frame["id"], "error": {"code": -32600, "message": "Could not restore inherited settings"}})
            continue
        result = {"thread": {"id": identifier, "path": str(copied), "cwd": params["cwd"]},
                  "cwd": params["cwd"]}
        if mode == "fork_wrong_identity":
            result["thread"]["id"] = thread_id
        if mode == "fork_foreign_path":
            result["thread"]["path"] = "/foreign-store/fork.jsonl"
    if method in {"thread/read", "thread/delete"}:
        path = root / f"{params['threadId']}.jsonl"
        if params["threadId"] == thread_id:
            path = history
        if not path.exists():
            message = "history lookup denied" if mode == "fork_missing_denied" else f"thread not found: {params['threadId']}"
            send({"id": frame["id"], "error": {"code": -32600, "message": message}})
            continue
        result = {"thread": {"id": params["threadId"], "path": str(path),
                             "cwd": path.with_suffix(".cwd").read_text() if path.with_suffix(".cwd").exists() else str(root)}}
        if method == "thread/delete":
            if mode == "fork_cleanup_failed":
                send({"id": frame["id"], "error": {"code": -32603, "message": "Native cleanup failed"}})
                continue
            path.unlink()
            result = {}
    if method in {"thread/start", "thread/resume"}:
        preparation_error = root / "native-preparation-error.json"
        if preparation_error.exists():
            error = json.loads(preparation_error.read_text())
            preparation_error.unlink()
            send({"id": frame["id"], "error": error})
            continue
        if method == "thread/resume" and (root / f"{params['threadId']}.jsonl").exists():
            thread_id = params["threadId"]
            history = root / f"{thread_id}.jsonl"
        if method == "thread/resume" and (not history.exists() or params["threadId"] != thread_id):
            send({"id": frame["id"], "error": {"code": -32600, "message": "No rollout found for thread"}})
            continue
        if method == "thread/start":
            counter = root / "native-thread-starts"
            count = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(count))
            thread_id = "exact-native-thread" if count == 1 else f"exact-native-thread-{count}"
            history.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread_id}}) + "\n"
                + json.dumps({"type": "response_item", "payload": {"type": "message", "role": "developer",
                    "content": [{"type": "input_text", "text": params["developerInstructions"]}]}}) + "\n")
        previous = json.loads(selected_path.read_text()) if method == "thread/resume" and selected_path.exists() else defaults
        overrides = params.get("config", {})
        explicit = params.get("model") is not None or "model_reasoning_effort" in overrides
        inherited = defaults if explicit else previous
        selected = {"model": params.get("model") or inherited["model"],
                    "effort": overrides.get("model_reasoning_effort", inherited["effort"])}
        selected_path.write_text(json.dumps(selected))
        tier = overrides.get("service_tier")
        result = {"thread": {"id": thread_id, "path": str(history)}, "model": selected["model"],
            "reasoningEffort": selected["effort"],
            "serviceTier": "default" if mode == "wrong_effective_tier" else "priority" if tier == "fast" else tier,
            "cwd": params.get("cwd"),
            "approvalPolicy": "never", "sandbox": {"type": "dangerFullAccess"}}
        result["collaborationMode"] = json.loads((root / "native-collaboration.json").read_text()) \
            if (root / "native-collaboration.json").exists() else {
                "mode": "default", "settings": {"model": selected["model"], "reasoning_effort": selected["effort"],
                                                "developer_instructions": None}}
    if method == "thread/inject_items":
        with history.open("a") as handle:
            for item in params["items"]:
                handle.write(json.dumps({"type": "response_item", "payload": item}) + "\n")
        if mode == "controller_lost":
            continue
    if method == "thread/compact/start":
        assert params == {"threadId": thread_id} and history.exists()
        with (root / "native-compacts.jsonl").open("a") as handle:
            handle.write(json.dumps(frame) + "\n")
        turn_id = "compact-turn"
        if mode in {"compact_ack_first", "compact_ack_only", "compact_control", "compact_stop_tail"}:
            send({"id": frame["id"], "result": {}})
        if mode == "compact_ack_only":
            continue
        send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id}}})
        if mode == "compact_disconnect":
            break
        if mode == "compact_error_after_root":
            send({"id": frame["id"], "error": {"code": -32600, "message": "Native failed after starting work"}})
            continue
        if mode == "compact_conflicting_root":
            send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": "unexpected-root"}}})
            continue
        if mode in {"compact_no_effect", "compact_native_failed"}:
            send({"id": frame["id"], "result": {}})
            send({"method": "turn/completed", "params": {"threadId": thread_id,
                "turn": {"id": turn_id, "status": "failed" if mode == "compact_native_failed" else "completed",
                    **({"error": {"message": "Native capacity exhausted", "codexErrorInfo": "serverOverloaded"}}
                        if mode == "compact_native_failed" else {})}}})
            continue
        send({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"id": "compact-item", "type": "contextCompaction"}}})
        if mode in {"compact_control", "compact_stop_tail", "compact_root_only"}:
            continue
        with history.open("a") as handle:
            handle.write(json.dumps({"type": "compacted", "payload": {"message": "Native summary"}}) + "\n")
        send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id,
            "tokenUsage": {"total": {"totalTokens": 90000}, "modelContextWindow": 1000,
                "last": {"totalTokens": 120, "inputTokens": 0, "outputTokens": 0,
                    "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "reasoningOutputTokens": 0}}}})
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"id": "compact-item", "type": "contextCompaction"}}})
        if mode not in {"compact_ack_first", "compact_lost_ack"}:
            send({"id": frame["id"], "result": {}})
        send({"method": "turn/completed", "params": {"threadId": thread_id,
            "turn": {"id": turn_id, "status": "completed"}}})
        continue
    if method == "turn/start":
        if params.get("collaborationMode"):
            controls = params["collaborationMode"]["settings"]
            selected = {"model": controls["model"], "effort": controls["reasoning_effort"]}
            selected_path.write_text(json.dumps(selected))
        with business.open("a") as handle:
            handle.write(json.dumps(params) + "\n")
        turn_id = "turn-" + str(len(business.read_text().splitlines()))
        if metrics_enabled:
            metric_record(thread_id, "event_msg", {"type": "task_started", "turn_id": turn_id})
            metric_record(thread_id, "turn_context", {"turn_id": turn_id, "model": selected["model"],
                                                      "effort": selected["effort"]})
        if mode in {"stop_late_accepted", "stop_late_rejected"}:
            delayed = frame["id"], turn_id
            continue
        if mode == "rejected":
            send({"id": frame["id"], "error": {"code": -32600, "message": "Input refused before acceptance"}})
            continue
        if mode == "start_internal_error":
            send({"id": frame["id"], "error": {"code": -32603, "message": "Internal error after possible creation"}})
            continue
        if mode not in {"unknown", "unknown_disconnect", "late_tools", "late_bad_item", "control_question_resolved_before_ack", "control_delayed_ack"}:
            send({"id": frame["id"], "result": {"turn": {"id": turn_id}}})
        if mode == "wrong_root":
            turn_id = "another-root"
        send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id}}})
        if mode in {"control_question", "control_question_resolved_before_ack"}:
            # Deliberately collide with the outstanding client turn/start ID.
            send({"id": frame["id"], "method": "item/tool/requestUserInput", "params": {
                "threadId": thread_id, "turnId": turn_id, "itemId": "question-item", "isBlocking": True,
                "questions": [{"id": "choice", "header": "Choice", "question": "Choose one", "options": [
                    {"label": "A", "description": "First"}, {"label": "B", "description": "Second"}]}]}})
            if mode == "control_question_resolved_before_ack":
                send({"method": "serverRequest/resolved", "params": {"threadId": thread_id, "requestId": frame["id"]}})
                send({"id": frame["id"], "result": {"turn": {"id": turn_id}}})
        if mode.startswith("control_"):
            if mode.startswith("control_background_"):
                send({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn_id,
                    "item": {"id": "background-command", "type": "commandExecution", "status": "inProgress",
                        "command": "pytest fixture", "cwd": str(root), "commandActions": [], "processId": "45196"}}})
            if mode == "control_group":
                child = subprocess.Popen([sys.executable, "-c",
                    "import signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    "Path(sys.argv[1]).touch(); exec('while not Path(sys.argv[2]).exists(): time.sleep(.01)')",
                    str(root / "native-group-ready"), str(root / "native-group-release")],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                (root / "native-child-pid").write_text(str(child.pid))
            continue
        if mode in {"late_tools", "late_bad_item", "native_tools", "missing_item_turn"}:
            kinds = ["commandExecution"] if mode != "native_tools" else ["collabAgentToolCall", "subAgentActivity", "webSearch", "fileChange"]
            for kind in kinds:
                for phase in ("started", "completed"):
                    event_params = {"threadId": thread_id, "turnId": turn_id, "item": {
                        "id": kind + "-1", "type": kind, "command": "printf TOOL", "cwd": str(root), "commandActions": [], "aggregatedOutput": "TOOL", "exitCode": 0,
                        "changes": [], "kind": "completed", "agentThreadId": "child-1", "agentPath": "/root/child",
                        "senderThreadId": thread_id, "agentsStates": {},
                        "tool": "wait", "query": "child-1", "receiverThreadIds": ["child-1"], "status": "completed"}}
                    if mode == "missing_item_turn":
                        event_params.pop("turnId")
                    send({"method": "item/" + phase, "params": event_params})
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"type": "agentMessage", "id": "answer-" + turn_id, "phase": "final_answer",
                "text": "Native answer after compaction" if any(json.loads(line).get("type") == "compacted"
                    for line in history.read_text().splitlines()) else "Native answer"}}})
        if mode == "late_bad_item":
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "unsupportedNewItem", "id": "bad-item"}}})
        if mode in {"disconnect", "unknown_disconnect"}:
            break
        if mode.startswith("steer_"):
            send({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"id": "native-wait", "type": "collabAgentToolCall", "tool": "wait",
                    "receiverThreadIds": ["child-1"], "senderThreadId": thread_id, "agentsStates": {}, "status": "inProgress"}}})
            (root / "native-waiting").touch()
        elif mode != "held":
            send({"method": "turn/completed", "params": {"threadId": thread_id,
                "turn": {**({} if mode == "missing_terminal_turn" else {"id": turn_id}), "status": "completed"}}})
        if mode in {"late_tools", "late_bad_item"}:
            send({"id": frame["id"], "result": {"turn": {"id": turn_id}}})
        if mode == "second_root":
            send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": "second-root"}}})
        continue
    if method == "turn/steer":
        with (root / "native-steers.jsonl").open("a") as handle:
            handle.write(json.dumps(frame) + "\n")
        if mode == "steer_reject" or (root / "native-steer-reject").exists():
            send({"id": frame["id"], "error": {"code": -32600, "message": "Target stopped before accepting input"}})
        elif mode == "steer_internal_error":
            send({"id": frame["id"], "error": {"code": -32603, "message": "Unspecified error after input submission"}})
        elif mode.startswith("steer_late_"):
            late_steer = frame["id"], turn_id
        elif mode == "steer_gate":
            gated_steer = frame["id"], turn_id
        elif mode == "steer_user_message":
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "userMessage", "id": "steered-input", "content": [], "clientId": params["clientUserMessageId"]}}})
        elif mode != "steer_lost":
            send({"id": frame["id"], "result": {"turnId": turn_id}})
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"id": "native-wait", "type": "collabAgentToolCall", "tool": "wait", "status": "completed",
                "senderThreadId": thread_id, "receiverThreadIds": ["child-1"], "agentsStates": {}}}})
        continue
    if method == "thread/backgroundTerminals/clean":
        (root / "native-clean-requested").write_text(json.dumps(params))
        if mode == "control_background_error":
            send({"id": frame["id"], "error": {"code": -32603, "message": "Cleanup unavailable"}})
            continue
        if mode == "control_background_before_ack":
            background_completed()
        send({"id": frame["id"], "result": {}})
        if mode == "control_background_after_ack":
            pending_clean = True
        continue
    if method == "turn/interrupt":
        send({"id": frame["id"], "result": {}})
        if mode not in {"control_stop_tail", "compact_stop_tail"}:
            send({"method": "turn/completed", "params": {"threadId": thread_id,
                "turn": {"id": params["turnId"], "status": "interrupted"}}})
        continue
    if "id" in frame:
        send({"id": frame["id"], "result": result})

# Retirement closes stdin after admission closes; proof can precede terminal recording.
if late_steer is not None:
    identifier, turn_id = late_steer
    if mode == "steer_late_accepted":
        send({"id": identifier, "result": {"turnId": turn_id}})
    else:
        send({"id": identifier, "error": {"code": -32600, "message": "Input was not accepted"}})
if delayed is not None:
    identifier, turn_id = delayed
    if mode == "stop_late_accepted":
        send({"id": identifier, "result": {"turn": {"id": turn_id}}})
    else:
        send({"id": identifier, "error": {"code": -32600, "message": "Input refused before acceptance"}})
