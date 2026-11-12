"""Public tools retain uncertain effects without replaying provider writes."""

import asyncio
import base64
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from server.mcp_servers.connectors import _shared, custom, github, gmail
from tests.connector_runtime import clock as clock
from tests.connector_runtime import provider_transport as provider_transport

WRITES = [
    (github.create_issue, {"repo": "owner/project", "title": "Original issue"}),
    (github.comment, {"repo": "owner/project", "number": 7, "body": "Original comment"}),
    (gmail.create_draft, {"to": "reader@example.com", "subject": "Original subject", "body": "Original text"}),
    (gmail.send_draft, {"draft_id": "original-draft"}),
    (gmail.label, {"message_id": "original-message", "label_ids": ["STARRED"]}),
    (gmail.unlabel, {"message_id": "original-message", "label_ids": ["STARRED"]}),
    (custom.request, {"method": "POST", "path": "/items", "body": {"title": "Original item"}}),
    (custom.request, {"method": "PUT", "path": "/items/original"}),
    (custom.request, {"method": "DELETE", "path": "/items/original"}),
]

READS = [github.list_repos, gmail.list_labels, lambda: custom.request("GET", "/items")]


@pytest.mark.parametrize("tool,arguments", WRITES, ids=[
    "issue", "comment", "draft", "send", "label", "unlabel", "custom-post", "custom-put", "custom-delete",
])
async def test_committed_write_with_503_is_not_replayed(provider_transport, tool, arguments):
    effects = []

    def committed(request):
        effects.append((request.method, str(request.url), request.content))
        return httpx.Response(503, headers={"Retry-After": "0"})

    provider_transport(committed)
    output = await tool(**arguments)
    assert len(effects) == 1
    assert "outcome_unknown" in output
    assert "Original" in output or "original" in output
    assert "Check" in output or "check" in output


@pytest.mark.parametrize("path,encoded", [
    ("README.md", b"README.md"),
    ("目录/space #?%.txt", b"%E7%9B%AE%E5%BD%95/space%20%23%3F%25.txt"),
    ("dir/%2F-%2e%2e.txt", b"dir/%252F-%252e%252e.txt"),
    ("%2e%2e/file", b"%252e%252e/file"),
])
async def test_github_file_keeps_raw_filename_and_ref_meaning(provider_transport, path, encoded):
    requests = []
    ref = "feature/中 #?%+&"

    def resource(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.url.raw_path.split(b"?", 1)[0] == b"/repos/owner/re.po-s_1/contents/" + encoded
        assert dict(request.url.params) == {"ref": ref}
        assert request.url.query == b"ref=feature%2F%E4%B8%AD+%23%3F%25%2B%26"
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"correct resource").decode()})

    provider_transport(resource)
    assert await github.get_file(repo="owner/re.po-s_1", path=path, ref=ref) == "correct resource"
    assert len(requests) == 1


@pytest.mark.parametrize("repo,path", [
    ("", "file"), ("owner", "file"), ("/name", "file"),
    ("owner/", "file"), ("owner/name/extra", "file"),
    ("./name", "file"), ("owner/..", "file"),
    ("owner/name", "/absolute"), ("owner/name", "//host/file"),
    ("owner/name", "."), ("owner/name", ".."),
    ("owner/name", "./file"), ("owner/name", "src/../file"),
    ("owner/name", "src/./file"), ("owner/name", "src//file"),
    ("owner/name", "src/"),
])
async def test_github_file_rejects_ambiguous_inputs_without_requesting_a_resource(provider_transport, repo, path):
    requests = []

    def resource(request):
        requests.append(request)
        return httpx.Response(200, json={})

    provider_transport(resource)
    result = await github.get_file(repo=repo, path=path)
    assert result.startswith("Error:") and ("repo" in result or "path" in result)
    assert requests == []


@pytest.mark.parametrize("path", ["", "src"])
async def test_github_file_keeps_root_and_directory_listings(provider_transport, path):
    def resource(request):
        assert request.url.raw_path == b"/repos/owner/name/contents/" + path.encode()
        return httpx.Response(200, json=[{"name": "child.txt", "type": "file"}])

    provider_transport(resource)
    result = await github.get_file(repo="owner/name", path=path)
    assert '"name": "child.txt"' in result and '"type": "file"' in result


