"""Native sub-agents, normalized (docs/implemented/native-subagents.md).

Both CLIs fan work out to short-lived helpers inside a turn and narrate it
while it runs — Claude Code through `system/task_*` events around a `Task`
call, Codex through `collab_tool_call` items. These tests pin the
normalization (one shape for both), the session-level bookkeeping (live UI
state, never persisted) and the `--agents` rendering that lets an Octopus
agent bring sub-agents of its own.

The event payloads below are copied from real CLI runs.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.harness import SubagentUpdate
from server.harness.claude_code import (
    ClaudeEventParser,
    _render_subagents,
    build_turn_argv,
)
from server.harness.codex import CodexEventParser
from server.harness.profile import TurnContext
from tests import test_question_lifecycle as question_tests
from tests.harness_profiles import process_profile

question_host = question_tests.question_host
owned_question_host = question_tests.owned_question_host

TOOL_USE_ID = "toolu_01Hgbnw2SWBPzZFH8wJyEZZT"
TASK_ID = "ad54ad25029726077"


def _ctx(**over) -> TurnContext:
    base = dict(
        prompt="hi",
        working_dir="/tmp",
        resume_id=None,
        system_prompt="persona",
        model=None,
        tool_allow=None,
        tool_deny=None,
        mcp_servers=[],
        credential=None,
    )
    base.update(over)
    return TurnContext(**base)


def _subagent_events(parser, objs) -> list[SubagentUpdate]:
    out: list[SubagentUpdate] = []
    for obj in objs:
        out += [
            e.subagent for e in parser.parse(obj).events if e.type == "subagent"
        ]
    return out


# --------------------------------------------------------------- claude code

CLAUDE_RUN = [
    {
        "type": "system",
        "subtype": "task_started",
        "task_id": TASK_ID,
        "tool_use_id": TOOL_USE_ID,
        "description": "Find MARKER_ZX9 token",
        "subagent_type": "Explore",
        "is_backgrounded": False,
        "spawn_depth": 1,
        "task_type": "local_agent",
        "prompt": "Search the working directory for MARKER_ZX9.",
    },
    {
        "type": "system",
        "subtype": "task_progress",
        "task_id": TASK_ID,
        "tool_use_id": TOOL_USE_ID,
        "description": "Running Search recursively for the token",
        "subagent_type": "Explore",
        "usage": {"total_tokens": 11843, "tool_uses": 1, "duration_ms": 1332},
        "last_tool_name": "Bash",
    },
    # The anonymous one: a status patch with no tool_use_id and no name.
    {
        "type": "system",
        "subtype": "task_updated",
        "task_id": TASK_ID,
        "patch": {"status": "completed", "end_time": 1789613746921},
    },
    {
        "type": "system",
        "subtype": "task_notification",
        "task_id": TASK_ID,
        "tool_use_id": TOOL_USE_ID,
        "status": "completed",
        "summary": "Found it in notes.txt",
        "usage": {"total_tokens": 13825, "tool_uses": 3, "duration_ms": 6100},
    },
]


def test_claude_task_events_normalize_into_one_run():
    updates = _subagent_events(ClaudeEventParser(), CLAUDE_RUN)
    assert [u.status for u in updates] == [
        "running",
        "running",
        "completed",
        "completed",
    ]
    # Every observation names the same run, including the status patch that
    # arrives with only a task_id — the one event that can't identify itself.
    assert {u.task_id for u in updates} == {TASK_ID}
    assert {u.tool_use_id for u in updates} == {TOOL_USE_ID}
    assert {u.name for u in updates} == {"Explore"}

    started, progress, _patch, done = updates
    assert started.prompt.startswith("Search the working directory")
    assert progress.description == "Running Search recursively for the token"
    assert (progress.tokens, progress.tool_uses) == (11843, 1)
    # The brief is carried only on the first observation; repeating it on
    # every progress tick would put a wall of text on the wire 20 times.
    assert progress.prompt == ""
    assert done.summary == "Found it in notes.txt"


def test_claude_unknown_task_status_is_still_running():
    (update,) = _subagent_events(
        ClaudeEventParser(),
        [
            {
                "type": "system",
                "subtype": "task_progress",
                "task_id": "t1",
                "tool_use_id": "tu1",
                "status": "whatever-comes-next",
            }
        ],
    )
    assert update.status == "running"


def test_claude_task_map_is_bounded():
    """A held process parses many turns; the name map can't grow forever."""
    parser = ClaudeEventParser()
    for i in range(200):
        parser.parse(
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": f"task-{i}",
                "tool_use_id": f"tu-{i}",
                "subagent_type": "Explore",
            }
        )
    assert len(parser._tasks) <= 64


