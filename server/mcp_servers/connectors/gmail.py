"""Gmail connector MCP server (connectors.md Phase C / §6.2).

Spawned as `python -m server.mcp_servers.connectors.gmail`. Mirrors the GitHub
server: per-call token from the host, projected + truncated results, 401 →
needs-reconnect, only safe reads retry. send_draft actually sends — the
system-prompt blurb requires the model to confirm with the user first.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email._header_value_parser import get_cfws, get_msg_id
from email.errors import HeaderParseError, ObsoleteHeaderDefect
from email.headerregistry import (
    AddressHeader,
    BaseHeader,
    ContentDispositionHeader,
    ContentTypeHeader,
)
from email.message import EmailMessage
from email.policy import default
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.mcp_servers.connectors._shared import (  # noqa: E402
    ConnectorContext,
    ConnectorTokenError,
    ResourceBudgetError,
    ResourceDecodeError,
    ResourceResult,
    array_data,
    decode_response,
    logical_resource,
    object_data,
    request_failure,
    request_with_retry,
    response_failure,
    text_data,
    to_text,
    token_failure_message,
    truncate,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s gmail-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-gmail")
ctx = ConnectorContext()

_API = "https://gmail.googleapis.com/gmail/v1"
_GET_BUDGET_SECONDS = 50.0
_BODY_MAX_BYTES = 256 * 1024
_BODY_MAX_EXTERNAL_READS = 4
_BODY_JSON_OVERHEAD = 1024
_BODY_TYPE_PRIORITY = {"text/plain": 3, "text/html": 2}


async def _api(
    method: str, path: str, *, retry_safe: bool = False,
    operation: str = "", check: str = "Check the original Gmail resource.",
    decode: Callable[[Any], Any], finish: Callable[[Any, ResourceResult], Awaitable[tuple[Any, str | None]]] | None = None,
    **kw: Any,
) -> tuple[Any, str | None]:
    """Return a fully projected resource or an explicit tool error."""
    with logical_resource(ctx) as result:
        try:
            info = await asyncio.to_thread(ctx.token_info)
        except ConnectorTokenError as token_error:
            result.error_code = "token"
            return {}, token_failure_message(token_error)
        headers = {"Authorization": f"Bearer {info['access_token']}"}
        url = path if path.startswith("http") else f"{_API}{path}"
        operation = operation or f"{method} {url}"
        r, failure = await request_with_retry(
            method, url, retry_safe=retry_safe, operation=operation,
            check=check, headers=headers, **kw,
        )
        if failure:
            result.error_code = failure.code
            return {}, failure.message
        assert r is not None
        if r.status_code == 401:
            result.error_code = "auth"
            authorization = await asyncio.to_thread(ctx.rejection_message, info)
            return {}, "Error: rejected: " + authorization.removeprefix("Error: ")
        error = response_failure(
            r, retry_safe=retry_safe, operation=operation, check=check,
            rejected_statuses=frozenset({400, 403, 404}),
        )
        if error:
            result.error_code = "http"
            return {}, error
        data, failure = decode_response(r, decode, provider="Gmail")
        if failure:
            result.error_code = failure.code
            return {}, request_failure(failure.message, retry_safe=retry_safe, operation=operation, check=check)
        result.error_code = None
        if finish is not None:
            # A later choice can consume inline data from this response.
            # External reads still settle their own resource calls.
            try:
                return await finish(data, result)
            except ResourceDecodeError as projection_error:
                result.error_code = projection_error.code
                reason = str(projection_error)
                if projection_error.code == "resource_decode":
                    reason = f"Invalid Gmail resource response: {reason}"
                return {}, request_failure(reason, retry_safe=retry_safe, operation=operation, check=check)
        return data, None


def _header(payload: dict, name: str) -> str:
    for entry in array_data(object_data(payload).get("headers", [])):
        h = object_data(entry)
        if text_data(h.get("name", "")).lower() == name.lower():
            return text_data(h.get("value", ""))
    return ""


@dataclass(eq=False)
class _TextPart:
    mime_type: str
    charset: str
    body: dict[str, Any]
    size: int | None
    attachment_id: str
    text: str | None = None

    @property
    def priority(self) -> int:
        return _BODY_TYPE_PRIORITY.get(self.mime_type, 1)

    @property
    def estimated_bytes(self) -> int:
        estimated = self.size or 0
        if not self.attachment_id and isinstance(encoded := self.body.get("data"), str):
            estimated = max(estimated, (len(encoded) - _base64_padding(encoded)) * 3 // 4)
        return estimated


@dataclass(frozen=True)
class _Alternatives:
    branches: list[_BodyPlan]
    invalid_branch: ResourceDecodeError | None


type _BodyPlan = list[_TextPart | _Alternatives]


def _alternatives(children: list[Any]) -> _BodyPlan:
    branches: list[_BodyPlan] = []
    invalid_branch = None
    for child in children:
        try:
            branch = _text_parts(child)
        except ResourceDecodeError as error:
            invalid_branch = invalid_branch or error
        else:
            if branch:
                branches.append(branch)
    if not branches:
        if invalid_branch is not None:
            raise invalid_branch
        return []
    if len(branches) == 1 and invalid_branch is None:
        return branches[0]
    return [_Alternatives(branches, invalid_branch)]


def _content_id(value: str) -> str:
    value = re.sub(r"\r?\n[ \t]+", " ", value).strip()
    if "<" not in value:
        return value
    try:
        token, tail = get_msg_id(value)
    except HeaderParseError:
        return ""
    # The stdlib leaves trailing comments unconsumed for IDs without a domain.
    _, tail = get_cfws(tail)
    return "" if tail else token.value.strip().removeprefix("<").removesuffix(">")


def _related_root(parts: list[Any], start: str | None) -> Any:
    if start is None:
        return parts[0]
    content_id = _content_id(start)
    matches = [part for part in parts if content_id and _content_id(_header(object_data(part), "Content-ID")) == content_id]
    if len(matches) != 1:
        raise ResourceDecodeError("multipart/related start must identify exactly one root part")
    return matches[0]


def _text_parts(value: Any, *, related_root: bool = False) -> _BodyPlan:
    part = object_data(value)
    headers = EmailMessage()
    try:
        for name in ("Content-Type", "Content-Disposition"):
            if raw := _header(part, name):
                headers[name] = re.sub(r"\r?\n[ \t]+", " ", raw)
    except ValueError as error:
        raise ResourceDecodeError("invalid MIME header") from error
    content_type = headers["Content-Type"]
    disposition = headers["Content-Disposition"]
    params = content_type.params if isinstance(content_type, ContentTypeHeader) else {}
    has_attachment_disposition = isinstance(disposition, ContentDispositionHeader) and (
        disposition.content_disposition != "inline" or bool(disposition.params.get("filename"))
    )
    if not related_root and (has_attachment_disposition or part.get("filename") or params.get("name")):
        return []

    header_type = content_type.content_type if isinstance(content_type, ContentTypeHeader) else "text/plain"
    mime_type = text_data(part.get("mimeType", "")).strip().lower() or header_type
    if mime_type.startswith("text/"):
        body = object_data(part.get("body"))
        attachment_id = text_data(body.get("attachmentId", ""))
        if attachment_id in (".", ".."):
            raise ResourceDecodeError("invalid external message body ID")
        size = _body_size(body)
        if (attachment_id and size == 0) or (not attachment_id and not size and body.get("data", "") == ""):
            return []
        return [_TextPart(mime_type, params.get("charset", "utf-8"), body, size, attachment_id)]
    if not mime_type.startswith("multipart/"):
        return []

    children = array_data(part.get("parts", []))
    if mime_type == "multipart/related":
        start = params.get("start")
        if not children and start is None:
            return []
        return _text_parts(_related_root(children, start), related_root=True)
    if mime_type == "multipart/alternative":
        return _alternatives(children)
    return [text for child in children for text in _text_parts(child)]


def _body_size(body: dict[str, Any]) -> int | None:
    if "size" not in body:
        return None
    size = body["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ResourceDecodeError("invalid message body size")
    return size


def _check_body_bytes(size: int, remaining: int) -> None:
    if size > remaining:
        raise _body_byte_limit()


def _body_byte_limit() -> ResourceBudgetError:
    return ResourceBudgetError(f"Gmail message body byte budget ({_BODY_MAX_BYTES} bytes) exhausted")


def _base64_padding(encoded: str) -> int:
    return 2 if encoded.endswith("==") else int(encoded.endswith("="))


def _decode_body(
    body: dict[str, Any], charset: str, remaining: int, *, expected_size: int = 0,
) -> tuple[str, int]:
    declared_size = _body_size(body)
    size = max(declared_size or 0, expected_size)
    encoded = text_data(body.get("data", ""))
    if not encoded:
        if size:
            raise ResourceDecodeError("message body bytes are missing")
        return "", 0

    padding = _base64_padding(encoded)
    unpadded = len(encoded) - padding
    required_padding = -unpadded % 4
    _check_body_bytes(max(size, unpadded * 3 // 4), remaining)
    if (required_padding == 3 or padding not in (0, required_padding)
            or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", encoded)):
        raise ResourceDecodeError("could not decode message body: invalid base64url")
    try:
        data = base64.b64decode(encoded + "=" * (required_padding - padding), altchars=b"-_", validate=True)
    except ValueError as error:
        raise ResourceDecodeError("could not decode message body: invalid base64url") from error
    if len(data) < expected_size or (declared_size is not None and len(data) != declared_size):
        raise ResourceDecodeError("message body size does not match returned data")
    try:
        text = data.decode(charset)
        text.encode("utf-8")
    except (LookupError, UnicodeError) as error:
        raise ResourceDecodeError("could not decode message body using its charset") from error
    return text, len(data)


def _read_external_body(value: Any, part: _TextPart, remaining: int) -> tuple[str, int]:
    body = object_data(value)
    if "data" not in body and "size" not in body:
        raise ResourceDecodeError("external message body bytes are missing")
    return _decode_body(body, part.charset, remaining, expected_size=part.size or 0)


def _message_listing(value: Any) -> dict:
    data = object_data(value)
    ids = [text_data(object_data(ref).get("id")) for ref in array_data(data.get("messages", []) or [])]
    return {"ids": ids, "has_more": bool(data.get("nextPageToken"))}


def _message_headers(value: Any, *, full: bool = False) -> dict:
    msg = object_data(value)
    payload = object_data(msg.get("payload") if full else msg.get("payload", {}))
    out = {
        "id": msg.get("id"), "thread_id": msg.get("threadId"),
        "from": _header(payload, "From"), "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
    }
    if full:
        out["to"] = _header(payload, "To")
    else:
        out["snippet"] = msg.get("snippet", "")
    return out


def _full_message(value: Any) -> tuple[dict, _BodyPlan]:
    message = _message_headers(value, full=True)
    return message, _text_parts(value["payload"])


@dataclass
class _BodyBudget:
    bytes_left: int
    reads_left: int


@dataclass
class _BodyProbe:
    part: _TextPart
    can_invalidate_branch: bool
    recheck_after_read: bool


@dataclass
class _BodySelection:
    parts: list[_TextPart]
    required: list[_TextPart]
    probe_options: list[list[_BodyProbe]]


def _select_alternative(group: _Alternatives) -> _BodySelection:
    best = _BodySelection([], [], [])
    preferred = None
    invalid = group.invalid_branch
    for branch in group.branches:
        try:
            option = _select_body(branch)
        except ResourceDecodeError as error:
            invalid = invalid or error
            continue
        lead = max(option.parts, key=lambda part: part.priority, default=None)
        if lead is not None and (preferred is None or lead.priority > preferred.priority):
            best, preferred = option, lead
    if preferred is None:
        if invalid is not None:
            raise invalid
        return best
    structural_probes = [
        probe for options in best.probe_options for probe in options if probe.can_invalidate_branch
    ]
    if preferred.text is None:
        can_invalidate = invalid is not None or bool(structural_probes)
        more_to_read = any(part is not preferred and part.text is None for part in best.parts)
        lead_probe = _BodyProbe(preferred, can_invalidate, can_invalidate or more_to_read)
        structural_probes = [lead_probe, *(probe for probe in structural_probes if probe.part is not preferred)]
    if structural_probes:
        # Either confirming the lead or checking a nested dependency can
        # advance selection. Keep both options until the budget is known.
        structural_probes.sort(key=lambda probe: bool(probe.part.attachment_id))
        return _BodySelection(best.parts, [], [structural_probes])
    return best


def _select_body(plan: _BodyPlan) -> _BodySelection:
    selection = _BodySelection([], [], [])
    for part in plan:
        if isinstance(part, _Alternatives):
            branch = _select_alternative(part)
            selection.parts.extend(branch.parts)
            selection.required.extend(branch.required)
            selection.probe_options.extend(branch.probe_options)
        elif part.text != "":
            selection.parts.append(part)
            if part.text is None:
                selection.required.append(part)
    return selection


def _body_read_limit() -> ResourceBudgetError:
    return ResourceBudgetError(f"Gmail external body request budget ({_BODY_MAX_EXTERNAL_READS} reads) exhausted")


def _minimum_probe_bytes(probe_options: list[list[_BodyProbe]], reads_available: int) -> list[list[int]]:
    # Each row is the minimum byte cost of the remaining independent choices
    # at each request allowance. The hard request cap keeps these rows small.
    costs = [[0] * (reads_available + 1)]
    too_large = _BODY_MAX_BYTES + 1
    for options in reversed(probe_options):
        inline = min((probe.part.estimated_bytes for probe in options if not probe.part.attachment_id), default=too_large)
        external = min((probe.part.estimated_bytes for probe in options if probe.part.attachment_id), default=too_large)
        following = costs[-1]
        costs.append([inline + following[0], *(
            min(inline + following[reads], external + following[reads - 1])
            for reads in range(1, reads_available + 1)
        )])
    costs.reverse()
    return costs


def _choose_body_probe(
    options: list[_BodyProbe], available_bytes: int, available_reads: int, following_costs: list[int],
) -> tuple[_BodyProbe, int]:
    for probe in options:
        reads = int(bool(probe.part.attachment_id))
        if reads <= available_reads:
            available = available_bytes - following_costs[available_reads - reads]
            if probe.part.estimated_bytes <= available:
                return probe, available
    raise _body_byte_limit()


async def _read_body_part(
    path: str, part: _TextPart, available: int, budget: _BodyBudget, message_call: ResourceResult,
) -> str | None:
    if part.attachment_id:
        budget.reads_left -= 1
        with message_call.independent_call():
            result, error = await _api(
                "GET", f"{path}/attachments/{quote(part.attachment_id, safe='')}",
                retry_safe=True, operation="read external Gmail message body",
                decode=partial(_read_external_body, part=part, remaining=available),
                max_response_bytes=4 * ((available + 2) // 3) + _BODY_JSON_OVERHEAD,
            )
        if error:
            return error
        part.text, size = result
    else:
        part.text, size = _decode_body(part.body, part.charset, available)
    budget.bytes_left -= size
    return None


async def _finish_message(
    result: tuple[dict, _BodyPlan], message_call: ResourceResult, *, path: str,
) -> tuple[dict, str | None]:
    message, plan = result
    budget = _BodyBudget(_BODY_MAX_BYTES, _BODY_MAX_EXTERNAL_READS)
    while True:
        selection = _select_body(plan)
        reserved_reads = sum(bool(part.attachment_id) for part in selection.required)
        forced_probe_reads = sum(all(probe.part.attachment_id for probe in options) for options in selection.probe_options)
        if reserved_reads + forced_probe_reads > budget.reads_left:
            raise _body_read_limit()
        reserved_bytes = sum(part.estimated_bytes for part in selection.required)
        read_allowance = budget.reads_left - reserved_reads
        probe_costs = _minimum_probe_bytes(selection.probe_options, read_allowance)
        _check_body_bytes(reserved_bytes + probe_costs[0][read_allowance], budget.bytes_left)
        for options, following_costs in zip(selection.probe_options, probe_costs[1:], strict=True):
            probe, available = _choose_body_probe(
                options, budget.bytes_left - reserved_bytes, budget.reads_left - reserved_reads, following_costs,
            )
            error = await _read_body_part(path, probe.part, available, budget, message_call)
            if error:
                return {}, error
            if not probe.part.text or probe.recheck_after_read:
                break
        else:
            for part in selection.required:
                reserved_bytes -= part.estimated_bytes
                error = await _read_body_part(path, part, budget.bytes_left - reserved_bytes, budget, message_call)
                if error:
                    return {}, error
            message["body"] = "\n".join(part.text for part in selection.parts if part.text)
            return message, None


def _labels(value: Any) -> list[dict]:
    return [{"id": lbl.get("id"), "name": lbl.get("name")}
            for lbl in map(object_data, array_data(object_data(value).get("labels", [])))]


@mcp.tool(name="search")
async def search(query: str, limit: int = 10) -> str:
    """Search messages with Gmail query syntax (e.g. 'from:alice is:unread
    newer_than:7d'). Returns brief headers + snippet per match."""
    listing, err = await _api(
        "GET", "/users/me/messages", retry_safe=True, decode=_message_listing,
        params={"q": query, "maxResults": min(limit, 25)}
    )
    if err:
        return err
    out = []
    failures = []
    for message_id in listing["ids"]:
        msg, merr = await _api(
            "GET",
            f"/users/me/messages/{message_id}",
            retry_safe=True, decode=_message_headers,
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "Date"],
            },
        )
        if merr:
            failures.append({"id": message_id, "error": merr})
            continue
        out.append(msg)
    if failures:
        return truncate(to_text({"messages": out, "complete": False, "failures": failures,
                                 "matched_in_page": len(listing["ids"]),
                                 "has_more": listing["has_more"]}))
    return truncate(to_text(out))