@pytest.mark.parametrize("status", [401, 302, 400, 403, 404, 422])
async def test_custom_write_keeps_uncertain_effect_separate_from_authorization(provider_transport, status):
    effects = []

    def committed(request):
        effects.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://other.invalid/items"})

    provider_transport(committed)
    output = await custom.request("POST", "/original-operation", body={"value": "original"})
    assert effects == ["https://custom.invalid/v1/original-operation"]
    assert "outcome_unknown" in output
    assert "original-operation" in output
    if status == 401:
        assert "reconnect" in output
    if status == 302:
        assert "redirect refused" in output


@pytest.mark.parametrize("tool", READS, ids=["github", "gmail", "custom"])
async def test_retry_after_exceeding_budget_returns_without_shortening_wait(provider_transport, clock, tool):
    requests = []

    def limited(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    provider_transport(limited)
    output = await tool()
    assert len(requests) == 1
    assert clock.waits == []
    assert "rate_limited" in output
    assert "60" in output and "budget" in output


@pytest.mark.parametrize("headers,message", [
    ({"Retry-After": "60"}, "secondary rate limit"),
    ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000060"}, "API rate limit exceeded"),
    ({}, "You have exceeded a secondary rate limit"),
])
async def test_github_403_rate_limit_is_not_permission_rejection(provider_transport, clock, headers, message):
    requests = []

    def limited(request):
        requests.append(request)
        return httpx.Response(403, headers=headers, json={"message": message})

    provider_transport(limited)
    output = await github.list_repos()
    assert len(requests) == 1
    assert clock.waits == []
    assert "rate_limited" in output and "60" in output and "budget" in output
    assert "rejected" not in output


@pytest.mark.parametrize("tool,arguments", WRITES)
@pytest.mark.parametrize("fault", ["transport", "429"])
async def test_unknown_write_keeps_original_identity_on_other_failures(provider_transport, tool, arguments, fault):
    effects = []

    def committed(request):
        effects.append((str(request.url), request.content))
        if fault == "transport":
            raise httpx.ReadError("response lost after commit", request=request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    provider_transport(committed)
    output = await tool(**arguments)
    assert len(effects) == 1
    assert "outcome_unknown" in output and "Do not repeat" in output


@pytest.mark.parametrize("tool", READS)
@pytest.mark.parametrize("date_header", [False, True])
async def test_safe_read_respects_seconds_and_http_date_then_recovers(provider_transport, clock, tool, date_header):
    requests = []
    header = format_datetime(datetime.fromtimestamp(clock.time() + 12, UTC), usegmt=True) if date_header else "12"

    def resource(request):
        requests.append((clock.now, request.extensions["timeout"]["read"]))
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": header})
        return httpx.Response(200, json={"labels": []} if tool is gmail.list_labels else [])

    provider_transport(resource)
    assert await tool() == "[]"
    assert requests == [(0.0, 30.0), (12.0, 18.0)]
    assert clock.waits == [12.0]


@pytest.mark.parametrize("tool", READS)
async def test_network_and_backoff_share_one_budget(provider_transport, clock, tool):
    requests = []

    def resource(request):
        requests.append(clock.now)
        clock.now += 10
        return httpx.Response(503, headers={"Retry-After": "6"})

    provider_transport(resource)
    output = await tool()
    assert requests == [0.0, 16.0]
    assert clock.waits == [6.0]
    assert "required wait 6s" in output and "budget (4s)" in output


@pytest.mark.parametrize("tool", READS)
async def test_safe_reads_have_at_most_three_attempts(provider_transport, clock, tool):
    requests = []

    def resource(request):
        requests.append(clock.now)
        return httpx.Response(503)

    provider_transport(resource)
    output = await tool()
    assert requests == [0.0, 1.0, 3.0]
    assert clock.waits == [1.0, 2.0]
    assert "three attempts exhausted" in output


@pytest.mark.parametrize("tool", READS)
async def test_late_wakeup_cannot_send_another_request(provider_transport, clock, monkeypatch, tool):
    requests = []

    async def late_sleep(delay):
        clock.now += 31

    monkeypatch.setattr(_shared.asyncio, "sleep", late_sleep)
    provider_transport(lambda request: requests.append(request) or httpx.Response(503))
    output = await tool()
    assert len(requests) == 1
    assert "budget exhausted during wait" in output


@pytest.mark.parametrize("tool", READS)
async def test_transport_errors_still_fail_without_retry(provider_transport, clock, tool):
    requests = []

    def resource(request):
        requests.append(request)
        raise httpx.ConnectError("connection unavailable", request=request)

    provider_transport(resource)
    output = await tool()
    assert len(requests) == 1 and clock.waits == []
    assert "connection unavailable" in output and "outcome_unknown" not in output


@pytest.mark.parametrize("tool,arguments", WRITES[:2] + WRITES[2:4])
async def test_documented_builtin_rejection_is_distinct_from_unknown(provider_transport, tool, arguments):
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(400, json={"error": "invalid input"}))
    output = await tool(**arguments)
    assert len(requests) == 1
    assert "rejected" in output and "outcome_unknown" not in output


async def test_github_read_recovers_after_primary_limit_reset(provider_transport, clock):
    requests = []

    def resource(request):
        requests.append(clock.now)
        if len(requests) == 1:
            return httpx.Response(403, headers={
                "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000012", "Retry-After": "8",
            })
        return httpx.Response(200, json=[])

    provider_transport(resource)
    assert await github.list_repos() == "[]"
    assert requests == [0.0, 12.0] and clock.waits == [12.0]


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_resource_deadline_cancels_body_read_and_closes_response(provider_transport, monkeypatch, method):
    started, closed = asyncio.Event(), asyncio.Event()
    requests = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b'{"unfinished":'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    provider_transport(lambda request: requests.append(request) or httpx.Response(200, stream=SlowBody()))
    monkeypatch.setattr(_shared, "RESOURCE_CALL_BUDGET_SECONDS", 0.05)
    output = await asyncio.wait_for(custom.request(method, "/original-item"), timeout=1)
    assert started.is_set() and closed.is_set()
    assert len(requests) == 1
    assert "budget" in output
    assert ("outcome_unknown" in output) == (method == "POST")


async def test_cancelled_write_retires_response_without_retry(provider_transport):
    started, closed = asyncio.Event(), asyncio.Event()
    requests = []

    class PendingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    provider_transport(lambda request: requests.append(request) or httpx.Response(200, stream=PendingBody()))
    task = asyncio.create_task(custom.request("POST", "/original-item"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and len(requests) == 1


@pytest.mark.parametrize("header", ["bad-date", "-1", "1.5", "NaN", "inf", ""])
async def test_invalid_retry_hint_uses_bounded_read_backoff(provider_transport, clock, header):
    requests = []

    def resource(request):
        requests.append(clock.now)
        return httpx.Response(503, headers={"Retry-After": header}) if len(requests) == 1 else httpx.Response(200, json=[])

    provider_transport(resource)
    assert await github.list_repos() == "[]"
    assert requests == [0.0, 1.0]


async def test_large_unknown_write_retains_bounded_reconciliation_message(provider_transport):
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(503))
    output = await custom.request("POST", "/original-operation", body={"text": "x" * 100_000})
    assert len(requests) == 1
    assert len(output.encode()) < 33_000
    assert "outcome_unknown" in output and "Check" in output and "Do not repeat" in output


@pytest.mark.parametrize("consumer,name,arguments", [
    (github, "create_issue", {"repo": "owner/project", "title": "Original issue"}),
    (gmail, "send_draft", {"draft_id": "original-draft"}),
    (custom, "request", {"method": "POST", "path": "/original-item"}),
])
async def test_mcp_consumer_receives_unknown_write_result(provider_transport, consumer, name, arguments):
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(503))
    result = await consumer.mcp.call_tool(name, arguments)
    blocks = result[0] if isinstance(result, tuple) else result
    output = "".join(block.text for block in blocks)
    assert len(requests) == 1
    assert "outcome_unknown" in output and "Do not repeat" in output
