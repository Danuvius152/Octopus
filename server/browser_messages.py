"""Disposable browser views; the message store and internal bus stay complete."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from .harness.codex_items import COMMAND_OUTPUT_NOTICE
from .models import (
    BrowserMessageContent,
    BrowserMessagePage,
    BrowserNativeDetailPage,
    BrowserPendingQuestionInfo,
    BrowserSessionDetail,
    BrowserSubagentRun,
    MessageContent,
    MessagePage,
    MessageRole,
    NativeDetailPage,
    PendingQuestionInfo,
    SessionDetail,
    SubagentRun,
)

MESSAGE_BYTES = 64 * 1024
EVENT_BYTES = 256 * 1024
COLLECTION_BYTES = 2 * 1024 * 1024
_STRUCTURED = "Structured content omitted from preview. Open the full saved message."


def _control_note(kind: str | None, body: Any) -> str:
    if kind in {"result", "recovery"} and isinstance(body, dict):
        status = body.get("status")
        if status in ("success", "failed", "interrupted", "needs_input", "unknown"):
            return f" Original {kind} status: {status}."
    return ""


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def text_preview(value: str, budget: int) -> str:
    if encoded_size(value) <= budget:
        return value
    low, high = 0, min(len(value), budget)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(value[:middle] + "…") <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low] + "…"


def _small_fields(value: dict[str, Any], budget: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if encoded_size({**result, key: item}) <= budget:
            result[key] = item
    return result


def _body_view(body: Any, kind: str, *, minimal: bool = False) -> Any:
    if isinstance(body, str):
        return text_preview(body, 512 if minimal else 24 * 1024)
    if not minimal and encoded_size(body) <= 24 * 1024:
        return body
    if isinstance(body, dict) and kind in {"result", "recovery"}:
        # Preserve the control structure, including future fields. Only these
        # known diagnostics are externalizable; size never selects the facts.
        result = dict(body)
        if isinstance(body.get("message"), str):
            result["message"] = text_preview(body["message"], 512 if minimal else 8 * 1024)
        if encoded_size(body.get("recording_locations")) > 4 * 1024:
            result.pop("recording_locations", None)
        if isinstance(body.get("compact"), dict):
            compact = body["compact"]
            result["compact"] = dict(compact)
            if isinstance(compact.get("reason"), str):
                result["compact"]["reason"] = text_preview(compact["reason"], 512 if minimal else 4 * 1024)
        return result
    return None if body is None else _STRUCTURED


_CARD_TEXT = ("name", "description", "prompt", "summary", "stop_reason")
_CONTROL_INPUT_TYPES = {"result", "recovery", "question_request", "question_answer", "question_closed", "question_snoozed", "native_subagent"}


def _card_display(values: dict[str, Any], *, minimal: bool = False) -> dict[str, Any]:
    result = dict(values)
    for key in _CARD_TEXT:
        if isinstance(result.get(key), str):
            result[key] = text_preview(result[key], 512 if minimal else 4 * 1024)
    if isinstance(result.get("steps"), list):
        result["steps"] = [text_preview(step, 512 if minimal else 1024) for step in result["steps"][-(1 if minimal else 12):]]
    return result


def _input_parts(value: dict[str, Any] | None, kind: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Separate control facts once; later passes only spend display bytes."""
    if value is None:
        return None, {}
    result = dict(value)
    if isinstance(result.get("request_context"), dict):
        context = result["request_context"]
        result["request_context"] = {key: context[key] for key in ("source", "request_id", "attempt_id", "work_id") if key in context}
    if isinstance(result.get("native_item"), dict):
        result["native_item"] = {key: item for key, item in result["native_item"].items()
            if key not in {"text", "content", "arguments", "output", "aggregatedOutput", "result"}}
    if kind in _CONTROL_INPUT_TYPES:
        display_keys = (*_CARD_TEXT, "steps") if kind == "native_subagent" else ("questions", "rejected_input")
        display = {key: result.pop(key) for key in display_keys if key in result}
        controls = result
        if isinstance(controls.get("question_resolution"), dict):
            # The canonical answer is content. Resolution identity/disposition
            # remains complete even when its duplicate answer body is large.
            controls["question_resolution"] = {key: item for key, item in controls["question_resolution"].items() if key != "answers"}
    else:
        controls = {key: result.pop(key) for key in ("request_context", "attempt_context", "native_steer", "async_questions_preview") if key in result}
        display = result
    native = value.get("native_item")
    questions = native.get("questions") if isinstance(native, dict) and native.get("type") == "agentMessage" else None
    if isinstance(questions, list) and questions and all(
            isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip() for item in questions):
        controls["async_questions_preview"] = True
    return controls, display


