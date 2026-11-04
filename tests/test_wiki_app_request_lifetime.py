"""Wiki HTTP 202 retains original process and journal responsibility."""

import asyncio
import gc
import json
import os
import signal
import weakref
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from server import app_backends
from server.app_backends import BackendCapacityError, BackendSupervisor
from server.routers import applications as route
from server.wiki import app_bridge
from tests.app_proxy import application, write_backend
from tests.test_wiki_history import sapphire as sapphire


@pytest.fixture
async def bridge(sapphire, monkeypatch):
    row, history = sapphire
    directory = Path(row["app_dir"])
    write_backend(directory, """
import http.server, json, os, threading, time
from pathlib import Path
class Handler(http.server.BaseHTTPRequestHandler):
 def reply(self, code, payload):
  data = b'not a receipt' if Path('invalid-receipt').exists() and code == 202 else json.dumps(payload).encode()
  self.send_response(code)
  self.send_header('Content-Length', str(len(data)))
  self.end_headers()
  if Path('hold-receipt').exists() and code == 202:
   self.wfile.write(data[:1])
   self.wfile.flush()
   Path('receipt-started').touch()
   while not Path('release-receipt').exists(): time.sleep(.01)
   data = data[1:]
  self.wfile.write(data)
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length', 0)))
  Path('mutations').open('a').write('once\\n')
  def work():
   while not Path('complete').exists(): time.sleep(.01)
   Path(os.environ['APP_DATA_DIR'], 'vaults/obv/repo/note.md').write_text('committed effect')
   Path('done').touch()
  threading.Thread(target=work, daemon=True).start()
  self.reply(202, {'id':'original-job'})
 def do_GET(self):
  Path('polls').open('a').write('poll\\n')
  self.reply(200, {'id':'original-job', 'status':'done' if Path('done').exists() else 'running'})
 def log_message(self, *args): pass
http.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
""")
    supervisor = BackendSupervisor()

    async def authorized(*args):
        return row

    monkeypatch.setattr(route, "backend_supervisor", supervisor)
    monkeypatch.setattr(route, "_application_for_request", authorized)
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    try:
        yield supervisor, row, history
    finally:
        await supervisor.shutdown()
        await app_bridge.shutdown()
        app_bridge.reopen()


async def dispatch(row):
    body = json.dumps({"action": "sync"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": f"/apps/{row['id']}/api/git",
        "headers": [(b"content-length", str(len(body)).encode())], "query_string": b"vault=obv",
    }, receive)
    return await route.proxy_application_api(request, row["id"], "git")


async def wait_until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.01)


async def test_202_handoff_protects_capacity_and_drain_until_effect_and_journal_finish(bridge, tmp_path):
    supervisor, row, history = bridge
    directory = Path(row["app_dir"])
    response = await dispatch(row)
    assert response.status_code == 202
    await wait_until(lambda: (directory / "polls").exists())
    assert supervisor.drain_blockers() and app_bridge.drain_blockers()
    with pytest.raises(BackendCapacityError):
        await supervisor.ensure_running("other", str(application(tmp_path, "other")))
    assert history.store.path("note.md").read_bytes() == b"original"
    (directory / "complete").touch()
    await wait_until(lambda: not supervisor.drain_blockers() and not app_bridge.drain_blockers())
    assert history.store.read("note.md")["content"] == "committed effect"
    assert len(history.history("note.md")["items"]) == 1
    assert (directory / "mutations").read_text() == "once\n"
    assert not (history.state / "external-change.json").exists()


@pytest.mark.parametrize("trigger", ["stop", "cancel", "lost-target"])
async def test_follower_exit_keeps_original_target_and_never_replays_mutation(bridge, trigger):
    supervisor, row, history = bridge
    directory = Path(row["app_dir"])
    assert (await dispatch(row)).status_code == 202
    await wait_until(lambda: (directory / "polls").exists())
    follower = next(task for task in app_bridge._pending if task.get_name() == "wiki-app-original-job")
    state = supervisor.get(row["id"])
    original, port = state.process, state.port
    replacement = None
    received = []

    async def stranger(reader, writer):
        received.append(await reader.read(4096))
        writer.close()
        await writer.wait_closed()

    try:
        if trigger == "stop":
            await asyncio.wait_for(supervisor.stop(row["id"]), 4)
        elif trigger == "cancel":
            follower.cancel()
        else:
            os.killpg(original.pid, signal.SIGKILL)
            await original.wait()
            replacement = await asyncio.start_server(stranger, "127.0.0.1", port)
        await wait_until(lambda: not supervisor.drain_blockers())
        assert follower.done()
        assert original.returncode is not None
        assert received == []
        assert (directory / "mutations").read_text() == "once\n"
        assert history.store.path("note.md").read_bytes() == b"original"
        assert not (history.state / "external-change.json").exists()
    finally:
        if replacement is not None:
            replacement.close()
            await replacement.wait_closed()


