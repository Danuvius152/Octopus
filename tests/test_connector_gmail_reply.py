"""Reply drafts use Gmail identity for lookup and RFC identity for mail headers."""

import base64
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from server.mcp_servers.connectors import gmail
from tests.connector_runtime import provider_transport as provider_transport


def parent_message():
    return {
        "id": "gmail-message-123", "threadId": "gmail-thread-456",
        "payload": {"headers": [
            {"name": "Message-ID", "value": "<parent@example.com>"},
            {"name": "References", "value": "<root@example.com> <previous@example.com>"},
            {"name": "Subject", "value": "季度计划"},
            {"name": "From", "value": "Original sender <sender@example.com>"},
            {"name": "Reply-To", "value": "Reply desk <replies@example.com>"},
            {"name": "To", "value": "Other recipient <other@example.com>"},
            {"name": "Cc", "value": "Bystander <bystander@example.com>"},
        ]},
    }


def decode_draft(request):
    message = json.loads(request.content)["message"]
    raw = base64.urlsafe_b64decode(message["raw"])
    return message, BytesParser(policy=policy.default).parsebytes(raw)


async def test_reply_looks_up_gmail_id_and_builds_thread_and_reference_chain(provider_transport):
    requests = []

    def resource(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=parent_message())
        return httpx.Response(200, json={"id": "draft-original"})

    provider_transport(resource)
    output = await gmail.create_draft(
        to="Chosen reader <chosen@example.com>", subject="季度计划", body="计划已确认。",
        in_reply_to_message_id="gmail-message-123",
    )
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[0].url.path.endswith("/messages/gmail-message-123")
    assert requests[0].url.params["format"] == "metadata"
    assert set(requests[0].url.params.get_list("metadataHeaders")) >= {
        "Message-ID", "References", "In-Reply-To", "Subject", "Reply-To", "From",
    }
    message, mail = decode_draft(requests[1])
    assert message["threadId"] == "gmail-thread-456"
    assert str(mail["In-Reply-To"]) == "<parent@example.com>"
    assert str(mail["References"]) == "<root@example.com> <previous@example.com> <parent@example.com>"
    assert str(mail["Subject"]) == "季度计划"
    assert mail["To"].addresses[0].addr_spec == "chosen@example.com"
    assert mail["Cc"] is None and mail["Bcc"] is None
    assert "计划已确认。" in mail.get_content()
    assert json.loads(output)["draft_id"] == "draft-original"


@pytest.mark.parametrize("has_reply_to,expected", [(True, "replies@example.com"), (False, "sender@example.com")])
async def test_reply_infers_only_the_original_reply_recipient(provider_transport, has_reply_to, expected):
    parent = parent_message()
    if not has_reply_to:
        parent["payload"]["headers"] = [h for h in parent["payload"]["headers"] if h["name"] != "Reply-To"]
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "reply-draft"})

    provider_transport(resource)
    result = json.loads(await gmail.create_draft(body="My reply", in_reply_to_message_id="gmail-message-123"))
    assert [r.method for r in requests] == ["GET", "POST"]
    _, mail = decode_draft(requests[1])
    assert [address.addr_spec for address in mail["To"].addresses] == [expected]
    assert str(mail["Subject"]) == "季度计划"
    assert mail["Cc"] is None and mail["Bcc"] is None
    assert expected in result["to"]


