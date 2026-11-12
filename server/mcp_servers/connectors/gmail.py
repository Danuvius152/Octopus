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
from collections.abc import Callable
from email._header_value_parser import get_msg_id
from email.errors import HeaderParseError, ObsoleteHeaderDefect
from email.headerregistry import AddressHeader, BaseHeader
from email.message import EmailMessage
from email.policy import default
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
    ResourceDecodeError,
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


async def _api(
    method: str, path: str, *, retry_safe: bool = False,
    operation: str = "", check: str = "Check the original Gmail resource.",
    decode: Callable[[Any], Any], **kw: Any,
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
        return data, None


def _b64url_decode(data: str) -> bytes:
    text_data(data)
    try:
        pad = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + pad)
    except ValueError as error:
        raise ResourceDecodeError(f"could not decode message body: {error}") from error


def _header(payload: dict, name: str) -> str:
    for entry in array_data(object_data(payload).get("headers", [])):
        h = object_data(entry)
        if text_data(h.get("name", "")).lower() == name.lower():
            return text_data(h.get("value", ""))
    return ""


def _find_plain(payload: dict) -> str | None:
    """Depth-first search for a text/plain part anywhere in the tree."""
    payload = object_data(payload)
    body = object_data(payload.get("body", {}))
    if payload.get("mimeType") == "text/plain" and body.get("data"):
        return _b64url_decode(body["data"]).decode("utf-8", "replace")
    for part in array_data(payload.get("parts", []) or []):
        found = _find_plain(part)
        if found is not None:
            return found
    return None


def _any_body(payload: dict) -> str:
    """First inline body anywhere — fallback when there's no text/plain part
    (e.g. an HTML-only message)."""
    payload = object_data(payload)
    data = object_data(payload.get("body", {})).get("data")
    if data:
        return _b64url_decode(data).decode("utf-8", "replace")
    for part in array_data(payload.get("parts", []) or []):
        text = _any_body(part)
        if text:
            return text
    return ""


def _extract_text(payload: dict) -> str:
    """Prefer text/plain (searched across the whole tree so it wins over a
    text/html sibling that appears first); fall back to any inline body."""
    plain = _find_plain(payload)
    return plain if plain is not None else _any_body(payload)


def _message_listing(value: Any) -> dict:
    data = object_data(value)
    ids = [text_data(object_data(ref).get("id")) for ref in array_data(data.get("messages", []) or [])]
    return {"ids": ids, "has_more": bool(data.get("nextPageToken"))}


def _message_headers(value: Any, *, full: bool = False) -> dict:
    msg = object_data(value)
    payload = object_data(msg.get("payload", {}))
    out = {
        "id": msg.get("id"), "thread_id": msg.get("threadId"),
        "from": _header(payload, "From"), "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
    }
    if full:
        out.update(to=_header(payload, "To"), body=_extract_text(payload))
    else:
        out["snippet"] = msg.get("snippet", "")
    return out


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
    """Full message: headers + decoded plain-text body."""
    msg, err = await _api("GET", f"/users/me/messages/{message_id}", retry_safe=True,
                          decode=lambda data: _message_headers(data, full=True), params={"format": "full"})
    if err:
        return err
    return truncate(to_text(msg))


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
