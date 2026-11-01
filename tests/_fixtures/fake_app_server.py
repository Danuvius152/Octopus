"""Deterministic stdio peer for public Codex submission tests (no provider)."""

import json
import os
import select
import subprocess
import sys
from pathlib import Path

root, mode = Path(sys.argv[1]), sys.argv[2]
history = root / "native-history.jsonl"
business = root / "native-business.jsonl"
thread_id = json.loads(history.read_text().splitlines()[0])["payload"]["id"] if history.exists() else "exact-native-thread"
delayed = None
late_steer = None
gated_steer = None
defaults_path = root / "native-defaults.json"
defaults = json.loads(defaults_path.read_text()) if defaults_path.exists() else {"model": "fixture", "effort": "low"}
selected_path = root / "native-selected.json"


def send(frame):
    if "__raw_stdout" in frame:
        print(frame["__raw_stdout"], flush=True)
        return
    print(json.dumps(frame), flush=True)


def frames():
    global gated_steer
    buffered = b""
    injected = 0
    while True:
        injections = root / "native-injections.jsonl"
        if injections.exists():
            values = injections.read_text().splitlines()
            for value in values[injected:]:
                send(json.loads(value))
            injected = len(values)
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
                if mode == "control_stop_tail":
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
        result = {"config": {"model": defaults["model"], "model_reasoning_effort": defaults["effort"],
            "features": {"fast_mode": mode != "bad_fast_config", "wake_on_agent_messages": mode == "bad_config", "goals": False,
            "sleep_tool": False, "multi_agent": True, "multi_agent_v2": {"enabled": True, "wait_agent_enabled": True}}}}
    if method in {"thread/start", "thread/resume"}:
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
    if method == "thread/inject_items":
        with history.open("a") as handle:
            for item in params["items"]:
                handle.write(json.dumps({"type": "response_item", "payload": item}) + "\n")
        if mode == "controller_lost":
            continue
    if method == "turn/start":
        with business.open("a") as handle:
            handle.write(json.dumps(params) + "\n")
        turn_id = "turn-" + str(len(business.read_text().splitlines()))
        if mode in {"stop_late_accepted", "stop_late_rejected"}:
            delayed = frame["id"], turn_id
            continue
        if mode == "rejected":
            send({"id": frame["id"], "error": {"code": -32600, "message": "Input refused before acceptance"}})
            continue
        if mode not in {"unknown", "unknown_disconnect", "late_tools", "late_bad_item", "control_question_resolved_before_ack"}:
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
                        "id": kind + "-1", "type": kind, "command": "printf TOOL", "aggregatedOutput": "TOOL", "exitCode": 0,
                        "tool": "wait", "receiverThreadIds": ["child-1"], "status": "completed"}}
                    if mode == "missing_item_turn":
                        event_params.pop("turnId")
                    send({"method": "item/" + phase, "params": event_params})
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"type": "agentMessage", "id": "answer-" + turn_id, "phase": "final_answer", "text": "Native answer"}}})
        if mode == "late_bad_item":
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "unsupportedNewItem", "id": "bad-item"}}})
        if mode in {"disconnect", "unknown_disconnect"}:
            break
        if mode.startswith("steer_"):
            send({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"id": "native-wait", "type": "collabAgentToolCall", "tool": "wait",
                    "receiverThreadIds": ["child-1"], "status": "inProgress"}}})
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
        if mode == "steer_reject":
            send({"id": frame["id"], "error": {"code": -32600, "message": "Target stopped before accepting input"}})
        elif mode == "steer_internal_error":
            send({"id": frame["id"], "error": {"code": -32603, "message": "Unspecified error after input submission"}})
        elif mode.startswith("steer_late_"):
            late_steer = frame["id"], turn_id
        elif mode == "steer_gate":
            gated_steer = frame["id"], turn_id
        elif mode == "steer_user_message":
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "userMessage", "id": "steered-input", "clientId": params["clientUserMessageId"]}}})
        elif mode != "steer_lost":
            send({"id": frame["id"], "result": {"turnId": turn_id}})
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"id": "native-wait", "type": "collabAgentToolCall", "tool": "wait", "status": "completed"}}})
        continue
    if method == "turn/interrupt":
        send({"id": frame["id"], "result": {}})
        if mode != "control_stop_tail":
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