@pytest.mark.parametrize("name", ["Reply-To", "From"])
async def test_inferred_empty_recipient_group_never_creates_draft(provider_transport, name):
    parent = parent_message()
    headers = parent["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] != "Reply-To"]
    if name == "From":
        headers[:] = [h for h in headers if h["name"] != "From"]
    headers.append({"name": name, "value": "undisclosed-recipients:;"})
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "empty-recipient-draft"})

    provider_transport(resource)
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert output.startswith("Error:") and "recipient" in output and "no draft was created" in output
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize("name", ["Reply-To", "From"])
@pytest.mark.parametrize("recipient,to,expected", [
    ("undisclosed-recipients:;", "Chosen <chosen@example.com>", ["chosen@example.com"]),
    ("Team: Alice <alice@example.com>, Bob <bob@example.com>;", None, ["alice@example.com", "bob@example.com"]),
])
async def test_reply_preserves_explicit_recipients_and_nonempty_groups(provider_transport, name, recipient, to, expected):
    parent = parent_message()
    headers = parent["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] != "Reply-To"]
    if name == "From":
        headers[:] = [h for h in headers if h["name"] != "From"]
    headers.append({"name": name, "value": recipient})
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "reply-draft"})

    provider_transport(resource)
    output = await gmail.create_draft(to=to, body="Reply", in_reply_to_message_id="gmail-message-123")
    assert not output.startswith("Error:")
    assert [request.method for request in requests] == ["GET", "POST"]
    _, mail = decode_draft(requests[-1])
    assert [address.addr_spec for address in mail["To"].addresses] == expected
    assert mail["Cc"] is None and mail["Bcc"] is None


@pytest.mark.parametrize("references,in_reply_to,expected", [
    (None, None, "<parent@example.com>"),
    (None, "<previous@example.com>", "<previous@example.com> <parent@example.com>"),
    (None, "<one@example.com> <two@example.com>", "<parent@example.com>"),
    ("<root@example.com>\r\n\t<previous@example.com>", None, "<root@example.com> <previous@example.com> <parent@example.com>"),
    ("(root) <root@example.com> (parent) <previous@example.com>", None,
     "(root) <root@example.com> (parent) <previous@example.com> <parent@example.com>"),
    ('<"root>id"@example.com>', None, '<"root>id"@example.com> <parent@example.com>'),
])
async def test_reply_retains_reference_history_and_legal_header_folding(provider_transport, references, in_reply_to, expected):
    parent = parent_message()
    headers = parent["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] != "References"]
    if references is not None:
        headers.append({"name": "rEfErEnCeS", "value": references})
    if in_reply_to is not None:
        headers.append({"name": "in-reply-to", "value": in_reply_to})
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "reply-draft"})

    provider_transport(resource)
    output = await gmail.create_draft(body="My reply", in_reply_to_message_id="gmail-message-123")
    assert not output.startswith("Error:")
    _, mail = decode_draft(requests[-1])
    assert str(mail["References"]) == expected


@pytest.mark.parametrize("field,value", [("threadId", None), ("threadId", 42), ("threadId", ""), ("id", "other-message")])
async def test_missing_or_mismatched_gmail_identity_never_creates_draft(provider_transport, field, value):
    parent = parent_message()
    parent[field] = value
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(200, json=parent))
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert output.startswith("Error:") and "no draft was created" in output
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize("name,value", [
    ("Message-ID", None), ("Message-ID", ""), ("Message-ID", "not-a-mail-id"),
    ("Message-ID", "<one@example.com> <two@example.com>"), ("Subject", None),
    ("References", "<root@example.com>\r\nBcc: intruder@example.com"),
    ("Reply-To", "not-an-address"),
])
async def test_incomplete_or_invalid_reply_headers_never_create_draft(provider_transport, name, value):
    parent = parent_message()
    headers = parent["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] != name]
    if value is not None:
        headers.append({"name": name, "value": value})
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(200, json=parent))
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert output.startswith("Error:") and "no draft was created" in output
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize("parent", [
    [], {}, {"id": "gmail-message-123", "threadId": "thread", "payload": None},
    {"id": "gmail-message-123", "threadId": "thread", "payload": {"headers": None}},
    {"id": "gmail-message-123", "threadId": "thread", "payload": {"headers": [None]}},
    {"id": "gmail-message-123", "threadId": "thread", "payload": {"headers": [{"name": "Message-ID", "value": 42}]}},
])
async def test_malformed_metadata_fails_before_draft_creation(provider_transport, parent):
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(200, json=parent))
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert output.startswith("Error:") and "no draft was created" in output
    assert [request.method for request in requests] == ["GET"]