def test_claude_events_without_a_task_id_are_ignored():
    assert _subagent_events(
        ClaudeEventParser(), [{"type": "system", "subtype": "task_progress"}]
    ) == []


# ---------------------------------------------------------------- the argv


def test_agents_json_is_rendered_only_when_defined():
    argv, _ = build_turn_argv(_ctx())
    assert "--agents" not in argv

    argv, _ = build_turn_argv(
        _ctx(
            subagents=[
                {
                    "name": "reviewer",
                    "description": "Reviews code",
                    "prompt": "You review.",
                    "model": "sonnet",
                    "tools": ["Read", "Grep"],
                },
                # Nameless drafts are dropped rather than sent as "".
                {"name": "   ", "description": "unfinished"},
            ]
        )
    )
    spec = json.loads(argv[argv.index("--agents") + 1])
    assert list(spec) == ["reviewer"]
    assert spec["reviewer"] == {
        "description": "Reviews code",
        "prompt": "You review.",
        "tools": ["Read", "Grep"],
        "model": "sonnet",
    }


def test_agents_json_omits_empty_optionals():
    # An explicit blank would override the CLI's own defaults; absence
    # doesn't.
    spec = _render_subagents([{"name": "a", "description": "d", "prompt": "p"}])
    assert spec == {"a": {"description": "d", "prompt": "p"}}
    assert _render_subagents(["not-a-dict", {"description": "no name"}]) == {}


# --------------------------------------------------------------------- codex

CODEX_RUN = [
    {
        "type": "item.started",
        "item": {
            "id": "item_0",
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": "parent",
            "receiver_thread_ids": [],
            "prompt": "Find MARKER_ZX9.",
            "agents_states": {},
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": "parent",
            "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
            "prompt": "Find MARKER_ZX9.",
            "agents_states": {
                "01a0ad62-b228-74d1-a2c3-1328858cca57": {
                    "status": "pending_init",
                    "message": None,
                }
            },
            "status": "completed",
        },
    },
    {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "collab_tool_call",
            "tool": "wait",
            "sender_thread_id": "parent",
            "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
            "prompt": None,
            "agents_states": {},
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "collab_tool_call",
            "tool": "wait",
            "sender_thread_id": "parent",
            "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
            "prompt": None,
            "agents_states": {
                "01a0ad62-b228-74d1-a2c3-1328858cca57": {
                    "status": "completed",
                    "message": "./notes.txt",
                }
            },
            "status": "completed",
        },
    },
]


def test_codex_collab_calls_track_one_sub_agent_across_items():
    parser = CodexEventParser()
    updates = _subagent_events(parser, CODEX_RUN)

    # `spawn_agent` and `wait` are separate tool calls describing the SAME
    # sub-agent, so they retain one exact child identity and spawning call.
    assert {u.task_id for u in updates} == {"01a0ad62-b228-74d1-a2c3-1328858cca57"}
    assert {u.tool_use_id for u in updates} == {"item_0"}
    assert [u.status for u in updates] == [
        "running",
        "completed",
    ]
    assert updates[0].prompt == "Find MARKER_ZX9."
    assert updates[-1].name == "agent 01a0ad62"
    assert updates[-1].summary == "./notes.txt"


def test_codex_collab_calls_are_also_real_tool_calls():
    """Claude's `Task` is a tool call in the transcript; Codex's equivalent
    has to be one too, or the card has nothing to attach to."""
    parser = CodexEventParser()
    events = [e for obj in CODEX_RUN for e in parser.parse(obj).events]
    kinds = [(e.type, e.tool_name, e.tool_use_id) for e in events if e.type in ("tool_use", "tool_result")]
    assert ("tool_use", "subagent:spawn_agent", "item_0") in kinds
    assert ("tool_use", "subagent:wait", "item_1") in kinds
    results = [e for e in events if e.type == "tool_result"]
    assert results[-1].content == "./notes.txt"