def _input_view(controls: dict[str, Any] | None, display: dict[str, Any], kind: str, *, minimal: bool = False) -> dict[str, Any] | None:
    if controls is None:
        return None
    if kind == "native_subagent":
        shown = _card_display(display, minimal=minimal)
        return {**shown, **controls, "display_truncated": shown != display or bool(controls.get("display_truncated"))}
    budget = 512 if minimal else 16 * 1024
    shown = display if encoded_size(display) <= budget else {**_small_fields(display, budget - 256), "preview": _STRUCTURED}
    return {**shown, **controls}


def record_view(message: MessageContent) -> MessageContent:
    """Derive browser-only text/arguments before spending preview bytes."""
    inputs = message.tool_input
    capture = inputs.get("output_capture") if inputs else None
    if (message.type == "tool_result" and isinstance(capture, dict) and capture.get("state") == "head_tail"
            and isinstance(message.content, str) and message.content.startswith(COMMAND_OUTPUT_NOTICE)):
        return message.model_copy(update={"content": message.content[len(COMMAND_OUTPUT_NOTICE):]})
    native = message.runtime_metadata.native if message.runtime_metadata else None
    item = inputs.get("native_item") if inputs else None
    if message.type == "tool_use" and inputs and native and native.item_id and isinstance(item, dict) and item.get("id") == native.item_id:
        parameters = {key: value for key, value in inputs.items() if key != "native_item"}
        # A native-only record may be the sole business payload.
        if parameters:
            return message.model_copy(update={"tool_input": parameters})
    return message


def message_view(session_id: str, message: MessageContent) -> BrowserMessageContent:
    message = record_view(message)
    source = message.model_dump(mode="json")
    values = dict(source)
    input_parts = _input_parts(source["tool_input"], message.type)
    values["content"] = _body_view(values["content"], message.type)
    values["tool_input"] = _input_view(*input_parts, message.type)
    compared = dict(values)
    if values["tool_input"] is not None:
        compared["tool_input"] = dict(values["tool_input"])
        for derived in ("async_questions_preview", "display_truncated"):
            if derived not in (source["tool_input"] or {}):
                compared["tool_input"].pop(derived, None)
    preview = compared != source
    values.update(body_state="preview" if preview else "inline", detail_ref=None)
    if preview and message.seq is not None:
        values["detail_ref"] = f"/api/sessions/{quote(session_id, safe='')}/messages/{message.seq}"
    view = BrowserMessageContent.model_validate(values)
    if encoded_size(view.model_dump(mode="json")) > MESSAGE_BYTES:
        # Spend display bytes only after the original associations fit. This
        # second, smaller body can rescue a large but representable identity.
        values["content"] = _body_view(source["content"], message.type, minimal=True)
        values["tool_input"] = _input_view(*input_parts, message.type, minimal=True)
        values["body_state"] = "preview"
        values["detail_ref"] = f"/api/sessions/{quote(session_id, safe='')}/messages/{message.seq}" if message.seq is not None else None
        view = BrowserMessageContent.model_validate(values)
    # Original provider IDs have no size invariant. Never truncate an ID and
    # pretend its associations survived; retain the full row behind the ref.
    if encoded_size(view.model_dump(mode="json")) > MESSAGE_BYTES:
        view = BrowserMessageContent(role=message.role, type="notice", seq=message.seq,
            body_state="preview", identity_unavailable=True,
            content="This message's identity exceeds browser capacity. Open the full saved message to inspect it."
                + _control_note(message.type, message.content),
            detail_ref=f"/api/sessions/{quote(session_id, safe='')}/messages/{message.seq}" if message.seq is not None else None)
    return view


def _window(session_id: str, messages: Sequence[MessageContent], *, newest: bool) -> tuple[list[BrowserMessageContent], bool]:
    result: list[BrowserMessageContent] = []
    size = 2  # The JSON array's brackets, plus commas between members below.
    for message in reversed(messages) if newest else messages:
        view = message_view(session_id, message)
        cost = encoded_size(view.model_dump(mode="json")) + bool(result)
        if size + cost > COLLECTION_BYTES:
            break
        size += cost
        result.append(view)
    if newest:
        result.reverse()
    return result, len(result) < len(messages)


def session_view(detail: SessionDetail) -> BrowserSessionDetail:
    messages, clipped = _window(detail.id, detail.messages, newest=True)
    return BrowserSessionDetail(**{**detail.model_dump(), "messages": messages,
        "pending_questions": [question_view(detail.id, question) for question in detail.pending_questions],
        "subagents": [subagent_view(child) for child in detail.subagents],
        "oldest_loaded_seq": messages[0].seq if messages else None,
        "has_more_messages": detail.has_more_messages or clipped})


def question_view(session_id: str, question: PendingQuestionInfo) -> BrowserPendingQuestionInfo:
    preview = encoded_size(question.questions) > 24 * 1024
    return BrowserPendingQuestionInfo(**{**question.model_dump(), "body_state": "preview" if preview else "inline",
        "questions": [] if preview else question.questions,
        "detail_ref": f"/api/sessions/{quote(session_id, safe='')}/messages/{question.message_seq}"
        if preview and question.message_seq is not None else None})


