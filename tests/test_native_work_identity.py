"""Captured native waits keep their work through binding and later rounds."""

import pytest

from server.harness.native_output import NativeOutput


def spawn():
    return "item/completed", {"threadId": "root", "turnId": "root-turn", "item": {
        "id": "spawn", "type": "subAgentActivity", "kind": "started", "agentThreadId": "child", "agentPath": "/root/helper"}}


def waiting(identifier="wait", *, completed=False):
    return "item/completed" if completed else "item/started", {"threadId": "root", "turnId": "root-turn", "item": {
        "id": identifier, "type": "collabAgentToolCall", "tool": "wait", "receiverThreadIds": ["child"],
        "agentsStates": {"child": {"status": "completed"}} if completed else {}}}


def child(turn, *, completed=False):
    return "turn/completed" if completed else "turn/started", {"threadId": "child", "turn": {
        "id": turn, "status": "completed" if completed else "inProgress", "rootTurnId": "root-turn"}}


@pytest.mark.parametrize("direct_terminal", ["before_wait", "after_wait", "absent"])
def test_old_pending_wait_can_complete_only_its_original_round(direct_terminal):
    output = NativeOutput("request", "attempt", "root", "root-turn")
    events = []
    for frame in [spawn(), waiting(), child("first")]:
        events.extend(output.observe(*frame))
    original_card = events[0].subagent.card_id
    if direct_terminal == "before_wait":
        events.extend(output.observe(*child("first", completed=True)))
    events.extend(output.observe(*child("second")))
    waiting_events = output.observe(*waiting(completed=True))
    assert all(event.subagent.card_id == original_card and event.subagent.native_turn_id == "first" for event in waiting_events)
    events.extend(waiting_events)
    if direct_terminal == "after_wait":
        assert output.observe(*child("first", completed=True)) == []
    events.extend(output.retire("parent_done", reconcile=True))
    cards = {event.subagent.card_id: event.subagent for event in events}
    assert len(cards) == 2
    assert (cards[original_card].status, cards[original_card].native_turn_id) == ("completed", "first")
    second = next(card for card in cards.values() if card.native_turn_id == "second")
    assert (second.status, second.stop_reason) == ("interrupted", "parent_done")


def test_wait_that_finishes_before_child_binding_retains_its_completed_card():
    output = NativeOutput("request", "attempt", "root", "root-turn")
    events = []
    for frame in [spawn(), waiting(), waiting(completed=True), child("first")]:
        events.extend(output.observe(*frame))
    events.extend(output.retire("parent_done", reconcile=True))
    assert len({event.subagent.card_id for event in events}) == 1
    assert (events[-1].subagent.status, events[-1].subagent.native_turn_id) == ("completed", "first")
    assert events[-1].runtime_metadata["native"]["thread_id"] == "child"
    assert events[-1].runtime_metadata["native"]["turn_id"] == "first"


def test_two_waits_and_direct_terminal_converge_on_the_same_bound_work():
    output = NativeOutput("request", "attempt", "root", "root-turn")
    events = []
    for frame in [spawn(), waiting("one"), waiting("two"), child("first"), child("first", completed=True)]:
        events.extend(output.observe(*frame))
    assert output.observe(*waiting("one", completed=True)) == []
    assert output.observe(*waiting("two", completed=True)) == []
    assert output.retire("parent_done") == []
    assert len({event.subagent.card_id for event in events}) == 1
    assert events[-1].subagent.status == "completed"