@mcp.tool(name="get")
async def get(message_id: str) -> str:
    """Full message headers and decoded body (plain text preferred to HTML)."""
    if message_id in ("", ".", ".."):
        return "Error: invalid Gmail message ID."
    path = f"/users/me/messages/{quote(message_id, safe='')}"
    try:
        async with asyncio.timeout(_GET_BUDGET_SECONDS):
            message, error = await _api(
                "GET", path, retry_safe=True, decode=_full_message,
                finish=partial(_finish_message, path=path), params={"format": "full"},
            )
            return error or truncate(to_text(message))
    except TimeoutError:
        return request_failure(
            f"Gmail message read budget ({_GET_BUDGET_SECONDS:g}s) exhausted",
            retry_safe=True, operation="read Gmail message", check="Check the original Gmail message.",
        )


@mcp.tool(name="list_labels")
async def list_labels() -> str:
    """The account's labels (id + name) — needed for label/unlabel."""
    data, err = await _api("GET", "/users/me/labels", retry_safe=True, decode=_labels)
    if err:
        return err
    return truncate(to_text(data))


def _usable_reply_header(header: BaseHeader | None) -> bool:
    # Received mail may use RFC 2822 legacy syntax for IDs or display names.
    # Obsolete syntax is interpretable; actual parse errors are not.
    return header is not None and not any(not isinstance(defect, ObsoleteHeaderDefect) for defect in header.defects)


