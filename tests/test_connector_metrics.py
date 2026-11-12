"""Logical resource results cross real provider wrappers and metrics storage."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from server.mcp_servers.connectors import _shared, custom, github, gmail
from server.monitor import Event, MetricsStore, query
from tests.connector_runtime import clock as clock
from tests.connector_runtime import provider_transport as provider_transport


@pytest.fixture
async def metrics(tmp_path, monkeypatch):
    import server.monitor

    store = MetricsStore(str(tmp_path / "metrics.db"))
    await store.initialize()
    monkeypatch.setattr(server.monitor, "monitor", store)
    yield store
    await store.stop()


async def test_token_failure_then_recovery_counts_both_resource_results(provider_transport, metrics):
    tokens = [httpx.Response(503), httpx.Response(200, json={
        "access_token": "provider-token", "grant_stamp": "original-grant",
    })]
    resources = []
    provider_transport(
        lambda request: resources.append(request) or httpx.Response(200, json={"labels": []}),
        token_handler=lambda request: tokens.pop(0),
    )
    assert "Error:" in await gmail.list_labels()
    assert await gmail.list_labels() == "[]"
    assert len(resources) == 1
    await metrics.drain()
    row, = await query.connectors(metrics.conn)
    assert (row["n"], row["ok_n"]) == (2, 1)
    events = await (await metrics.conn.execute(
        "SELECT ok, error_code, detail, duration_ms FROM events ORDER BY rowid"
    )).fetchall()
    assert [(event[0], event[1]) for event in events] == [(0, "token"), (1, None)]
    assert all('"unit": "logical_resource_v1"' in event[2] and event[3] >= 0 for event in events)


async def test_committed_post_with_bad_resource_structure_keeps_unknown_effect(provider_transport, metrics):
    writes = []
    provider_transport(lambda request: writes.append(request) or httpx.Response(200, json=[]))
    output = await gmail.send_draft("original-draft")
    assert len(writes) == 1 and writes[0].method == "POST"
    assert "outcome_unknown" in output and "original-draft" in output and "Check Gmail Sent" in output
    await metrics.drain()
    event, = await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall()
    assert event == (0, "resource_decode")


@pytest.mark.parametrize("tool,arguments,body", [
    (github.get_file, {"repo": "owner/name", "path": "file"}, {"encoding": "base64", "content": "a"}),
    (gmail.get, {"message_id": "original"}, {"payload": {"mimeType": "text/plain", "body": {"data": "a"}}}),
    (gmail.search, {"query": "original"}, {"messages": [{}]}),
])
async def test_resource_decode_failure_is_the_only_sample(provider_transport, metrics, tool, arguments, body):
    requests = []
    provider_transport(lambda request: requests.append(request) or httpx.Response(200, json=body))
    assert "Error:" in await tool(**arguments)
    assert len(requests) == 1
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall() == [(0, "resource_decode")]


async def test_custom_text_that_looks_like_an_error_is_success(provider_transport, metrics):
    provider_transport(lambda request: httpx.Response(200, text="Error: legitimate resource text"))
    assert await custom.request("GET", "/items") == "Error: legitimate resource text"
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall() == [(1, None)]


async def test_custom_bad_path_has_no_resource_or_metric(provider_transport, metrics):
    calls = []
    provider_transport(lambda request: calls.append(request) or httpx.Response(200),
                       token_handler=lambda request: calls.append(request) or httpx.Response(503))
    assert "path must be relative" in await custom.request("POST", "../elsewhere")
    assert calls == []
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT COUNT(*) FROM events")).fetchone() == (0,)


@pytest.mark.parametrize("to,expected", [(None, [(0, "resource_decode")]),
                                       ("chosen@example.com", [(1, None), (1, None)])])
async def test_reply_decodes_only_the_recipient_it_needs(provider_transport, metrics, to, expected):
    methods = []
    parent = {"id": "original", "threadId": "thread", "payload": {"headers": [
        {"name": "Message-ID", "value": "<parent@example.com>"},
        {"name": "Subject", "value": "Original subject"},
        {"name": "Reply-To", "value": "undisclosed-recipients:;"},
    ]}}

    def resource(request):
        methods.append(request.method)
        return httpx.Response(200, json=parent if request.method == "GET" else {"id": "draft"})

    provider_transport(resource)
    output = await gmail.create_draft(to=to, body="Reply", in_reply_to_message_id="original")
    assert ("Error:" in output) == (to is None)
    assert methods == (["GET"] if to is None else ["GET", "POST"])
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events ORDER BY rowid")).fetchall() == expected


async def test_reply_subject_conflict_keeps_the_successful_get_sample(provider_transport, metrics):
    provider_transport(lambda request: httpx.Response(200, json={
        "id": "original", "threadId": "thread", "payload": {"headers": [
            {"name": "Message-ID", "value": "<parent@example.com>"},
            {"name": "Subject", "value": "Original subject"},
        ]},
    }))
    output = await gmail.create_draft(to="chosen@example.com", subject="Different", in_reply_to_message_id="original")
    assert "Subject must match" in output
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall() == [(1, None)]


@pytest.mark.parametrize("provider", ["github", "gmail", "custom"])
@pytest.mark.parametrize("case,code,attempts", [
    ("401", "auth", 1), ("404", "http", 1), ("503", "retry_exhausted", 3),
    ("budget", "budget", 1), ("transport", "transport", 1), ("json", "json_decode", 1),
])
async def test_each_failed_resource_has_one_finite_category(provider_transport, metrics, clock, provider, case, code, attempts):
    calls = []

    def resource(request):
        calls.append(request)
        if case == "transport":
            raise httpx.ReadError("synthetic transport failure")
        if case == "json":
            return httpx.Response(200, text="not JSON")
        return httpx.Response(503 if case == "budget" else int(case),
                              headers={"Retry-After": "60"} if case == "budget" else {})

    provider_transport(resource)
    if provider == "github":
        output = await github.list_repos()
    elif provider == "gmail":
        output = await gmail.list_labels()
    else:
        output = await custom.request("GET", "/items")
    text_fallback = provider == "custom" and case == "json"
    assert ("Error:" in output) != text_fallback
    assert len(calls) == attempts
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall() == [
        (1, None) if text_fallback else (0, code),
    ]


@pytest.mark.parametrize("provider", ["github", "gmail", "custom"])
async def test_write_503_is_http_failure_without_retry_exhaustion(provider_transport, metrics, provider):
    calls = []
    provider_transport(lambda request: calls.append(request) or httpx.Response(503))
    if provider == "github":
        output = await github.create_issue("owner/name", "Original")
    elif provider == "gmail":
        output = await gmail.send_draft("original")
    else:
        output = await custom.request("POST", "/items")
    assert "outcome_unknown" in output and len(calls) == 1
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall() == [(0, "http")]


async def test_retry_success_duration_includes_token_body_and_backoff(provider_transport, metrics, clock):
    calls = []

    def token(request):
        clock.now += .2
        return httpx.Response(200, json={"access_token": "provider-token", "grant_stamp": "original-grant"})

    def resource(request):
        calls.append(request)
        clock.now += .3
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"labels": []})

    provider_transport(resource, token_handler=token)
    assert await gmail.list_labels() == "[]"
    assert len(calls) == 2 and clock.waits == [1.0]
    await metrics.drain()
    event, = await (await metrics.conn.execute("SELECT ok, error_code, duration_ms, detail FROM events")).fetchall()
    assert event[:2] == (1, None)
    assert event[2] == pytest.approx(1800)
    assert "provider-token" not in event[3] and "host.invalid" not in event[3]


async def test_search_counts_list_and_each_detail_as_separate_resources(provider_transport, metrics):
    calls = []

    def resource(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "one"}, {"id": "two"}]})
        if request.url.path.endswith("/one"):
            return httpx.Response(200, json={"id": "one", "snippet": "resource content"})
        return httpx.Response(404)

    provider_transport(resource)
    output = await gmail.search("original")
    assert '"complete": false' in output and "resource content" in output and "two" in output
    assert len(calls) == 3
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events ORDER BY rowid")).fetchall() == [
        (1, None), (1, None), (0, "http"),
    ]


async def test_cancelled_write_records_original_installation_without_replay(provider_transport, metrics, monkeypatch):
    entered, closed = asyncio.Event(), asyncio.Event()
    calls = []

    class PendingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Future()
            yield b"unreachable"

        async def aclose(self):
            closed.set()

    provider_transport(lambda request: calls.append(request) or httpx.Response(200, stream=PendingBody()))
    task = asyncio.create_task(gmail.send_draft("original"))
    await asyncio.wait_for(entered.wait(), 2)
    monkeypatch.setenv("OCTOPUS_INSTALLATION_ID", "replacement")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and len(calls) == 1
    await metrics.drain()
    event, = await (await metrics.conn.execute("SELECT ok, error_code, detail FROM events")).fetchall()
    assert event[:2] == (0, "cancelled")
    assert "installation-original" in event[2] and "replacement" not in event[2]


@pytest.mark.parametrize("failure_at", ["record", "storage"])
async def test_metrics_failure_cannot_change_or_repeat_resource(provider_transport, metrics, monkeypatch, failure_at):
    calls = []
    provider_transport(lambda request: calls.append(request) or httpx.Response(200, json={"labels": []}))
    if failure_at == "record":
        def fail_record(event):
            raise OSError("synthetic queue failure")
        monkeypatch.setattr(metrics, "record", fail_record)
    else:
        async def fail_write(*args, **kwargs):
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(metrics.conn, "executemany", fail_write)
    assert await gmail.list_labels() == "[]"
    assert await metrics.drain() == 0
    assert len(calls) == 1


async def test_unexpected_decoder_defect_propagates_and_records_internal(provider_transport, metrics, monkeypatch):
    provider_transport(lambda request: httpx.Response(200, json={"labels": []}))

    def broken_decoder(value):
        # Inject a programming defect, rather than malformed provider data.
        raise RuntimeError("synthetic decoder defect")

    monkeypatch.setattr(gmail, "_labels", broken_decoder)
    with pytest.raises(RuntimeError, match="synthetic decoder defect"):
        await gmail.list_labels()
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall() == [(0, "internal")]


async def test_resource_body_deadline_is_budget_failure_not_caller_cancellation(provider_transport, metrics, monkeypatch):
    closed = asyncio.Event()
    calls = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Future()
            yield b"unreachable"

        async def aclose(self):
            closed.set()

    monkeypatch.setattr(_shared, "RESOURCE_CALL_BUDGET_SECONDS", .02)
    provider_transport(lambda request: calls.append(request) or httpx.Response(200, stream=SlowBody()))
    output = await custom.request("POST", "/original")
    assert "outcome_unknown" in output and "budget" in output
    assert len(calls) == 1 and closed.is_set()
    await metrics.drain()
    assert await (await metrics.conn.execute("SELECT ok, error_code FROM events")).fetchall() == [(0, "budget")]


async def test_provider_results_reach_authenticated_api_and_cli_with_legacy_samples(
    provider_transport, metrics, monkeypatch, capsys,
):
    from fastapi import FastAPI

    from server import deps
    from server.cli import do_monitor
    from server.config import settings
    from server.database import Database
    from server.routers.monitor import router
    from tests.accounts import create_account

    responses = [httpx.Response(401), httpx.Response(200, json={"labels": []}),
                 httpx.Response(200, json={"labels": []})]
    provider_transport(lambda request: responses.pop(0))
    assert "reconnect" in await gmail.list_labels()
    assert await gmail.list_labels() == await gmail.list_labels() == "[]"
    for installation, count in (("installation-original", 11), ("legacy-only", 7)):
        for _ in range(count):
            metrics.record(Event(kind="connector_call", ok=False, duration_ms=999_999,
                                 detail={"installation_id": installation}))
    await metrics.drain()
    monkeypatch.setattr(settings, "metrics_db_path", metrics._db_path)
    monkeypatch.setattr(deps, "_user_manager", None)
    database = Database(":memory:")
    await database.initialize()
    try:
        _, token = await create_account(database)
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/monitor/connectors")).status_code == 401
            headers = {"Authorization": "Bearer " + token}
            response = await client.get("/api/monitor/connectors?window=1h", headers=headers)
            assert response.status_code == 200, response.text
            rows = response.json()["rows"]
            measured, legacy = rows
            assert (measured["installation"], measured["n"], measured["ok_n"],
                    measured["success_pct"], measured["legacy_failure_samples"]) == ("installation-original", 3, 2, 66.7, 11)
            assert measured["last_seen"] is not None and 0 <= measured["avg_ms"] <= measured["max_ms"] < 999_999
            assert (legacy["n"], legacy["ok_n"], legacy["success_pct"], legacy["last_seen"]) == (0, 0, None, None)
            overview = (await client.get("/api/monitor/overview?window=1h", headers=headers)).json()
            assert overview["connectors"] == rows
            assert overview["counts_unit"] == "event_samples"
            count, = overview["counts"]
            assert (count["kind"], count["n"], count["failures"]) == ("connector_call", 21, 19)
        for report in ("connectors", "overview"):
            with pytest.raises(SystemExit) as exit_info:
                await asyncio.to_thread(do_monitor, SimpleNamespace(report=report, since="1h"))
            assert exit_info.value.code == 0
            output = capsys.readouterr().out
            assert "logical resource requests" in output and "legacy_failure_samples" in output
            line = next(line for line in output.splitlines() if line.strip().startswith("installation-original"))
            assert line.split()[1:4] == ["3", "2", "66.7"] and line.split()[-1] == "11"
            old = next(line for line in output.splitlines() if line.strip().startswith("legacy-only"))
            assert old.split()[1:] == ["0", "0", "-", "-", "-", "-", "7"]
            if report == "overview":
                assert "event samples" in output and "top error event samples" in output
    finally:
        await database.close()