def test_codex_late_child_result_keeps_original_spawn_after_many_agents():
    parser = CodexEventParser()
    for i in range(200):
        parser.parse(
            {
                "type": "item.completed",
                "item": {
                    "id": f"item_{i}",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "receiver_thread_ids": [f"thread-{i}"],
                    "agents_states": {},
                },
            }
        )
    updates = _subagent_events(parser, [{
        "type": "item.completed",
        "item": {
            "id": "item_200",
            "type": "collab_tool_call",
            "tool": "wait",
            "receiver_thread_ids": ["thread-0"],
            "agents_states": {"thread-0": {"status": "completed", "message": "late result"}},
        },
    }])
    assert len(updates) == 1
    assert updates[0].task_id == "thread-0"
    assert updates[0].tool_use_id == "item_0"
    assert updates[0].status == "completed"
    assert updates[0].summary == "late result"


# ------------------------------------------------------- session bookkeeping


def _session():
    from server.session_manager import Session

    return Session(id="s1", name="s", working_dir="/tmp")


def test_observations_merge_onto_the_run():
    """Every harness reports sub-agents in partial shapes — a status patch
    with no name, a summary with no counters. The card needs the union."""
    from server.session_manager import SessionManager

    session = _session()
    SessionManager._record_subagent(
        session,
        SubagentUpdate(
            task_id="t1",
            tool_use_id="tu1", tool_call_id="tu1",
            name="Explore",
            description="searching",
            prompt="find it",
            tokens=100,
        ),
    )
    SessionManager._record_subagent(
        session,
        SubagentUpdate(task_id="t1", tool_use_id="tu1", tool_call_id="tu1", status="completed", summary="done"),
    )

    (run,) = session._subagents.values()
    assert run.status == "completed"
    assert run.summary == "done"
    # Carried forward rather than blanked by the partial update.
    assert run.name == "Explore"
    assert run.description == "searching"
    assert run.prompt == "find it"
    assert run.tokens == 100


def test_runs_are_bounded_per_session():
    from server.session_manager import _MAX_SUBAGENTS_PER_SESSION, SessionManager

    session = _session()
    for i in range(_MAX_SUBAGENTS_PER_SESSION + 20):
        SessionManager._record_subagent(
            session, SubagentUpdate(task_id=f"t{i}", tool_use_id=f"tu{i}", tool_call_id=f"tu{i}")
        )
    assert len(session._subagents) == _MAX_SUBAGENTS_PER_SESSION
    # Oldest dropped, newest kept.
    assert "tu0" not in session._subagents
    assert f"tu{_MAX_SUBAGENTS_PER_SESSION + 19}" in session._subagents


def test_an_observation_without_a_key_is_ignored():
    from server.session_manager import SessionManager

    session = _session()
    SessionManager._record_subagent(session, SubagentUpdate(task_id="", tool_use_id=None))
    assert session._subagents == {}


def test_subagent_events_broadcast_but_never_persist():
    from server.harness import HarnessEvent
    from server.session_manager import SessionManager

    event = HarnessEvent(
        type="subagent",
        subagent=SubagentUpdate(
            task_id="t1",
            tool_use_id="tu1", tool_call_id="tu1",
            status="running",
            name="Explore",
            description="searching",
            tokens=11843,
            tool_uses=1,
        ),
    )
    # The durable record is the Task tool call already in the transcript;
    # persisting progress would double every sub-agent in the history.
    assert SessionManager._event_to_message_content(event) is None

    ws = SessionManager._event_to_ws_message("s1", event)
    assert ws == {
        "type": "subagent",
        "session_id": "s1",
        "task_id": "t1",
        "tool_use_id": "tu1",
        "tool_call_id": "tu1",
        "card_id": None,
        "native_thread_id": None,
        "native_turn_id": None,
        "parent_thread_id": None,
        "runtime_metadata": None,
        "status": "running",
        "stop_reason": None,
        "name": "Explore",
        "description": "searching",
        "prompt": "",
        "summary": "",
        "tokens": 11843,
        "tool_uses": 1,
        "duration_ms": None,
    }


