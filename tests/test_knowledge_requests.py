"""Knowledge request contracts at the admission/derive/return/edit seam."""

import pytest

from server.knowledge import (
    AdmittedRequest,
    admit,
    decode_owned,
    derive,
    encode,
    receive_return,
)


@pytest.mark.parametrize("prompt", [
    "/wiki 维护一下知识库",
    "/wiki 维护本地知识库",
    "/wiki 维护个人知识库",
    "/wiki 如何维护知识库？",
    "/wiki Please review the notes I just added.",
])
def test_wiki_source_selection_leaves_the_action_to_the_agent_without_imposing_read_only(prompt):
    context = admit(session_id="human", request_id="current", user_id="owner", prompt=prompt)
    assert context.trusted_request == prompt
    assert context.sources == ("wiki", "web")
    assert context.mode == "auto"
    assert context.maintenance.may_submit is True
    restricted = admit(session_id="human", request_id="current", user_id="owner", prompt=prompt, selected="read")
    assert restricted.sources == context.sources
    assert restricted.mode == "read"
    assert restricted.maintenance.may_submit is False


def test_explicit_read_keeps_local_and_web_requirements_without_submission_permission():
    context = admit(
        session_id="question-session", request_id="question-request", user_id="reader",
        prompt="/wiki Compare the methods using our notes and relevant evidence.",
        selected="read",
    )

    assert context.sources == ("wiki", "web")
    assert context.mode == "read"
    assert context.maintenance.may_submit is False


def test_web_subtask_returns_to_the_direct_parent_with_its_full_source_contract():
    parent = admit(
        session_id="parent", request_id="parent-request", user_id="reader",
        prompt="/wiki Compare X and Y.",
    )
    task = derive(AdmittedRequest(parent.origin, parent), "Investigate external evidence.", sources=("web",))

    assert task.execution.sources == ("web",)
    returned = receive_return(task, session_id="parent", user_id="reader", work_id=None)
    assert returned == parent
    assert returned.sources == ("wiki", "web")


def test_owned_contract_round_trip_survives_parent_end_and_unknown_versions_are_not_tasks():
    parent = admit(session_id="parent", request_id="request", user_id="reader", prompt="/wiki Explain X.")
    task = derive(AdmittedRequest(parent.origin, parent), "Read the external source.")

    restored = decode_owned(encode(task))
    assert receive_return(restored, session_id="parent", user_id="reader", work_id=None) == parent
    assert decode_owned('{"version":999,"value":{}}') is None


def test_nested_child_returns_to_its_direct_parent_without_borrowing_root_limits():
    root = admit(session_id="root", request_id="r", user_id="reader", prompt="A normal request")
    first = derive(AdmittedRequest(root.origin, root), "/wiki maintain Library/X.md")
    caller = AdmittedRequest(type(root.origin)("child", "a", "reader"), first.execution, "child-work")
    nested = derive(caller, "/wiki maintain Library/Y.md")

    assert nested.execution.maintenance.required_selections == (
        "/wiki maintain Library/X.md", "/wiki maintain Library/Y.md",
    )
    assert receive_return(nested, session_id="root", user_id="reader", work_id=None) is None
    assert receive_return(nested, session_id="child", user_id="reader", work_id="child-work") == first.execution


def test_worker_can_delegate_queries_but_cannot_create_a_submitter():
    worker = admit(session_id="worker", request_id="w", user_id="reader",
                   prompt="/wiki maintain Library/X.md", worker=True)
    child = derive(AdmittedRequest(worker.origin, worker), "/wiki maintain Library/X.md")
    assert child.execution.mode == "read"
    assert child.execution.maintenance.may_submit is False


def test_definition_edit_preserves_field_omission_and_rejects_ambiguous_null():
    from server.knowledge import revise

    existing = admit(session_id="origin", request_id="o", user_id="reader", prompt="/wiki Explain X.", selected="read")
    edit = admit(session_id="editor", request_id="e", user_id="reader", prompt="/wiki maintain Library/Y.md")
    caller = AdmittedRequest(edit.origin, edit)
    revised = revise(existing, caller, {"prompt": "/wiki maintain Library/Y.md"}, human=True)
    assert revised.maintenance == existing.maintenance
    assert revised.sources == existing.sources
    assert revised.mode == "read"
    with pytest.raises(ValueError, match="null"):
        revise(existing, caller, {"wiki_mode": None}, human=True)