def _validate_reference_history(value: str) -> None:
    # Python 3.12 treats References as unstructured text. Use the same msg-id
    # tokenizer as headerregistry on all supported versions, rather than
    # adding our own RFC grammar or trusting version-dependent diagnostics.
    remaining = value.strip()
    try:
        while remaining:
            token, remaining = get_msg_id(remaining)
            if any(not isinstance(defect, ObsoleteHeaderDefect) for defect in token.all_defects):
                raise ValueError("original References is invalid")
    except HeaderParseError as error:
        raise ValueError("original References is invalid") from error


def _reply_metadata(data: Any, message_id: str) -> tuple[str, EmailMessage]:
    """Keep Gmail's object identity separate from the original mail headers."""
    if (not isinstance(data, dict) or data.get("id") != message_id
            or not isinstance(data.get("threadId"), str) or not data["threadId"].strip()):
        raise ValueError("original message identity or threadId is missing or invalid")
    payload = data.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("headers"), list):
        raise ValueError("original message headers are missing")
    headers = EmailMessage()
    selected = {"message-id", "references", "in-reply-to", "subject", "reply-to", "from"}
    for entry in payload["headers"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("invalid original message header")
        if entry["name"].lower() in selected:
            if not isinstance(entry.get("value"), str):
                raise ValueError(f"invalid original {entry['name']} header")
            # Unfold only legal continuation lines; bare CR/LF stays invalid.
            value = re.sub(r"\r?\n[ \t]+", " ", entry["value"])
            if entry["name"].lower() == "references":
                _validate_reference_history(value)
            headers[entry["name"]] = value
    if not _usable_reply_header(headers["Message-ID"]):
        raise ValueError("original RFC Message-ID is missing or invalid")
    if "Subject" not in headers:
        raise ValueError("original Subject is missing")
    return data["threadId"], headers


def _reply_resource(data: Any, message_id: str, *, infer_recipient: bool) -> tuple[str, str, str | None, str, str]:
    """Decode the original mail fields this particular reply will consume."""
    try:
        thread_id, headers = _reply_metadata(data, message_id)
        recipient = None
        if infer_recipient:
            original = headers["Reply-To"] if "Reply-To" in headers else headers["From"]
            if (not _usable_reply_header(original)
                    or not isinstance(original, AddressHeader) or not original.addresses):
                raise ValueError("original Reply-To/From recipient is missing or invalid")
            recipient = str(original)
        parent_id = str(headers["Message-ID"])
        history = str(headers.get("References", ""))
        if not history and headers.get("In-Reply-To"):
            previous = default.header_factory("Message-ID", str(headers["In-Reply-To"]))
            if _usable_reply_header(previous):
                history = str(previous)
        return thread_id, str(headers["Subject"]), recipient, parent_id, f"{history} {parent_id}".strip()
    except ValueError as error:
        raise ResourceDecodeError(f"cannot create reply draft: {error}; no draft was created") from error


def _build_raw(
    to: str, subject: str, body: str, *, in_reply_to: str | None = None, references: str | None = None,
) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


@mcp.tool(name="create_draft")
async def create_draft(
    to: str | None = None, subject: str | None = None, body: str = "", in_reply_to_message_id: str | None = None,
) -> str:
    """Compose a draft (does NOT send). Returns the draft id to pass to
    send_draft after the user approves. `in_reply_to_message_id` is the Gmail
    message id returned by search/get, not an RFC Message-ID. Reply metadata
    supplies the thread and reference chain. Omit `subject` to inherit it, or
    supply the exact original subject. Omit `to` in a reply to use Reply-To,
    otherwise From. Explicit `to` is preserved; original To/Cc/Bcc are never
    added. A new message requires `to`; an omitted subject is empty."""
    message: dict[str, Any] = {}
    parent_id, references = None, None
    if in_reply_to_message_id is not None:
        if not in_reply_to_message_id.strip():
            return "Error: reply Gmail message id must not be empty; no draft was created."
        parent, error = await _api(
            "GET", f"/users/me/messages/{quote(in_reply_to_message_id, safe='')}", retry_safe=True,
            decode=lambda data: _reply_resource(data, in_reply_to_message_id, infer_recipient=to is None),
            params={"format": "metadata", "metadataHeaders": [
                "Message-ID", "References", "In-Reply-To", "Subject", "Reply-To", "From",
            ]},
        )
        if error:
            return error
        thread_id, original_subject, recipient, parent_id, references = parent
        if subject is not None and subject != original_subject:
            return "Error: cannot create reply draft: reply Subject must match the original Subject; no draft was created."
        subject = original_subject
        if to is None:
            to = recipient
        message["threadId"] = thread_id
    if to is None or not to.strip():
        return "Error: a draft recipient is required; no draft was created."
    if subject is None:
        subject = ""
    try:
        message["raw"] = _build_raw(to, subject, body, in_reply_to=parent_id, references=references)
    except ValueError as header_error:
        return f"Error: invalid draft headers: {header_error}; no draft was created."
    data, err = await _api(
        "POST", "/users/me/drafts", json={"message": message},
        decode=lambda value: {"draft_id": object_data(value).get("id"), "to": to, "subject": subject},
        operation=f"create_draft to={to!r} subject={subject!r} in_reply_to_message_id={in_reply_to_message_id!r}",
        check="Check Gmail Drafts for the original recipient, subject and text.",
    )
    if err:
        return err
    return to_text(data)


@mcp.tool(name="send_draft")
async def send_draft(draft_id: str) -> str:
    """Send a previously-created draft. REQUIRES explicit user confirmation in
    the same turn (ask via mcp__ask__user first) — this delivers email."""
    data, err = await _api(
        "POST", "/users/me/drafts/send", json={"id": draft_id},
        decode=lambda data: {"sent": True, "message_id": object_data(data).get("id")},
        operation=f"send_draft draft_id={draft_id!r}",
        check="Check Gmail Sent and the original draft; a missing draft alone does not prove delivery.",
    )
    if err:
        return err
    return to_text(data)


@mcp.tool(name="label")
async def label(message_id: str, label_ids: list[str]) -> str:
    """Add labels to a message (label ids from list_labels)."""
    data, err = await _api(
        "POST",
        f"/users/me/messages/{message_id}/modify",
        json={"addLabelIds": label_ids},
        decode=lambda value: {"id": object_data(value).get("id"), "labels": value.get("labelIds", [])},
        operation=f"label message_id={message_id!r} label_ids={label_ids!r}",
        check=f"Check labels on Gmail message {message_id!r}.",
    )
    if err:
        return err
    return to_text(data)


@mcp.tool(name="unlabel")
async def unlabel(message_id: str, label_ids: list[str]) -> str:
    """Remove labels from a message (label ids from list_labels)."""
    data, err = await _api(
        "POST",
        f"/users/me/messages/{message_id}/modify",
        json={"removeLabelIds": label_ids},
        decode=lambda value: {"id": object_data(value).get("id"), "labels": value.get("labelIds", [])},
        operation=f"unlabel message_id={message_id!r} label_ids={label_ids!r}",
        check=f"Check labels on Gmail message {message_id!r}.",
    )
    if err:
        return err
    return to_text(data)


if __name__ == "__main__":
    mcp.run()
