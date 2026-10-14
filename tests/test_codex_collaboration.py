"""Codex collaboration observations retain real child identity and outcomes."""

import pytest

from server.harness.codex import CodexEventParser


def test_mailbox_wait_is_a_tool_call_without_an_anonymous_helper():
    parser = CodexEventParser()
    events = []
    for lifecycle, status in (("item.started", "in_progress"), ("item.completed", "completed")):
        events.extend(parser.parse({
            "type": lifecycle,
            "item": {
                "id": "item_0", "type": "collab_tool_call", "tool": "wait",
                "receiver_thread_ids": [], "agents_states": {}, "status": status,
            },
        }).events)

    assert [(event.type, event.tool_use_id) for event in events] == [
        ("tool_use", "item_0"), ("tool_result", "item_0"),
    ]
    assert events[0].tool_name == "subagent:wait"


def test_spawn_and_wait_observe_the_child_thread_and_original_spawn_call():
    parser = CodexEventParser()
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    started = parser.parse({
        "type": "item.started",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
                 "receiver_thread_ids": [], "agents_states": {}, "prompt": "Review this change."},
    }).events
    assert [event.type for event in started] == ["tool_use"]

    spawned = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "pending_init"}},
                 "prompt": "Review this change."},
    }).events[-1].subagent
    finished = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "collab_tool_call", "tool": "wait",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "completed", "message": "Review complete."}}},
    }).events[-1].subagent
    assert spawned is not None and finished is not None
    assert (spawned.task_id, spawned.tool_use_id, spawned.status, spawned.prompt) == (
        child, "item_0", "running", "Review this change.",
    )
    assert (finished.task_id, finished.tool_use_id, finished.status, finished.summary) == (
        child, "item_0", "completed", "Review complete.",
    )


@pytest.mark.parametrize(("native_status", "status", "reason"), [
    ("errored", "failed", None),
    ("interrupted", "interrupted", "native_interrupted"),
    ("shutdown", "interrupted", "native_shutdown"),
    ("not_found", "unknown", "native_not_found"),
    ("cancelled", "cancelled", "native_cancelled"),
])
def test_native_child_status_preserves_the_distinction_from_model_failure(native_status, status, reason):
    parser = CodexEventParser()
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    events = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": native_status}}},
    }).events
    update = events[-1].subagent
    assert update is not None
    assert (update.task_id, update.tool_use_id, update.status, update.stop_reason) == (
        child, None, status, reason,
    )


@pytest.mark.parametrize("later_state", [{}, {"status": "running"}, {"status": "future_status"}, {"status": "not_found"}])
def test_wait_without_a_new_execution_cannot_replace_a_confirmed_completion(later_state):
    parser = CodexEventParser()
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    completed = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "completed", "message": "Confirmed answer."}}},
    }).events[-1].subagent
    assert completed is not None and completed.status == "completed"

    events = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "collab_tool_call", "tool": "wait",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: later_state}},
    }).events
    assert [event.type for event in events] == ["tool_result"]


def test_wait_observing_an_unbound_running_child_does_not_invent_an_execution():
    events = CodexEventParser().parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
                 "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
                 "status": "completed", "agents_states": {
                     "01a0ad62-b228-74d1-a2c3-1328858cca57": {"status": "running"},
                 }},
    }).events
    update = events[-1].subagent
    assert update is not None and update.status == "unknown" and update.tool_use_id is None


@pytest.mark.parametrize("tool", ["send_input", "resume_agent"])
def test_accepted_explicit_continuation_can_start_a_new_execution_on_the_same_child(tool):
    parser = CodexEventParser()
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    parser.parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "completed", "message": "First answer."}}},
    })
    waiting_for_acceptance = parser.parse({
        "type": "item.started",
        "item": {"id": "item_1", "type": "collab_tool_call", "tool": tool,
                 "receiver_thread_ids": [child], "status": "in_progress", "agents_states": {}},
    }).events
    assert [event.type for event in waiting_for_acceptance] == ["tool_use"]
    accepted = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "collab_tool_call", "tool": tool,
                 "receiver_thread_ids": [child], "status": "completed", "agents_states": {}},
    }).events[-1].subagent
    assert accepted is not None
    assert (accepted.task_id, accepted.tool_use_id, accepted.status, accepted.stop_reason) == (
        child, "item_0", "running", None,
    )
    finished = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_2", "type": "collab_tool_call", "tool": "wait",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "completed", "message": "Second answer."}}},
    }).events[-1].subagent
    assert finished is not None and finished.status == "completed" and finished.summary == "Second answer."