def test_definition_agent_edit_narrows_but_explicit_human_control_can_reauthorize():
    from server.knowledge import revise

    existing = admit(session_id="origin", request_id="o", user_id="reader", prompt="/wiki Explain X.", selected="read")
    edit = admit(session_id="editor", request_id="e", user_id="reader", prompt="General task")
    caller = AdmittedRequest(edit.origin, edit)
    agent = revise(existing, caller, {"prompt": "/wiki maintain Library/Y.md", "wiki_mode": "auto"})
    assert agent.mode == "read" and not agent.maintenance.may_submit
    human = revise(existing, caller, {"prompt": "General task", "wiki_mode": "auto"}, human=True)
    assert human.mode == "auto" and human.maintenance.may_submit


def test_unknown_definition_cannot_be_reconstructed_by_agent_edit():
    from server.knowledge import revise

    edit = admit(session_id="editor", request_id="e", user_id="reader", prompt="General task")
    with pytest.raises(ValueError, match="contract"):
        revise(None, AdmittedRequest(edit.origin, edit), {"prompt": "Recover previous task"})


def test_agent_definition_edit_adds_its_further_selection_to_the_common_limit():
    from server.knowledge import revise

    existing = admit(session_id="origin", request_id="o", user_id="reader", prompt="Normal scheduled task")
    editor = admit(session_id="editor", request_id="e", user_id="reader", prompt="Normal current task")
    revised = revise(existing, AdmittedRequest(editor.origin, editor), {
        "prompt": "/wiki maintain Library/X.md", "wiki_mode": "maintain",
    })
    assert revised.maintenance.required_selections == ("/wiki maintain Library/X.md",)


def test_explicit_auto_control_does_not_remove_a_childs_more_specific_target_selection():
    existing = admit(session_id="origin", request_id="o", user_id="reader", prompt="Normal task")
    derived = derive(AdmittedRequest(existing.origin, existing), "/wiki maintain Library/X.md", selected="auto")
    assert derived.execution.mode == "auto"
    assert derived.execution.maintenance.required_selections == ("/wiki maintain Library/X.md",)


def test_human_maintenance_control_edit_preserves_untouched_sources_and_constraints():
    from server.knowledge import revise

    original = admit(session_id="origin", request_id="o", user_id="reader", prompt="/wiki Explain X; do not make new web requests.")
    editor = admit(session_id="editor", request_id="e", user_id="reader", prompt="Current scheduled task")
    existing = revise(original, AdmittedRequest(editor.origin, editor), {"prompt": "Current scheduled task"}, human=True)
    revised = revise(existing, AdmittedRequest(editor.origin, editor), {"wiki_mode": "auto"}, human=True)
    assert revised.sources == original.sources
    assert "do not make new web requests" in revised.trusted_request
    assert revised.mode == "auto" and revised.maintenance.may_submit


def test_nested_source_constraints_are_rendered_and_return_does_not_inherit_the_child_scope():
    from server.knowledge import RequestRef, render

    root = admit(session_id="root", request_id="r", user_id="reader", prompt="/wiki Compare X and Y")
    first = derive(AdmittedRequest(root.origin, root), "Use local notes only; do not make new web requests.")
    nested = derive(AdmittedRequest(RequestRef("child", "c", "reader"), first.execution), "Verify the detail of X")
    assert "Use local notes only; do not make new web requests." in render(nested.execution)
    assert receive_return(first, session_id="root", user_id="reader", work_id=None) == root


@pytest.mark.parametrize("changes", [{"wiki_mode": "read"}, {"name": "Current plan"}])
def test_model_edit_without_body_inherits_callers_concrete_source_limits(changes):
    from server.knowledge import render, revise

    existing = admit(session_id="origin", request_id="o", user_id="reader", prompt="/wiki Compare X")
    caller = admit(session_id="editor", request_id="e", user_id="reader",
                   prompt="This plan must use local notes only; do not make new web requests.")
    child = derive(AdmittedRequest(caller.origin, caller), "Preserve this further local-source restriction")
    revised = revise(existing, AdmittedRequest(caller.origin, child.execution), changes)
    assert "This plan must use local notes only; do not make new web requests." in render(revised)
    assert "Preserve this further local-source restriction" in render(revised)