async def test_explicit_recipient_range_does_not_require_or_add_original_recipients(provider_transport):
    parent = parent_message()
    parent["payload"]["headers"] = [h for h in parent["payload"]["headers"] if h["name"] not in {"From", "Reply-To"}]
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "reply-draft"})

    provider_transport(resource)
    output = await gmail.create_draft(
        to="Alice <alice@example.com>, Bob <bob@example.com>", body="Reply", in_reply_to_message_id="gmail-message-123",
    )
    assert not output.startswith("Error:")
    _, mail = decode_draft(requests[-1])
    assert [address.addr_spec for address in mail["To"].addresses] == ["alice@example.com", "bob@example.com"]
    assert mail["Cc"] is None and mail["Bcc"] is None


async def test_conflicting_reply_subject_is_rejected_before_creation(provider_transport):
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(200, json=parent_message()))
    output = await gmail.create_draft(subject="Different conversation", body="Reply", in_reply_to_message_id="gmail-message-123")
    assert "Subject must match" in output and "no draft was created" in output
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize("status", [404, 503])
async def test_metadata_fetch_failure_cannot_create_incomplete_reply(provider_transport, status):
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(status, headers={"Retry-After": "60"}))
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert output.startswith("Error:")
    assert [request.method for request in requests] == ["GET"]


async def test_reply_creation_keeps_unknown_write_safety_after_metadata_lookup(provider_transport):
    requests = []

    def resource(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=parent_message())
        return httpx.Response(503)

    provider_transport(resource)
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert [request.method for request in requests] == ["GET", "POST"]
    assert "outcome_unknown" in output and "gmail-message-123" in output


async def test_reply_mcp_entry_can_infer_subject_and_recipient(provider_transport):
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent_message() if request.method == "GET" else {"id": "reply-draft"})

    provider_transport(resource)
    result = await gmail.mcp.call_tool("create_draft", {"body": "Reply", "in_reply_to_message_id": "gmail-message-123"})
    blocks = result[0] if isinstance(result, tuple) else result
    assert "reply-draft" in "".join(block.text for block in blocks)
    message, mail = decode_draft(requests[-1])
    assert message["threadId"] == "gmail-thread-456" and str(mail["Subject"]) == "季度计划"
    assert mail["To"].addresses[0].addr_spec == "replies@example.com"


@pytest.mark.parametrize("message_id", ["(origin) <parent@example.com>", '<"parent"@example.com>'])
async def test_reply_accepts_valid_original_message_id_syntax(provider_transport, message_id):
    parent = parent_message()
    parent["payload"]["headers"][0]["value"] = message_id
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "reply-draft"})

    provider_transport(resource)
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert not output.startswith("Error:")
    _, mail = decode_draft(requests[-1])
    assert str(mail["In-Reply-To"]) == message_id


@pytest.mark.parametrize("name", ["Reply-To", "From"])
async def test_reply_accepts_received_display_names_with_periods(provider_transport, name):
    parent = parent_message()
    headers = parent["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] not in {"Reply-To", "From"}]
    headers.append({"name": name, "value": "Dr. Smith <replies@example.com>"})
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "reply-draft"})

    provider_transport(resource)
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert not output.startswith("Error:")
    _, mail = decode_draft(requests[-1])
    assert mail["To"].addresses[0].addr_spec == "replies@example.com"
    assert mail["To"].addresses[0].display_name == "Dr. Smith"


@pytest.mark.parametrize("references", ["<broken", "<root@example.com> junk"])
async def test_invalid_reference_history_fails_before_creating_reply(provider_transport, references):
    parent = parent_message()
    parent["payload"]["headers"][1]["value"] = references
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(200, json=parent))
    output = await gmail.create_draft(body="Reply", in_reply_to_message_id="gmail-message-123")
    assert output.startswith("Error:") and "References" in output
    assert [request.method for request in requests] == ["GET"]
