"""Gmail connector MCP server (connectors.md §6.2): search projection, body
decode, draft/send, label, and the 401→reconnect path. HTTP mocked by
monkeypatching the module's httpx; tools are called directly."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from server.mcp_servers.connectors import gmail as gm
from tests.connector_runtime import clock as clock
from tests.connector_runtime import provider_transport as provider_transport


class FakeResp:
    def __init__(self, status=200, json_body=None, headers=None):
        self.status_code = status
        self._json = json_body
        self.text = ""
        self.headers = headers or {}
        self.content = b"x" if json_body is not None else b""

    def json(self):
        return self._json


class TrackedBody(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(gm.ctx, "token_info", lambda: {"access_token": "tok", "grant_stamp": "stamp"})
    recon: list[str] = []
    monkeypatch.setattr(
        gm.ctx, "mark_needs_reconnect", lambda stamp: recon.append(stamp) or "applied"
    )
    return recon


def _patch(monkeypatch, handler):
    async def send(self, method, url, **kwargs):
        return handler(method, url, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "request", send)


def _b64url(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).decode().rstrip("=")


def _external_part(attachment_id, *, mime_type="text/plain", size=None, headers=()):
    body = {"attachmentId": attachment_id}
    if size is not None:
        body["size"] = size
    return {"mimeType": mime_type, "headers": [{"name": name, "value": value} for name, value in headers], "body": body}


def _full_message(payload, *, message_id="m1"):
    return httpx.Response(200, json={"id": message_id, "threadId": "t1", "payload": payload})


async def test_search_lists_and_projects(monkeypatch, authed):
    def handler(method, url, **kw):
        if url.endswith("/users/me/messages"):
            return FakeResp(json_body={"messages": [{"id": "m1"}]})
        if "/users/me/messages/m1" in url:
            return FakeResp(
                json_body={
                    "id": "m1",
                    "threadId": "t1",
                    "snippet": "hi there",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "alice@x.com"},
                            {"name": "Subject", "value": "Hello"},
                        ]
                    },
                }
            )
        return FakeResp(status=404)

    _patch(monkeypatch, handler)
    out = await gm.search(query="is:unread", limit=5)
    assert "alice@x.com" in out
    assert "Hello" in out
    assert "hi there" in out


@pytest.mark.parametrize("failed", [["m1"], ["m1", "m2"]])
async def test_search_reports_metadata_failures_instead_of_false_empty(monkeypatch, authed, failed):
    def handler(method, url, **kwargs):
        if url.endswith("/users/me/messages"):
            return FakeResp(json_body={"messages": [{"id": "m1"}, {"id": "m2"}]})
        message_id = url.rsplit("/", 1)[-1]
        if message_id in failed:
            return FakeResp(status=503, headers={"Retry-After": "60"})
        return FakeResp(json_body={"id": message_id})
    _patch(monkeypatch, handler)
    result = json.loads(await gm.search("query"))
    assert result["complete"] is False
    assert [entry["id"] for entry in result["failures"]] == failed
    assert len(result["messages"]) == 2 - len(failed)


async def test_get_decodes_plain_body(monkeypatch, authed):
    payload = {
        "headers": [{"name": "Subject", "value": "S"}],
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64url(b"<p>nope</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64url(b"Body text here")}},
        ],
    }
    _patch(
        monkeypatch,
        lambda m, u, **k: FakeResp(json_body={"id": "m1", "threadId": "t1", "payload": payload}),
    )
    out = await gm.get(message_id="m1")
    assert "Body text here" in out


@pytest.mark.parametrize("binary_first", [True, False])
async def test_get_keeps_html_body_and_skips_attachments(provider_transport, binary_first):
    binary = {"mimeType": "application/octet-stream", "body": {"data": _b64url(b"\xff\x01")}}
    text_attachment = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Disposition", "value": 'attachment; filename="notes.txt"'}],
        "body": {"data": _b64url(b"attachment text")},
    }
    html = {"mimeType": "text/html", "body": {"data": _b64url(b"<p>Message text</p>")}}
    parts = [binary, text_attachment, html] if binary_first else [html, text_attachment, binary]
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "m1", "payload": {"mimeType": "multipart/mixed", "parts": parts}})

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "<p>Message text</p>"
    assert [(request.method, request.url.path, dict(request.url.params)) for request in requests] == [
        ("GET", "/gmail/v1/users/me/messages/m1", {"format": "full"}),
    ]


async def test_get_reads_only_the_selected_parts_of_nested_mime(provider_transport):
    alternative = {
        "mimeType": "multipart/alternative",
        "parts": [
            _external_part("unused-html", mime_type="text/html"),
            _external_part("body+/=?", size=4, headers=[("Content-Type", 'text/plain; charset="iso-8859-1"')]),
        ],
    }
    related = {
        "mimeType": "multipart/related",
        "headers": [{"name": "Content-Type", "value": 'multipart/related; type="text/html";\r\n start="<root>"'}],
        "parts": [
            _external_part("unused-resource", headers=[("Content-ID", "<resource>")]),
            {
                "mimeType": "text/html",
                "filename": "root.html",
                "headers": [
                    {"name": "Content-ID", "value": "<root>"},
                    {"name": "Content-Disposition", "value": "attachment"},
                    {"name": "Content-Type", "value": "text/html; charset=UTF-8"},
                ],
                "body": {"data": _b64url("<p>東京</p>".encode())},
            },
        ],
    }
    attached = {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "Content-Disposition", "value": "inline; filename*=utf-8''archive.eml"}],
        "parts": [_external_part("unused-attached-text")],
    }
    forwarded = {"mimeType": "message/rfc822", "parts": [_external_part("unused-forwarded-text")]}
    base = b"/gmail/v1/users/me/messages/m%2F1"
    requests = []

    def resource(request):
        path = request.url.raw_path.split(b"?", 1)[0]
        requests.append(path)
        if path == base:
            return _full_message(
                {"mimeType": "multipart/mixed", "parts": [attached, related, forwarded, alternative]}, message_id="m/1",
            )
        assert path == base + b"/attachments/body%2B%2F%3D%3F"
        return httpx.Response(200, json={"size": 4, "data": _b64url(b"caf\xe9")})

    provider_transport(resource)
    assert json.loads(await gm.get("m/1"))["body"] == "<p>東京</p>\ncafé"
    assert requests == [base, base + b"/attachments/body%2B%2F%3D%3F"]


async def test_get_uses_nonempty_alternative_and_default_related_root(provider_transport):
    html = {
        "mimeType": "multipart/related",
        "parts": [
            {"mimeType": "text/html", "body": {"data": "PHA-YWJjPC9wPg=="}},
            _external_part("unused-resource"),
        ],
    }
    message = {
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/plain", "body": {"size": 0}}, html],
    }
    requests = []
    provider_transport(lambda request: requests.append(request) or _full_message(message))
    assert json.loads(await gm.get("m1"))["body"] == "<p>abc</p>"
    assert len(requests) == 1


@pytest.mark.parametrize("matches", [0, 2])
async def test_get_rejects_related_message_without_a_unique_declared_root(provider_transport, matches):
    message = {
        "mimeType": "multipart/related",
        "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<root>"'}],
        "parts": [_external_part("auxiliary")] + [
            _external_part(f"root-{i}", headers=[("Content-ID", "<root>")]) for i in range(matches)
        ],
    }
    requests = []
    provider_transport(lambda request: requests.append(request) or _full_message(message))
    output = await gm.get("m1")
    assert output.startswith("Error:") and "exactly one root" in output
    assert len(requests) == 1


@pytest.mark.parametrize("header", ["disposition", "charset", "content-id"])
async def test_get_parses_mime_header_comments(provider_transport, header):
    if header == "content-id":
        message = {
            "mimeType": "multipart/related",
            "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<root@example>"'}],
            "parts": [
                _external_part("resource"),
                {"mimeType": "text/html", "body": {"data": "Y2Fmw6k"}, "headers": [
                    {"name": "Content-ID", "value": "(prefix) <root@example> (comment)"},
                ]},
            ],
        }
    else:
        name, value = ("Content-Disposition", "inline (comment)") if header == "disposition" else (
            "Content-Type", "text/plain; charset=iso-8859-1 (comment)",
        )
        message = {
            "mimeType": "text/plain",
            "body": {"data": "Y2Fmw6k" if header == "disposition" else "Y2Fm6Q"},
            "headers": [{"name": name, "value": value}],
        }

    def resource(request):
        assert "/attachments/" not in request.url.path
        return _full_message(message)

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "café"


@pytest.mark.parametrize(("size", "external_calls"), [(0, 0), (None, 1)])
async def test_get_uses_html_alternative_when_external_plain_text_is_empty(provider_transport, size, external_calls):
    requests = []

    def resource(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/attachments/body"):
            return httpx.Response(200, json={"size": 0})
        return _full_message({"mimeType": "multipart/alternative", "parts": [
            _external_part("body", size=size), {"mimeType": "text/html", "body": {"data": "PGI-"}},
        ]})

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "<b>"
    assert len(requests) == 1 + external_calls


async def test_get_shares_request_budget_across_empty_alternatives(provider_transport, monkeypatch):
    monkeypatch.setattr(gm, "_BODY_MAX_EXTERNAL_READS", 2)
    paths = []

    def resource(request):
        paths.append(request.url.path)
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"size": 0})
        return _full_message({"mimeType": "multipart/alternative", "parts": [
            _external_part("plain-1"), _external_part("plain-2"),
            _external_part("html", mime_type="text/html"),
        ]})

    provider_transport(resource)
    assert "external body request budget (2 reads) exhausted" in await gm.get("m1")
    assert [path.rsplit("/", 1)[1] for path in paths] == ["m1", "plain-1", "plain-2"]


async def test_get_counts_bytes_spent_on_an_empty_decoded_alternative(provider_transport, monkeypatch):
    monkeypatch.setattr(gm, "_BODY_MAX_BYTES", 4)

    def resource(request):
        if request.url.path.endswith("/attachments/plain"):
            return httpx.Response(200, json={"size": 2, "data": "__4"})  # UTF-16 BOM with no text.
        return _full_message({"mimeType": "multipart/alternative", "parts": [
            _external_part("plain", headers=[("Content-Type", "text/plain; charset=UTF-16")]),
            {"mimeType": "text/html", "body": {"data": "YWJj"}},
        ]})

    provider_transport(resource)
    assert "message body byte budget (4 bytes) exhausted" in await gm.get("m1")


@pytest.mark.parametrize("invalid_nested_branch", [False, True])
@pytest.mark.parametrize("mixed_wrapper", [False, True])
async def test_get_reconsiders_outer_alternatives_after_the_inner_choice_is_empty(
    provider_transport, monkeypatch, invalid_nested_branch, mixed_wrapper,
):
    monkeypatch.setattr(gm, "_BODY_MAX_BYTES", 4)
    nested_fallback = _external_part("markdown", mime_type="text/markdown", size=3)
    if invalid_nested_branch:
        nested_fallback = {
            "mimeType": "multipart/related",
            "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<missing@example>"'}],
            "parts": [_external_part("auxiliary")],
        }
    nested = {"mimeType": "multipart/alternative", "parts": [
        _external_part("plain", headers=[("Content-Type", "text/plain; charset=UTF-16")]), nested_fallback,
    ]}
    if mixed_wrapper:
        nested = {"mimeType": "multipart/mixed", "parts": [
            _external_part("calendar", mime_type="text/calendar"), nested,
        ]}
    message = {"mimeType": "multipart/alternative", "parts": [
        nested, {"mimeType": "text/html", "body": {"data": "aA"}},
    ]}

    def resource(request):
        if request.url.path.endswith("/attachments/plain"):
            return httpx.Response(200, json={"size": 2, "data": "__4"})
        assert "/attachments/" not in request.url.path
        return _full_message(message)

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "h"


async def test_get_preflights_nested_fallback_and_outer_required_parts_together(provider_transport, monkeypatch):
    monkeypatch.setattr(gm, "_BODY_MAX_EXTERNAL_READS", 2)
    nested = {"mimeType": "multipart/alternative", "parts": [
        _external_part("plain"), _external_part("html", mime_type="text/html", size=1),
    ]}
    message = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [nested, _external_part("markdown", mime_type="text/markdown")]},
        _external_part("trailer", size=1),
    ]}
    paths = []

    def resource(request):
        paths.append(request.url.path.rsplit("/", 1)[1])
        if request.url.path.endswith("/attachments/plain"):
            return httpx.Response(200, json={"size": 0})
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"size": 1, "data": "aA"})
        return _full_message(message)

    provider_transport(resource)
    assert "external body request budget (2 reads) exhausted" in await gm.get("m1")
    assert paths == ["m1", "plain"]


@pytest.mark.parametrize("budget", ["bytes", "requests"])
@pytest.mark.parametrize("plain_empty", [False, True])
async def test_get_reserves_branch_companions_only_after_confirming_the_winner(
    provider_transport, monkeypatch, budget, plain_empty,
):
    if budget == "bytes":
        monkeypatch.setattr(gm, "_BODY_MAX_BYTES", 4)
        plain = {
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-16"}],
            "body": {"data": "__4" if plain_empty else "__5wAA"},
        }
        reason = "message body byte budget (4 bytes) exhausted"
    else:
        monkeypatch.setattr(gm, "_BODY_MAX_EXTERNAL_READS", 1)
        plain = _external_part("plain")
        reason = "external body request budget (1 reads) exhausted"
    message = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "multipart/mixed", "parts": [plain, _external_part("markdown", mime_type="text/markdown", size=4)]},
        {"mimeType": "text/html", "body": {"data": "aA"}},
    ]}
    paths = []

    def resource(request):
        paths.append(request.url.path.rsplit("/", 1)[1])
        if request.url.path.endswith("/attachments/plain"):
            data = {"size": 0} if plain_empty else {"size": 1, "data": "cA"}
            return httpx.Response(200, json=data)
        assert "/attachments/" not in request.url.path
        return _full_message(message)

    provider_transport(resource)
    output = await gm.get("m1")
    if plain_empty:
        assert json.loads(output)["body"] == "h"
    else:
        assert output.startswith("Error:") and reason in output
    assert paths == (["m1", "plain"] if budget == "requests" else ["m1"])


async def test_get_defers_companions_while_a_nested_choice_can_invalidate_the_branch(provider_transport, monkeypatch):
    monkeypatch.setattr(gm, "_BODY_MAX_BYTES", 4)
    invalid_related = {
        "mimeType": "multipart/related",
        "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<missing@example>"'}],
        "parts": [_external_part("auxiliary")],
    }
    required_choice = {"mimeType": "multipart/alternative", "parts": [
        _external_part("probe", mime_type="text/html"), invalid_related,
    ]}
    message = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": "cA"}}, required_choice,
            _external_part("markdown", mime_type="text/markdown", size=4),
        ]},
        {"mimeType": "text/html", "body": {"data": "aA"}},
    ]}

    def resource(request):
        if request.url.path.endswith("/attachments/probe"):
            return httpx.Response(200, json={"size": 0})
        assert "/attachments/" not in request.url.path
        return _full_message(message)

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "h"


@pytest.mark.parametrize("external_size", [None, 256 * 1024 + 1])
@pytest.mark.parametrize("earlier_external_probe", [False, True])
async def test_get_can_rule_out_an_alternative_using_only_the_full_message(
    provider_transport, external_size, earlier_external_probe,
):
    invalid_related = {
        "mimeType": "multipart/related",
        "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<missing@example>"'}],
        "parts": [_external_part("auxiliary")],
    }
    inline_bom = {
        "mimeType": "text/html",
        "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-16"}],
        "body": {"data": "__4"},
    }
    preferred_parts = [_external_part("plain", size=external_size)]
    if earlier_external_probe:
        preferred_parts.append({"mimeType": "multipart/alternative", "parts": [
            _external_part("preview", mime_type="text/html"), invalid_related,
        ]})
    preferred_parts.append({"mimeType": "multipart/alternative", "parts": [inline_bom, invalid_related]})
    message = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "multipart/mixed", "parts": preferred_parts},
        {"mimeType": "text/html", "body": {"data": "aA"}},
    ]}
    paths = []

    def resource(request):
        paths.append(request.url.path)
        if "/attachments/" in request.url.path:
            return httpx.Response(404)
        return _full_message(message)

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "h"
    assert paths == ["/gmail/v1/users/me/messages/m1"]


@pytest.mark.parametrize("required_tail", [False, True])
@pytest.mark.parametrize("plain_empty", [False, True])
async def test_get_uses_an_affordable_probe_if_inline_structure_checks_exceed_budget(
    provider_transport, required_tail, plain_empty,
):
    invalid_related = {
        "mimeType": "multipart/related",
        "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<missing@example>"'}],
        "parts": [_external_part("auxiliary")],
    }
    max_body_bytes = 256 * 1024
    inline_size = max_body_bytes if required_tail else max_body_bytes + 1
    inline_markdown = {"mimeType": "text/markdown", "body": {"data": _b64url(b"x" * inline_size)}}
    plain = _external_part("plain", headers=[("Content-Type", "text/plain; charset=UTF-16")])
    possible_body = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "multipart/mixed", "parts": [
            plain, {"mimeType": "multipart/alternative", "parts": [inline_markdown, invalid_related]},
        ]},
        {"mimeType": "text/html", "body": {"data": "aA"}},
    ]}
    message = possible_body if not required_tail else {"mimeType": "multipart/mixed", "parts": [
        possible_body, {"mimeType": "text/plain", "body": {"data": "ZW5k"}},
    ]}
    paths = []

    def resource(request):
        paths.append(request.url.path.rsplit("/", 1)[1])
        if request.url.path.endswith("/attachments/plain"):
            body = {"size": 2, "data": "__4"} if plain_empty else {"size": 4, "data": "__5wAA"}
            return httpx.Response(200, json=body)
        assert "/attachments/" not in request.url.path
        return _full_message(message)

    provider_transport(resource)
    output = await gm.get("m1")
    if plain_empty:
        assert json.loads(output)["body"] == ("h\nend" if required_tail else "h")
    else:
        assert output.startswith("Error:") and "message body byte budget (262144 bytes) exhausted" in output
    assert paths == ["m1", "plain"]


async def test_get_checks_unavoidable_probes_before_starting_independent_reads(provider_transport, monkeypatch):
    monkeypatch.setattr(gm, "_BODY_MAX_EXTERNAL_READS", 1)
    message = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            _external_part(part_id), {"mimeType": "text/html", "body": {"data": "aA"}},
        ]}
        for part_id in ("first", "second")
    ]}

    def resource(request):
        assert "/attachments/" not in request.url.path
        return _full_message(message)

    provider_transport(resource)
    assert "external body request budget (1 reads) exhausted" in await gm.get("m1")


async def test_get_reserves_bytes_and_requests_jointly_for_independent_choices(provider_transport, monkeypatch):
    monkeypatch.setattr(gm, "_BODY_MAX_BYTES", 18)
    monkeypatch.setattr(gm, "_BODY_MAX_EXTERNAL_READS", 1)
    invalid_related = {
        "mimeType": "multipart/related",
        "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<missing@example>"'}],
        "parts": [_external_part("auxiliary")],
    }
    choices = []
    for part_id, shifts in (("first", 4), ("second", 2), ("third", 2)):
        inline = {
            "mimeType": "text/markdown",
            "headers": [{"name": "Content-Type", "value": "text/markdown; charset=ISO-2022-JP"}],
            "body": {"data": _b64url(b"\x1b(B" * shifts)},  # Shift sequences with no text.
        }
        plain = _external_part(part_id, size=2, headers=[("Content-Type", "text/plain; charset=UTF-16")])
        choices.append({"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "multipart/mixed", "parts": [
                plain, {"mimeType": "multipart/alternative", "parts": [inline, invalid_related]},
            ]},
            {"mimeType": "text/html", "body": {"data": "aA"}},
        ]})
    paths = []

    def resource(request):
        paths.append(request.url.path.rsplit("/", 1)[1])
        if request.url.path.endswith("/attachments/first"):
            return httpx.Response(200, json={"size": 2, "data": "__4"})
        assert "/attachments/" not in request.url.path
        return _full_message({"mimeType": "multipart/mixed", "parts": choices})

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "h\nh\nh"
    assert paths == ["m1", "first"]


async def test_get_uses_other_inline_text_when_it_is_the_body(provider_transport):
    provider_transport(lambda request: _full_message({"mimeType": "text/markdown", "body": {"data": "IyBib2R5"}}))
    assert json.loads(await gm.get("m1"))["body"] == "# body"


async def test_get_ignores_invalid_structure_in_unused_alternative(provider_transport):
    invalid_html = {
        "mimeType": "multipart/related",
        "headers": [{"name": "Content-Type", "value": 'multipart/related; start="<missing@example>"'}],
        "parts": [_external_part("auxiliary")],
    }

    def resource(request):
        assert "/attachments/" not in request.url.path
        return _full_message({"mimeType": "multipart/alternative", "parts": [
            invalid_html, {"mimeType": "text/plain", "body": {"data": "Ym9keQ"}},
        ]})

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "body"


@pytest.mark.parametrize(("body", "external"), [({"size": 0}, False), ({"size": 0}, True)])
async def test_get_returns_a_valid_empty_body(provider_transport, body, external):
    def resource(request):
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json=body)
        return _full_message(_external_part("body", size=0) if external else {"mimeType": "text/plain", "body": body})

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == ""


@pytest.mark.parametrize(("body", "charset", "reason"), [
    ({"size": 3}, "UTF-8", "bytes are missing"),
    ({"size": 3, "data": "YQ"}, "UTF-8", "size does not match"),
    ({"data": "+A=="}, "ISO-8859-1", "invalid base64url"),
    ({"data": "YQ="}, "UTF-8", "invalid base64url"),
    ({"data": "_w"}, "UTF-8", "using its charset"),
    ({"data": "YQ"}, "unknown-charset", "using its charset"),
])
async def test_get_reports_decode_errors(provider_transport, body, charset, reason):
    message = {
        "mimeType": "text/plain", "body": body,
        "headers": [{"name": "Content-Type", "value": f"text/plain; charset={charset}"}],
    }
    provider_transport(lambda request: _full_message(message))
    output = await gm.get("m1")
    assert output.startswith("Error:") and reason in output


async def test_get_reports_missing_full_payload(provider_transport):
    provider_transport(lambda request: httpx.Response(200, json={"id": "m1"}))
    assert "Invalid Gmail resource response" in await gm.get("m1")


@pytest.mark.parametrize(("status", "body", "reason"), [
    (404, {}, "HTTP 404"),
    (200, {}, "external message body bytes are missing"),
    (200, {"size": 0}, "message body bytes are missing"),
    (200, {"size": 3, "data": "YQ"}, "size does not match"),
    (200, {"size": 3, "data": "...."}, "invalid base64url"),
])
async def test_get_reports_external_body_failure_instead_of_empty(provider_transport, status, body, reason):
    def resource(request):
        if "/attachments/" in request.url.path:
            return httpx.Response(status, json=body)
        return _full_message(_external_part("body", size=3))

    provider_transport(resource)
    output = await gm.get("m1")
    assert output.startswith("Error:") and reason in output


@pytest.mark.parametrize(("actual", "declared", "expected_calls"), [
    (4, 4, 2),
    (5, 4, 2),
    (5, 5, 1),
])
async def test_get_checks_total_decoded_byte_budget(provider_transport, monkeypatch, actual, declared, expected_calls):
    monkeypatch.setattr(gm, "_BODY_MAX_BYTES", 8)
    requests = []

    def resource(request):
        requests.append(request)
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"size": declared, "data": _b64url(b"1" * actual)})
        return _full_message({"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url(b"abcd")}},
            _external_part("body", size=declared),
        ]})

    provider_transport(resource)
    output = await gm.get("m1")
    if actual == 4:
        assert json.loads(output)["body"] == "abcd\n1111"
    else:
        assert output.startswith("Error:") and "message body byte budget (8 bytes) exhausted" in output
    assert len(requests) == expected_calls


@pytest.mark.parametrize("count", [4, 5])
async def test_get_checks_external_request_budget_before_fetching(provider_transport, count):
    requests = []

    def resource(request):
        requests.append(request)
        if "/attachments/" in request.url.path:
            suffix = request.url.path.rsplit("/body-", 1)[1]
            return httpx.Response(200, json={"size": 1, "data": _b64url(suffix.encode())})
        return _full_message({
            "mimeType": "multipart/mixed", "parts": [_external_part(f"body-{i}", size=1) for i in range(count)],
        })

    provider_transport(resource)
    output = await gm.get("m1")
    if count == 4:
        assert json.loads(output)["body"] == "0\n1\n2\n3"
        assert len(requests) == 5
    else:
        assert output.startswith("Error:") and "external body request budget (4 reads) exhausted" in output
        assert len(requests) == 1


@pytest.mark.parametrize(("status", "headers", "reason", "reads"), [
    (200, {}, "resource response byte budget", 2),
    (200, {"Content-Encoding": "gzip"}, "must use identity encoding", 0),
    (401, {}, "reconnect", 0),
    (429, {"Retry-After": "60"}, "required wait 60s exceeds remaining", 0),
])
async def test_get_bounds_the_external_response_stream(
    provider_transport, monkeypatch, status, headers, reason, reads,
):
    monkeypatch.setattr(gm, "_BODY_MAX_BYTES", 8)
    requests = []
    stream = TrackedBody(b"x" * 900, b"y" * 200, b"unread")

    def resource(request):
        requests.append(request)
        if "/attachments/" in request.url.path:
            assert request.headers["Accept-Encoding"] == "identity"
            return httpx.Response(status, headers=headers, stream=stream)
        return _full_message(_external_part("body"))

    provider_transport(resource)
    output = await gm.get("m1")
    assert output.startswith("Error:") and reason in output
    assert stream.reads == reads and stream.closed
    assert len(requests) == 2


async def test_get_retries_a_temporary_external_body_failure_within_resource_budget(provider_transport, clock):
    stream = TrackedBody(b"unused error body")
    attempts = []

    def resource(request):
        if "/attachments/" not in request.url.path:
            return _full_message(_external_part("body"))
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, headers={"Retry-After": "2"}, stream=stream)
        return httpx.Response(200, json={"size": 4, "data": _b64url(b"body")})

    provider_transport(resource)
    assert json.loads(await gm.get("m1"))["body"] == "body"
    assert len(attempts) == 2 and clock.waits == [2]
    assert stream.reads == 0 and stream.closed


@pytest.mark.parametrize("revoked", [True, False])
async def test_get_rechecks_authorization_before_each_external_resource(provider_transport, revoked):
    callback_calls, provider_calls = [], []

    def token(request):
        callback_calls.append(request)
        if len(callback_calls) == 1:
            return httpx.Response(200, json={"access_token": "provider-token", "grant_stamp": "first-grant"})
        if revoked:
            return httpx.Response(403, json={"detail": {"kind": "permission", "message": "permission revoked"}})
        return httpx.Response(200, json={"access_token": "provider-token", "grant_stamp": "original-grant"})

    def resource(request):
        provider_calls.append(request)
        if "/attachments/" in request.url.path:
            return httpx.Response(401)
        return _full_message(_external_part("body"))

    # The host transport checks that a 401 uses the external read's grant stamp.
    provider_transport(resource, token_handler=token)
    output = await gm.get("m1")
    assert output.startswith("Error:") and ("permission revoked" if revoked else "reconnect") in output
    assert len(callback_calls) == 2
    assert len(provider_calls) == (1 if revoked else 2)


async def test_get_uses_one_deadline_and_closes_the_inflight_body(provider_transport, monkeypatch):
    requests = []
    entered, closed = [], []
    loop = asyncio.get_running_loop()
    loop_time = loop.time
    elapsed = 0.0
    monkeypatch.setattr(loop, "time", lambda: loop_time() + elapsed)

    class TimedBody(httpx.AsyncByteStream):
        def __init__(self, name, body):
            self.name = name
            self.body = body

        async def __aiter__(self):
            nonlocal elapsed
            entered.append(self.name)
            # Both resources individually fit in 30s. Give the event loop
            # a turn to deliver the tool timeout after the second advance.
            elapsed += 26.0
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            yield self.body

        async def aclose(self):
            closed.append(self.name)

    def resource(request):
        requests.append(request)
        if request.url.path.endswith("/slow"):
            body = json.dumps({"size": 4, "data": _b64url(b"body")}).encode()
            return httpx.Response(200, stream=TimedBody("slow", body))
        assert "/attachments/" not in request.url.path
        message = _full_message({"mimeType": "multipart/mixed", "parts": [_external_part("slow"), _external_part("later")]})
        return httpx.Response(200, stream=TimedBody("message", message.content))

    provider_transport(resource)
    output = await gm.get("m1")
    assert output.startswith("Error:") and "Gmail message read budget (50s) exhausted" in output
    assert entered == closed == ["message", "slow"]
    assert len(requests) == 2 and requests[-1].url.path.endswith("/slow")


async def test_get_preserves_output_truncation_for_unicode_body(provider_transport):
    data = _b64url(("真实正文" * 4_000).encode())
    provider_transport(lambda request: _full_message({"mimeType": "text/plain", "body": {"data": data}}))
    output = await gm.get("m1")
    assert '"body": "真实正文' in output and "…[truncated " in output
    assert len(output.encode()) < 32 * 1024 + 100


async def test_list_labels(monkeypatch, authed):
    _patch(
        monkeypatch,
        lambda m, u, **k: FakeResp(
            json_body={"labels": [{"id": "INBOX", "name": "INBOX"}]}
        ),
    )
    out = await gm.list_labels()
    assert "INBOX" in out


async def test_create_draft_builds_raw(monkeypatch, authed):
    cap = {}

    def handler(method, url, **kw):
        cap["method"], cap["url"], cap["json"] = method, url, kw.get("json")
        return FakeResp(json_body={"id": "d1"})

    _patch(monkeypatch, handler)
    out = await gm.create_draft(to="bob@x.com", subject="Hi", body="Hello Bob")
    assert cap["method"] == "POST" and cap["url"].endswith("/users/me/drafts")
    raw = cap["json"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "bob@x.com" in decoded and "Hello Bob" in decoded
    assert '"draft_id": "d1"' in out


async def test_send_draft(monkeypatch, authed):
    cap = {}

    def handler(method, url, **kw):
        cap["url"], cap["json"] = url, kw.get("json")
        return FakeResp(json_body={"id": "sent-1"})

    _patch(monkeypatch, handler)
    out = await gm.send_draft(draft_id="d1")
    assert cap["url"].endswith("/users/me/drafts/send")
    assert cap["json"] == {"id": "d1"}
    assert '"sent": true' in out


async def test_label_modifies(monkeypatch, authed):
    cap = {}

    def handler(method, url, **kw):
        cap["json"] = kw.get("json")
        return FakeResp(json_body={"id": "m1", "labelIds": ["INBOX", "STARRED"]})

    _patch(monkeypatch, handler)
    out = await gm.label(message_id="m1", label_ids=["STARRED"])
    assert cap["json"] == {"addLabelIds": ["STARRED"]}
    assert "STARRED" in out


async def test_401_marks_reconnect(monkeypatch, authed):
    _patch(monkeypatch, lambda m, u, **k: FakeResp(status=401))
    out = await gm.get(message_id="m1")
    assert "reconnect" in out.lower()
    assert authed == ["stamp"]


async def test_token_unavailable(monkeypatch):
    def unavailable():
        raise gm.ConnectorTokenError("unavailable", kind="temporary", code="transport")
    monkeypatch.setattr(gm.ctx, "token_info", unavailable)
    output = await gm.list_labels()
    assert "unavailable" in output.lower()