# ------------------------------------------------------------------ the API


@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from server.agent_manager import AgentManager
    from server.database import Database
    from server.main import app
    from server.routers import agents as agents_mod
    from server.session_manager import session_manager

    db = Database(":memory:")
    await db.initialize()
    from tests.accounts import create_account
    _, token = await create_account(db)
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    agents_mod.set_manager(AgentManager(db))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": "Bearer " + token}) as c:
        yield c
    await db.close()


@pytest.mark.asyncio
async def test_an_agent_can_carry_its_own_subagents(client):
    created = await client.post(
        "/api/agents",
        json={
            "name": "Builder",
            "subagents": [
                {
                    "name": "reviewer",
                    "description": "Reviews code",
                    "prompt": "You review.",
                }
            ],
        },
        headers=client.headers,
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    assert [sa["name"] for sa in agent["subagents"]] == ["reviewer"]

    # And they round-trip through an edit, where an agent that defines none
    # is the normal state.
    patched = await client.patch(
        f"/api/agents/{agent['id']}", json={"subagents": []}, headers=client.headers
    )
    assert patched.status_code == 200
    assert patched.json()["subagents"] == []


@pytest.mark.asyncio
async def test_pre_existing_agents_read_back_with_no_subagents(client):
    """The column is additive — every row that predates it means "built-ins
    only", not "broken agent"."""
    listed = (await client.get("/api/agents", headers=client.headers)).json()
    assert listed, "the Default Agent should exist"
    assert all(a["subagents"] == [] for a in listed)


@pytest.mark.asyncio
async def test_the_session_snapshot_carries_live_runs(client):
    from server.session_manager import SessionManager, session_manager

    agents = (await client.get("/api/agents", headers=client.headers)).json()
    made = await client.post(
        "/api/sessions",
        json={"name": "S", "agent_id": agents[0]["id"]},
        headers=client.headers,
    )
    assert made.status_code == 201, made.text
    sid = made.json()["id"]

    SessionManager._record_subagent(
        session_manager.sessions[sid],
        SubagentUpdate(
            task_id="t1", tool_use_id="tu1", tool_call_id="tu1", name="Explore", description="searching"
        ),
    )

    # A browser reloading mid-run repaints the card instead of showing a
    # tool call that looks frozen.
    detail = (await client.get(f"/api/sessions/{sid}", headers=client.headers)).json()
    assert detail["subagents"] == [
        {
            "task_id": "t1",
            "tool_use_id": "tu1",
            "tool_call_id": "tu1",
            "card_id": None,
            "native_thread_id": None,
            "native_turn_id": None,
            "parent_thread_id": None,
            "runtime_metadata": None,
            "status": "running",
            "stop_reason": None,
            "name": "Explore",
            "description": "searching",
            "prompt": "",
            "summary": "",
            "tokens": None,
            "tool_uses": None,
            "duration_ms": None,
            # The trail starts with the step it is on, so a reload mid-run
            # restores history rather than a single line.
            "steps": ["searching"],
        }
    ]


# ------------------------------------------- work that outlives its own turn


@pytest.fixture
async def held_subagent_transport(owned_question_host, monkeypatch):
    """Keep the actual process, stream and original idle reporter together."""
    from server.harness import get_harness
    from server.harness.run import HarnessRun

    manager, original, client, events = owned_question_host
    directory = Path(original.working_dir) / "held-subagent-transport"
    directory.mkdir(parents=True)
    model = directory / "held_model.py"
    initial_path, late_path = directory / "initial.json", directory / "late.json"
    identity_path, input_path = directory / "identity.json", directory / "input.json"
    release = directory / "release-late"
    model.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
        "Path(sys.argv[2]).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'start_time':fields[19]}))\n"
        "Path(sys.argv[3]).write_text(sys.stdin.readline())\n"
        "for event in json.loads(Path(sys.argv[1]).read_text()): print(json.dumps(event),flush=True)\n"
        "while not Path(sys.argv[5]).exists(): time.sleep(.005)\n"
        "for event in json.loads(Path(sys.argv[4]).read_text()): print(json.dumps(event),flush=True)\n"
        "sys.stdin.read()\n"
    )
    initial_path.write_text(json.dumps([
        {"type": "system", "subtype": "init", "session_id": "held-original-thread"},
        {"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": TOOL_USE_ID, "name": "Task",
            "input": {"description": "searching", "prompt": "find it", "subagent_type": "general-purpose"},
        }]}},
        {"type": "system", "subtype": "task_started", "task_id": TASK_ID,
         "tool_use_id": TOOL_USE_ID, "description": "searching", "subagent_type": "general-purpose"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "in turn"}]}},
        {"type": "result", "subtype": "success", "session_id": "held-original-thread", "is_error": False},
    ]))
    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(initial_path), str(identity_path),
             str(input_path), str(late_path), str(release)],
            {"cwd": context.working_dir},
        ),
    ))
    processes, captured_runs, idle_events = [], [], []
    stream_readers, stdout_readers = set(), set()
    spawn, stream, install_reporter = asyncio.create_subprocess_exec, HarnessRun.stream, HarnessRun.set_idle_handler

    async def observe_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            processes.append(process)
        return process

    async def observe_stream(run):
        stream_readers.add(asyncio.current_task())
        async for event in stream(run):
            yield event

    def capture_reporter(run, reporter):
        captured_runs.append(run)

        async def observe_original_idle(event):
            stdout_readers.add(asyncio.current_task())
            idle_events.append(event)
            await reporter(event)

        install_reporter(run, observe_original_idle)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(HarnessRun, "stream", observe_stream)
    monkeypatch.setattr(HarnessRun, "set_idle_handler", capture_reporter)
    session = await manager.create_session(
        original.agent_id, "Original held subagent", str(directory), backend="claude-code",
    )
    owner_events, foreign_events = [], []

    async def observe_owner(event):
        if event.get("session_id") == session.id:
            owner_events.append(event)

    async def observe_foreign(event):
        if event.get("session_id") == session.id:
            foreign_events.append(event)

    manager.on_broadcast("held-original-owner", observe_owner, user_id=session.user_id)
    manager.on_broadcast("held-foreign-owner", observe_foreign, user_id="another-subagent-owner")
    receipt = None

    def emit_late(frames):
        late_path.write_text(json.dumps(frames))
        release.touch()

    try:
        prompt = "Keep the original native helper and its later report."
        receipt = await manager.start_message(session.id, prompt, work_id=f"held-work-{session.id}")
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.recording_state == "committed" and completion.outcome.status == "success"
        assert len(processes) == len(captured_runs) == len(stream_readers) == 1
        identity = json.loads(identity_path.read_text())
        assert identity["pid"] == identity["pgid"] == processes[0].pid
        assert Path(f"/proc/{identity['pid']}").exists() and processes[0].returncode is None
        assert all(reader.done() for reader in stream_readers)
        assert json.loads(input_path.read_text())["message"]["content"] == prompt
        attempt = next(event["attempt_id"] for event in owner_events if event["type"] == "attempt_started")
        yield SimpleNamespace(
            manager=manager, session=session, client=client, events=events,
            owner_events=owner_events, foreign_events=foreign_events, idle_events=idle_events,
            processes=processes, stream_readers=stream_readers, stdout_readers=stdout_readers,
            receipt=receipt, attempt=attempt, identity=identity, emit_late=emit_late, prompt=prompt,
        )
    finally:
        if not release.exists():
            emit_late([])
        try:
            await manager.interrupt(session.id)
            await manager.await_session_execution(session.id, timeout=5)
            await manager.drain_turns(timeout=5)
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 3)
            await asyncio.gather(*stream_readers, *stdout_readers, return_exceptions=True)