def subagent_view(child: SubagentRun) -> BrowserSubagentRun:
    values = _card_display(child.model_dump(mode="json"))
    values["display_truncated"] = values != child.model_dump(mode="json")
    return BrowserSubagentRun.model_validate(values)


def page_view(session_id: str, page: MessagePage) -> BrowserMessagePage:
    messages, clipped = _window(session_id, page.messages, newest=True)
    return BrowserMessagePage(messages=messages, oldest_loaded_seq=messages[0].seq if messages else None,
        has_more_messages=page.has_more_messages or clipped)


def native_view(session_id: str, page: NativeDetailPage) -> BrowserNativeDetailPage:
    messages, clipped = _window(session_id, page.messages, newest=False)
    return BrowserNativeDetailPage(**{**page.model_dump(), "messages": messages,
        "child": subagent_view(page.child),
        "next_after_seq": messages[-1].seq if messages else None, "has_more": page.has_more or clipped})


def unavailable_event(event: dict[str, Any], detail_ref: str | None = None) -> dict[str, Any]:
    sid, seq = event.get("session_id"), event.get("seq")
    # A committed question can exceed the identity budget even with an inline
    # body. Its saved reference depends on persistence, not body truncation.
    if (detail_ref is None and event.get("type") == "question_request"
            and isinstance(sid, str) and type(seq) is int and seq >= 0):
        detail_ref = f"/api/sessions/{quote(sid, safe='')}/messages/{seq}"
    result = {"type": "browser_unavailable", **({"session_id": sid} if isinstance(sid, str) else {}),
        "seq": seq if type(seq) is int and seq >= 0 else None,
        "message": "Some event data could not fit this browser view. Read the saved message or refresh session state; unsaved live data is unavailable."
            + _control_note(event.get("type"), event.get("content")),
        "detail_ref": detail_ref, "identity_unavailable": True}
    if encoded_size(result) > EVENT_BYTES:
        return {"type": "error", "message": "Event identity exceeds browser capacity. Refresh session state to inspect saved data."}
    return result


def event_view(event: dict[str, Any]) -> dict[str, Any]:
    """Called only by the browser subscription, never by the shared bus."""
    result = _event_view(event)
    return result if encoded_size(result) <= EVENT_BYTES else unavailable_event(event, result.get("detail_ref"))


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") == "assistant_delta":
        return {**event, "content": text_preview(event.get("content", ""), 32 * 1024)}
    if event.get("type") == "question_snoozed":
        # The existing pending identity already owns the question body. This
        # event only changes its live control facts, never its answer options.
        return {key: value for key, value in event.items() if key != "questions"}
    if event.get("type") == "question_request":
        pending = PendingQuestionInfo.model_validate({**event, "message_seq": event.get("seq")})
        return {**event, **question_view(event["session_id"], pending).model_dump(mode="json")}
    if event.get("type") == "subagent":
        result = _card_display(event)
        result["display_truncated"] = result != event
        # Prompt/summary are live hints, not part of the saved card at seq.
        # No detail_ref can claim to recover their omitted bytes.
        return result
    shapes = {
        "assistant_text": (MessageRole.assistant, "text", "content", "tool_input"),
        "user_message": (MessageRole.user, "text", "content", "tool_input"),
        "steered": (MessageRole.user, "text", "content", "tool_input"),
        "tool_use": (MessageRole.assistant, "tool_use", "content", "input"),
        "tool_result": (MessageRole.tool, "tool_result", "output", "tool_input"),
        "result": (MessageRole.system, "result", "content", "tool_input"),
        "recovery": (MessageRole.system, "recovery", "content", "tool_input"),
        "question_answer": (MessageRole.user, "question_answer", "content", "tool_input"),
        "question_closed": (MessageRole.system, "question_closed", "message", "tool_input"),
    }
    shape = shapes.get(event.get("type", ""))
    if shape is None:
        return event
    role, kind, body_field, input_field = shape
    view = message_view(event["session_id"], MessageContent(
        role=role, type=kind, content=event.get(body_field), seq=event.get("seq"),
        tool_name=event.get("tool"), tool_use_id=event.get("tool_use_id"),
        tool_input=event.get(input_field), runtime_metadata=event.get("runtime_metadata"),
        created_at=event.get("created_at"),
    ))
    if view.identity_unavailable:
        return unavailable_event(event, view.detail_ref)
    return {**event, body_field: view.content, input_field: view.tool_input,
        "runtime_metadata": view.runtime_metadata.model_dump() if view.runtime_metadata else None,
        "body_state": view.body_state, "detail_ref": view.detail_ref, "identity_unavailable": view.identity_unavailable}