@pytest.mark.parametrize("trigger", ["disconnect", "stop"])
async def test_partial_mutation_receipt_keeps_original_cleanup_without_stop_wait_cycle(bridge, trigger):
    supervisor, row, history = bridge
    directory = Path(row["app_dir"])
    (directory / "hold-receipt").touch()
    pending = asyncio.create_task(dispatch(row))
    await wait_until(lambda: (directory / "receipt-started").exists())
    original = supervisor.get(row["id"]).process
    if trigger == "disconnect":
        pending.cancel()
    else:
        await asyncio.wait_for(supervisor.stop(row["id"]), 4)
    result = (await asyncio.gather(pending, return_exceptions=True))[0]
    assert isinstance(result, BaseException)
    await wait_until(lambda: not supervisor.drain_blockers() and not app_bridge.drain_blockers())
    assert original.returncode is not None
    assert (directory / "mutations").read_text() == "once\n"
    assert history.store.read("note.md")["content"] == "original"
    assert not (history.state / "external-change.json").exists()


@pytest.mark.parametrize("failure", ["receipt", "handoff"])
async def test_unaccepted_follower_handoff_confirms_process_and_journal_retirement(bridge, monkeypatch, failure):
    from server.wiki.store import WikiError

    supervisor, row, history = bridge
    directory = Path(row["app_dir"])
    if failure == "receipt":
        (directory / "invalid-receipt").touch()
        expected = WikiError
    else:
        def refuse_handoff(*args):
            raise RuntimeError("Follower handoff rejected")

        monkeypatch.setattr(app_bridge.AppMutation, "follow_job", refuse_handoff)
        expected = RuntimeError
    with pytest.raises(expected):
        await dispatch(row)
    await wait_until(lambda: not supervisor.drain_blockers() and not app_bridge.drain_blockers())
    assert supervisor.get(row["id"]).process is None
    assert (directory / "mutations").read_text() == "once\n"
    assert history.store.read("note.md")["content"] == "original"
    assert not (history.state / "external-change.json").exists()


async def test_failed_poll_response_close_survives_a_later_done_receipt(sapphire, monkeypatch):
    row, history = sapphire
    supervisor = BackendSupervisor()
    directory = Path(row["app_dir"])
    write_backend(directory, "import http.server,os; http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),http.server.BaseHTTPRequestHandler).serve_forever()")
    monkeypatch.setattr(app_bridge, "_pending", {})
    real_client = httpx.AsyncClient
    polls = 0
    closes = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, payload, number):
            self.payload, self.number = payload, number

        async def __aiter__(self):
            yield json.dumps(self.payload).encode()

        async def aclose(self):
            closes.append(self.number)
            if self.number == 1:
                raise OSError("Original poll close unconfirmed")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal polls
            if request.method == "POST":
                return httpx.Response(202, stream=Stream({"id": "original-job"}, 0))
            polls += 1
            if polls >= 2:
                history.store.path("note.md").write_text("confirmed job effect")
            return httpx.Response(200, stream=Stream({"id": "original-job", "status": "running" if polls == 1 else "done"}, polls))

        async def aclose(self):
            closes.append("client")

    async def authorized(*args):
        return row

    monkeypatch.setattr(route, "backend_supervisor", supervisor)
    monkeypatch.setattr(route, "_application_for_request", authorized)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=Transport(), **kwargs))
    assert (await dispatch(row)).status_code == 202
    follower = next(iter(app_bridge._pending))
    await asyncio.wait_for(asyncio.gather(follower, return_exceptions=True), 3)
    try:
        assert supervisor.drain_blockers(), "A later done receipt cannot retire an unconfirmed original response close"
        assert app_bridge.drain_blockers()
        assert polls == 2 and closes == [0, 1, 2, "client"]
        assert history.store.read("note.md")["content"] == "confirmed job effect"
    finally:
        # The fault owns no real network handles; keep the unconfirmed use and
        # journal diagnosis intact while retiring this probe's actual process.
        with pytest.raises(RuntimeError, match="request cleanup unconfirmed"):
            await supervisor.shutdown()
    assert supervisor.get(row["id"]).process.returncode is not None


async def test_closed_poll_responses_are_released_while_original_job_use_remains_active(bridge, monkeypatch):
    supervisor, row, history = bridge
    real_client = httpx.AsyncClient
    held, release = asyncio.Event(), asyncio.Event()
    responses = []
    polls = 0

    async def backend(request):
        nonlocal polls
        if request.method == "POST":
            return httpx.Response(202, json={"id": "original-job"})
        polls += 1
        if polls == 4:
            held.set()
            await release.wait()
            history.store.path("note.md").write_text("final effect")
        response = httpx.Response(200, json={"id": "original-job", "status": "done" if polls == 4 else "running"})
        responses.append(weakref.ref(response))
        return response

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(backend), **kwargs))
    assert (await dispatch(row)).status_code == 202
    try:
        await asyncio.wait_for(held.wait(), 3)
        gc.collect()
        assert len(responses) == 3 and all(reference() is None for reference in responses[:2])
        assert supervisor.drain_blockers() and app_bridge.drain_blockers()
    finally:
        release.set()
    await wait_until(lambda: not supervisor.drain_blockers() and not app_bridge.drain_blockers())
    assert history.store.read("note.md")["content"] == "final effect"
