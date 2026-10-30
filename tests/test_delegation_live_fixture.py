"""Live delegation capture preserves the current admission/receipt contract."""

from tests import test_delegations_real as live_tests
from tests import test_question_lifecycle as question_tests

question_host = question_tests.question_host


async def test_parent_capture_returns_committed_receipt_and_forwards_child_keywords(question_host, monkeypatch):
    manager, parent, _client, _events = question_host
    child = await manager.create_session(parent.agent_id, "Child capture")
    forwarded = []
    child_receipt = object()

    async def actual_start(sid, prompt, attachment_ids=None, **kwargs):
        forwarded.append((sid, prompt, attachment_ids, kwargs))
        return child_receipt

    monkeypatch.setattr(manager, "start_message", actual_start)
    captured = live_tests._intercept_parent_injections(manager, parent.id)
    keywords = {"source": "delegation", "work_id": "child-work", "knowledge_context": None}
    assert await manager.start_message(child.id, "Child request", ["attachment"], **keywords) is child_receipt
    assert forwarded == [(child.id, "Child request", ["attachment"], keywords)]

    receipt = await manager.start_message(parent.id, "[agent-reply:Vera] PONG",
                                          source="delegation", delivery_id="reply-work", knowledge_context=None)
    assert receipt.accepted
    completion = await receipt.wait()
    assert completion.input_request_id == receipt.request_id
    assert completion.recording_state == "committed"
    assert completion.outcome.status == "success"
    assert captured == [(parent.id, "[agent-reply:Vera] PONG")]
    assert len(forwarded) == 1, "capturing a parent reply must not launch the parent model"