async def _retire_original_transport(env):
    await env.manager.interrupt(env.session.id)
    await env.manager.await_session_execution(env.session.id, timeout=5)
    await asyncio.wait_for(env.processes[0].wait(), 3)
    assert not Path(f"/proc/{env.identity['pid']}").exists()
    assert len(env.stdout_readers) == 1
    assert all(reader.done() for reader in env.stdout_readers | env.stream_readers)
    assert (await env.receipt.wait()).outcome.status == "success"
    print("Actual original held transport retired: " + json.dumps({
        "pid": env.identity["pid"], "request_id": env.receipt.request_id,
        "attempt_id": env.attempt, "work_id": f"held-work-{env.session.id}",
        "returncode": env.processes[0].returncode, "stream_readers_joined": len(env.stream_readers),
        "stdout_readers_joined": len(env.stdout_readers), "foreign_frames": len(env.foreign_events),
    }), flush=True)


@pytest.mark.asyncio
async def test_events_after_a_turn_reach_the_session(held_subagent_transport):
    """The CLI's async sub-agents are the reason this exists.

    `Async agent launched successfully` returns immediately: the turn ends,
    the sub-agent keeps working inside the held process, and when it lands the
    CLI wakes the agent to report it. Those events used to be dropped — the
    card spun forever and the answer never arrived (native-subagents.md §7).
    """
    env = held_subagent_transport
    delivered = asyncio.Event()
    body = "The background agent found 2 files."

    async def see_original_report(event):
        if event.get("session_id") == env.session.id and event.get("content") == body:
            delivered.set()

    env.manager.on_broadcast("original-late-report", see_original_report, user_id=env.session.user_id)
    initial = await env.client.get(f"/api/sessions/{env.session.id}")
    assert initial.status_code == 200
    assert initial.json()["subagents"][0]["status"] == "running"
    original_call = initial.json()["subagents"][0]["tool_call_id"]
    env.emit_late([
        {"type": "system", "subtype": "task_notification", "task_id": TASK_ID,
         "tool_use_id": TOOL_USE_ID, "status": "completed", "summary": "2 files"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": body}]}},
    ])
    await asyncio.wait_for(delivered.wait(), 3)
    kinds = [message["type"] for message in env.owner_events]
    assert "subagent" in kinds and "assistant_text" in kinds

    # The report is persisted, so it's still there after a reload — unlike
    # the progress that produced it.
    detail = (await env.client.get(f"/api/sessions/{env.session.id}")).json()
    assert any(
        m["type"] == "text" and "background agent found 2 files" in (m["content"] or "")
        for m in detail["messages"]
    )
    assert detail["subagents"][0]["status"] == "completed"
    assert detail["subagents"][0]["summary"] == "2 files"
    assert detail["subagents"][0]["tool_call_id"] == original_call
    assert [row["content"] for row in detail["messages"] if row["role"] == "assistant" and row["type"] == "text"] == ["in turn", body]
    assert [row["content"] for row in detail["messages"] if row["role"] == "user"] == [env.prompt]
    assert len([row for row in detail["messages"] if row["type"] == "result"]) == 1
    late = [event for event in env.owner_events if event.get("content") == body
            or event.get("type") == "subagent" and event.get("status") == "completed"]
    assert len(late) == 2
    assert all(event["request_id"] == env.receipt.request_id and event["attempt_id"] == env.attempt
               and event["work_id"] == f"held-work-{env.session.id}" for event in late)
    assert env.foreign_events == []
    await _retire_original_transport(env)


@pytest.mark.asyncio
async def test_an_idle_event_for_an_unknown_session_is_dropped():
    from server.harness import HarnessEvent
    from server.session_manager import session_manager

    # Deleted while its process was still finishing something.
    await session_manager._handle_idle_event(
        "gone", HarnessEvent(type="text", content="hello?")
    )


def test_losing_the_process_ends_its_sub_agents():
    """A sub-agent lives inside the CLI process. When the reaper stops it, a
    card that keeps spinning is a lie."""
    from server.session_manager import SessionManager

    mgr = SessionManager()
    session = _session()
    SessionManager._record_subagent(
        session, SubagentUpdate(task_id="t1", tool_use_id="tu1", tool_call_id="tu1", name="Explore")
    )
    SessionManager._record_subagent(
        session,
        SubagentUpdate(
            task_id="t2", tool_use_id="tu2", tool_call_id="tu2", status="completed", summary="done"
        ),
    )
    session._run.held_since = 1.0

    mgr._forget_backend(session)

    assert session._run.backend is None and session._run.held_since is None
    assert session._subagents["tu1"].status == "interrupted"
    assert session._subagents["tu1"].stop_reason == "process_ended"
    # A run that already finished keeps its result.
    assert session._subagents["tu2"].status == "completed"
    assert session._subagents["tu2"].summary == "done"


@pytest.mark.asyncio
async def test_the_run_hands_post_turn_events_to_the_idle_handler():
    """Below the session layer: once a turn's stream is closed, further
    events go to the handler instead of the floor."""
    from server.harness import HarnessEvent
    from server.harness.registry import get_harness
    from server.harness.run import HarnessRun

    run = get_harness("claude-code").create_run()
    seen: list[HarnessEvent] = []

    async def handler(event: HarnessEvent) -> None:
        seen.append(event)

    run.set_idle_handler(handler)

    # An in-turn text lands on the stream…
    await run._handle_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "in turn"}]},
            }
        )
    )
    assert seen == []
    assert isinstance(run, HarnessRun)

    # …the turn ends…
    run._close_stream()

    # …and what comes after belongs to the session.
    await run._handle_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after the turn"}]},
            }
        )
    )
    assert [e.content for e in seen] == ["after the turn"]


