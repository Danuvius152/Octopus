"""Original large messages for public HTTP/WS and browser replay contracts."""

from server.models import MessageContent, TurnOutcome, TurnRecovery


def control_sample(kind):
    root_kib = {"missing_final": 16, "unavailable_question": 128, "unavailable_small_question": 300}.get(kind, 26)
    root = "n" * (root_kib * 1024)
    metadata = {"request_id": "request", "attempt_id": "attempt", "native": {
        "thread_id": root, "turn_id": "turn", "root_thread_id": root, "root_turn_id": "turn",
        "item_id": "event", "phase": "final_answer"}}
    inputs = {"attempt_context": {"request_id": "request", "attempt_id": "attempt"},
        "rejected_input": {"content": "d" * (12 * 1024), "attachment_ids": []}}
    prefix = []
    if kind == "recovery":
        body = TurnRecovery(request_id="request", phase="retrying", code="transient_error",
            message="Recovery detail " * 20_000, attempt=1, maximum=2, delay=0.5).model_dump(mode="json")
        row = MessageContent(role="system", type="recovery", seq=0, content=body, tool_input=inputs, runtime_metadata=metadata)
    elif kind == "async_question":
        body = "Original asynchronous question " * 20_000
        inputs = {"native_item": {"type": "agentMessage", "text": body,
            "questions": [{"title": "Choose an original direction " * 10_000}]}}
        row = MessageContent(role="assistant", type="text", seq=0, content=body, tool_input=inputs, runtime_metadata=metadata)
    elif kind == "missing_final":
        final_metadata = {**metadata, "native": {**metadata["native"], "item_id": "saved-final"}}
        prefix = [MessageContent(role="assistant", type="text", seq=0, content="Saved final candidate", runtime_metadata=final_metadata)]
        body = TurnOutcome(request_id="request", status="success", message="Completion diagnostic " * 20_000,
            final_answer={"thread_id": root, "turn_id": "turn", "message_seqs": [0]},
            recording_integrity="incomplete", recording_issues={"native_final_missing": 1}, recovery={"transient": 2}).model_dump(mode="json")
        row = MessageContent(role="system", type="result", seq=1, content=body, tool_input=inputs, runtime_metadata=metadata)
    elif kind in {"unavailable_question", "unavailable_small_question"}:
        inputs = {"request_id": "request", "attempt_id": "attempt", "questions": [{
            "header": "Original unavailable topic", "question": "Original unavailable question",
            "options": [{"label": "Original unavailable option", "description": "Original option description " * 2_000}]}]}
        if kind == "unavailable_small_question":
            inputs["questions"][0]["options"][0]["description"] = "Original short description"
            inputs["native_question"] = {"question_id": "original-question", "connection_generation": "connection",
                "server_request_id": "native-request", "request_id": "request", "attempt_id": "attempt",
                "thread_id": root, "turn_id": "turn", "item_id": "event", "question_ids": ["original-option"], "blocking": True}
            metadata["native"] = {**metadata["native"], "thread_id": "small-thread", "root_thread_id": "small-thread"}
        row = MessageContent(role="assistant", type="question_request", seq=0, tool_use_id="original-question",
            tool_input=inputs, runtime_metadata=metadata)
    else:
        raise ValueError(kind)
    return prefix, row, root
