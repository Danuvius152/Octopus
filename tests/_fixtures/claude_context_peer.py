"""Controlled Claude stream-json peer; fields follow the saved 2.1.319 capture.

This process is synthetic protocol/product evidence, never a live Claude run.
It records actual stdin and argv, and accepts ordered externally written frames.
"""

import json
import os
import re
import runpy
import select
import sys
from pathlib import Path

read_frames = runpy.run_path(str(Path(__file__).with_name("jsonl_frames.py")))["read_frames"]
root = Path(sys.argv[1])
sid = "exact-claude-session"
model = "deepseek-flash"
offset_path = root / "claude-offset"
offset = int(offset_path.read_text()) if offset_path.exists() else 0
current = None


def send(value):
    if value.get("__exit"):
        sys.exit(0)
    print(json.dumps(value), flush=True)


def injections(*, finished=False):
    global offset
    values = read_frames(root / "claude-injections.jsonl", finished=finished)
    for value in values[offset:]:
        send(value)
    offset = len(values)
    offset_path.write_text(str(offset))


pending = b""
while True:
    injections()
    if not select.select([sys.stdin], [], [], .01)[0]:
        continue
    chunk = os.read(sys.stdin.fileno(), 65536)
    if not chunk:
        injections(finished=True)
        tail = root / "claude-eof.json"
        if tail.exists():
            for value in json.loads(tail.read_text()):
                send(value)
        break
    pending += chunk
    while b"\n" in pending:
        line, pending = pending.split(b"\n", 1)
        current = json.loads(line)
        with (root / "claude-inputs.jsonl").open("a") as handle:
            handle.write(json.dumps({"input": current, "pid": os.getpid(), "argv": sys.argv[2:]}) + "\n")
        plan_path = root / "claude-plan.json"
        plan = json.loads(plan_path.read_text()) if plan_path.exists() else {}
        send({"type": "system", "subtype": "init", "session_id": plan.get("init_session_id", sid),
              "model": model, "slash_commands": plan.get("commands", ["compact"])})
        if plan.get("hold"):
            continue
        identity = {"session_id": sid, "parent_tool_use_id": None,
                    "user_message_uuid": current["uuid"], "user_message_uuids": [current["uuid"]]}
        if plan.get("save_input_history"):
            directory = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f"{sid}.jsonl").open("a") as handle:
                handle.write(json.dumps({"type": "user", "uuid": current["uuid"], "sessionId": sid,
                    "isSidechain": False, "message": current["message"]}) + "\n")
        if plan.get("hold_after_history"):
            continue
        if plan.get("failure"):
            send({**identity, "type": "result", "subtype": "success", "is_error": True,
                  "terminal_reason": "api_error", "api_error_status": 400,
                  "result": "API Error: 400 synthetic replay installation probe failure"})
            continue
        if current["message"]["content"] == "/compact":
            mode = plan.get("compact_mode")
            if mode != "no_start":
                send({"type": "command_lifecycle", "state": "started", "session_id": sid,
                      "command_uuid": current["uuid"]})
            send({"type": "system", "subtype": "status", "status": "compacting", "session_id": sid})
            if mode == "hold":
                continue
            if mode == "disconnect":
                sys.exit(0)
            if mode in {"noop", "malformed_noop", "missing_identity", "unrecognized_noop"}:
                # Conditional official-doc contract, not a captured 2.1.319
                # producer outcome (that version's empty-history case failed).
                if mode == "malformed_noop":
                    print("{malformed", flush=True)
                source = {"session_id": sid} if mode == "missing_identity" else identity
                send({**source, "type": "result", "subtype": "success", "is_error": False,
                      "local_command": "other" if mode == "unrecognized_noop" else "compact",
                      "result": "Not enough messages to compact."})
                continue
            if mode == "local_failed":
                send({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
                      "local_command_run": {"command": "compact", "args": ""},
                      "local_command_outcome": {"kind": "failed"},
                      "message": {"id": "local-failed", "model": "<synthetic>", "usage": {"input_tokens": 0},
                                  "content": [{"type": "text", "text": plan.get("reason", "Error: No messages to compact")}]}})
                send({**identity, "type": "result", "subtype": "success", "is_error": False,
                      "local_command": "compact", "result": ""})
                continue
            if mode == "success_without_boundary":
                send({**identity, "type": "result", "subtype": "success", "is_error": False,
                      "local_command": "compact", "result": ""})
                continue
            send({"type": "system", "subtype": "compact_boundary", "session_id": sid,
                  "uuid": "boundary-" + current["uuid"], "compact_metadata": {
                      "trigger": "auto" if mode == "auto_only" else "manual", "pre_tokens": 90000, "post_tokens": 123}})
            (root / "claude-summary-installed").touch()
            send({**identity, "type": "result", "subtype": "error_during_execution" if mode == "applied_then_error" else "success",
                  "is_error": mode == "applied_then_error", "local_command": "compact",
                  "result": "Native post-compaction hook failed" if mode == "applied_then_error" else ""})
            continue
        if plan.get("fail_once") and not (root / "claude-failed-once").exists():
            (root / "claude-failed-once").touch()
            send({**identity, "type": "result", "subtype": "error_during_execution", "is_error": True,
                  "errors": ["API Error: 503 Service unavailable"]})
            continue
        answer = "Native answer after compaction" if (root / "claude-summary-installed").exists() else "Native answer"
        send({**identity, "type": "assistant", "message": {
            "id": "message-" + current["uuid"], "model": model,
            "content": [{"type": "text", "text": answer}],
            "usage": {"input_tokens": 111, "cache_creation_input_tokens": 222,
                      "cache_read_input_tokens": 333, "output_tokens": 1}}})
        send({**identity, "type": "result", "subtype": "success", "is_error": False,
              "result": answer, "total_cost_usd": .002, "modelUsage": {
                  "wrong-first-model": {"contextWindow": 1000000},
                  model: {"contextWindow": 200000, "outputTokens": 90000}}})