def test_the_trail_records_each_new_step_once():
    """"What is it doing?" can't be answered by one line when a run makes
    eighty tool calls — but a progress event repeats the current step while
    that call runs, so the trail must not fill with duplicates."""
    from server.session_manager import SessionManager

    session = _session()
    for description in ("Searching the web", "Searching the web", "Fetching a page"):
        SessionManager._record_subagent(
            session,
            SubagentUpdate(task_id="t1", tool_use_id="tu1", tool_call_id="tu1", description=description),
        )
    (run,) = session._subagents.values()
    assert run.steps == ["Searching the web", "Fetching a page"]


def test_the_trail_is_bounded():
    from server.harness.events import MAX_SUBAGENT_STEPS
    from server.session_manager import SessionManager

    session = _session()
    for i in range(MAX_SUBAGENT_STEPS + 25):
        SessionManager._record_subagent(
            session,
            SubagentUpdate(task_id="t1", tool_use_id="tu1", tool_call_id="tu1", description=f"step {i}"),
        )
    (run,) = session._subagents.values()
    assert len(run.steps) == MAX_SUBAGENT_STEPS
    assert run.steps[-1] == f"step {MAX_SUBAGENT_STEPS + 24}"


@pytest.mark.asyncio
async def test_out_of_turn_token_deltas_are_dropped(held_subagent_transport):
    """A delta is a live view of a block that is about to arrive complete.

    Out of turn there is nothing live to view: the UI has already painted the
    final text, so delivering a trailing delta repaints a stale partial on top
    of the answer. An e2e run caught this under load, where a short reply's
    completed block beat its own deltas.
    """
    env = held_subagent_transport
    delivered = asyncio.Event()

    async def see_original_answer(event):
        if event.get("session_id") == env.session.id and event.get("content") == "the real answer":
            delivered.set()

    env.manager.on_broadcast("original-complete-answer", see_original_answer, user_id=env.session.user_id)
    env.emit_late([
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": "stale part"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "the real answer"}]}},
    ])
    await asyncio.wait_for(delivered.wait(), 3)
    assert [(event.type, event.content) for event in env.idle_events] == [
        ("text_delta", "stale part"), ("text", "the real answer"),
    ]
    kinds = [message["type"] for message in env.owner_events]
    assert "assistant_delta" not in kinds
    assert "assistant_text" in kinds
    late = [event for event in env.owner_events if event.get("content") == "the real answer"]
    assert len(late) == 1
    assert late[0]["request_id"] == env.receipt.request_id and late[0]["attempt_id"] == env.attempt
    assert late[0]["work_id"] == f"held-work-{env.session.id}"
    detail = (await env.client.get(f"/api/sessions/{env.session.id}")).json()
    assert [row["content"] for row in detail["messages"] if row["role"] == "assistant" and row["type"] == "text"] == [
        "in turn", "the real answer",
    ]
    assert not any("stale part" in str(row["content"]) for row in detail["messages"])
    assert len([row for row in detail["messages"] if row["type"] == "result"]) == 1
    assert [row["content"] for row in detail["messages"] if row["role"] == "user"] == [env.prompt]
    assert env.foreign_events == []
    await _retire_original_transport(env)