def test_accepted_input_to_an_already_running_child_reports_unconfirmed_execution():
    parser = CodexEventParser()
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    parser.parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "running"}}},
    })
    accepted = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "collab_tool_call", "tool": "send_input",
                 "receiver_thread_ids": [child], "status": "completed", "agents_states": {}},
    }).events[-1].subagent
    assert accepted is not None
    assert (accepted.status, accepted.stop_reason) == ("unknown", "overlapping_execution")


@pytest.mark.parametrize("tool", ["spawn_agent", "send_input", "resume_agent"])
def test_repeated_accepted_call_cannot_reopen_the_completed_child(tool):
    parser = CodexEventParser()
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    call = {"id": "item_0", "type": "collab_tool_call", "tool": tool,
            "receiver_thread_ids": [child], "status": "completed",
            "agents_states": {child: {"status": "pending_init"}}}
    parser.parse({"type": "item.completed", "item": call})
    parser.parse({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "collab_tool_call", "tool": "wait",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "completed", "message": "Final answer."}}},
    })
    repeated = parser.parse({"type": "item.completed", "item": call}).events
    assert [event.type for event in repeated] == ["tool_result"]


@pytest.mark.parametrize(("native_status", "status", "reason"), [
    ("not_found", "unknown", "native_not_found"),
    ("errored", "failed", None),
    ("shutdown", "interrupted", "native_shutdown"),
])
def test_continuation_with_a_known_unavailable_target_does_not_claim_running(native_status, status, reason):
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    update = CodexEventParser().parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "send_input",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": native_status}}},
    }).events[-1].subagent
    assert update is not None and (update.status, update.stop_reason) == (status, reason)


def test_accepted_continuation_without_a_known_previous_run_is_unconfirmed():
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    update = CodexEventParser().parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "send_input",
                 "receiver_thread_ids": [child], "status": "completed", "agents_states": {}},
    }).events[-1].subagent
    assert update is not None and update.status == "unknown"


def test_repeated_old_wait_cannot_finish_an_explicit_new_execution():
    parser = CodexEventParser()
    child = "01a0ad62-b228-74d1-a2c3-1328858cca57"
    parser.parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
                 "receiver_thread_ids": [child], "status": "completed",
                 "agents_states": {child: {"status": "running"}}},
    })
    first_answer = {"id": "item_1", "type": "collab_tool_call", "tool": "wait",
                    "receiver_thread_ids": [child], "status": "completed",
                    "agents_states": {child: {"status": "completed", "message": "First answer."}}}
    parser.parse({"type": "item.completed", "item": first_answer})
    continued = parser.parse({
        "type": "item.completed",
        "item": {"id": "item_2", "type": "collab_tool_call", "tool": "send_input",
                 "receiver_thread_ids": [child], "status": "completed", "agents_states": {}},
    }).events[-1].subagent
    assert continued is not None and continued.status == "running"
    repeated = parser.parse({"type": "item.completed", "item": first_answer}).events
    assert [event.type for event in repeated] == ["tool_result"]


@pytest.mark.parametrize("receivers", [[None], [""], "child-thread", {"child-thread": True}])
def test_malformed_receivers_do_not_create_guessed_child_identity(receivers):
    events = CodexEventParser().parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
                 "receiver_thread_ids": receivers, "status": "completed", "agents_states": {}},
    }).events
    assert [event.type for event in events] == ["tool_result"]


def test_successful_spawn_with_an_exact_child_can_begin_running_without_a_status_patch():
    update = CodexEventParser().parse({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
                 "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
                 "status": "completed", "agents_states": {}},
    }).events[-1].subagent
    assert update is not None and update.status == "running"
