"""The host an in-turn tool call reaches, for the real-CLI tests.

A tool a real model calls mid-turn is an HTTP request to
`http://127.0.0.1:{settings.port}`, and since the namespaces moved in-process
(polish-2026-09.md §4 B1) there are two halves to it:

* the **REST routes** each tool shims to (`/api/sessions/{sid}/delegations`,
  `…/schedules`, `…/questions`), and
* the **`/mcp/<key>` mounts** the CLI itself connects to over streamable-HTTP.

A test that serves only the first half leaves the model unable to call the tool
at all, which reads exactly like the model declining to call it — the failure
mode `test_delegations_real` was already written to avoid for the REST half.
One helper, so the next transport change has one place to follow rather than a
copy per test file.

Mounting is not enough on its own: each `FastMCP` owns a session manager that
must be running before it will serve, and a mounted sub-app's lifespan is not
run by its parent (see `server/mcp_http.lifespan`). And a host that stops has to
leave the process able to serve the next one, which takes more than closing a
socket — see `_seize_sse_shutdown_latch`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any


def _seize_sse_shutdown_latch() -> None:
    """Take `sse_starlette`'s process-global shutdown latch away from it.

    Every MCP response — `initialize` included — is an `EventSourceResponse`,
    and sse_starlette cuts one short the moment it believes the process is
    shutting down. That belief is a *class attribute*, `AppStatus.should_exit`,
    and it latches on the first uvicorn server in the process to stop, by two
    independent routes: sse_starlette patches `Server.handle_exit` at import,
    and its per-loop watcher reads `signal.getsignal(SIGTERM).__self__` to find
    a server and poll that server's `should_exit` directly. `Server.serve()`
    installs the SIGTERM handler itself (`capture_signals`), so a test's
    `install_signal_handlers = lambda: None` does not keep its server out of
    reach either way.

    Nothing resets the latch. Production never needs it reset — one server,
    stopped once, as the process exits — but a test suite stops servers while
    it keeps running, and from then on every namespace call gets an HTTP 200
    followed by a truncated body. The CLI waits out `MCP_TIMEOUT`, reports every
    namespace unreachable and drops its tools, and the test fails as though the
    model had ignored its instructions. That is the whole of the cross-test
    failure recorded in docs/post-mortems/2026-09-25-polish-pass.md.

    So from here on the latch is set by `CallbackApi.stop()` and by nothing
    else: disabling the automatic drain makes both of sse_starlette's routes
    inert, and `stop()` does the draining itself.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.enable_automatic_graceful_drain = False
    AppStatus.should_exit = False


def _set_sse_shutdown_latch(value: bool) -> None:
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = value


async def _own_namespaces(
    app: Any, servers: dict[str, Any], ready: asyncio.Event, release: asyncio.Event
) -> None:
    """Hold the namespaces' lifespan for as long as this task lives.

    anyio requires the task that entered a cancel scope to be the task that
    exits it, and every MCP session manager enters one. This lifespan is entered
    by `start_callback_api` and left by `CallbackApi.stop()` — two separate
    calls, which nothing obliges a caller to make from the same task. Entering
    on the caller and exiting on the closer therefore works only by luck, and
    raises "Attempted to exit cancel scope in a different task than it was
    entered in" when the luck runs out. A context that outlives the function
    which opened it belongs to a task of its own.
    """
    from server.mcp_http import lifespan as mcp_lifespan

    async with mcp_lifespan(app, servers):
        ready.set()
        await release.wait()


@dataclass
class CallbackApi:
    """A live callback host. `port` is what `settings.port` must be pointed at."""

    port: int
    app: Any
    _server: Any
    _task: asyncio.Task
    _release: asyncio.Event | None
    _owner: asyncio.Task | None

    async def stop(self) -> None:
        # Latch first, so an SSE stream still open ends and uvicorn's graceful
        # shutdown is not left waiting on it. Clear it once this host is gone,
        # so the next one in the process can serve (see the latch helpers).
        _set_sse_shutdown_latch(True)
        try:
            self._server.should_exit = True
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            if self._release is None or self._owner is None:
                return
            self._release.set()
            try:
                await asyncio.wait_for(self._owner, timeout=10.0)
            except (TimeoutError, asyncio.CancelledError):
                self._owner.cancel()
        finally:
            _set_sse_shutdown_latch(False)


async def start_callback_api(
    *routers: Any,
    mcp: bool = True,
    websocket: bool = False,
    overrides: dict[Any, Any] | None = None,
    exception_handlers: dict[Any, Any] | None = None,
    readiness_token: str | None = None,
) -> CallbackApi:
    """Serve `routers` plus (by default) every MCP namespace on a free port.

    `overrides` goes to `app.dependency_overrides`, which is how a test points
    the routes at its own objects now that they take the session manager as a
    dependency rather than reading a module global (server/deps.py).
    """
    import uvicorn
    from fastapi import FastAPI

    from server.mcp_http import build_servers, mount_all

    _seize_sse_shutdown_latch()
    app = FastAPI(exception_handlers=exception_handlers or {})
    for router in routers:
        app.include_router(router)
    app.dependency_overrides.update(overrides or {})
    # Fresh namespace instances, mounted and started as one set: each test gets
    # its own event loop and its own server, and a FastMCP session manager runs
    # exactly once per instance (see mcp_http.build_servers).
    servers = build_servers(fresh=True) if mcp else {}
    if mcp:
        mount_all(app, servers)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        # An ephemeral port per test. A fixed one was tried while chasing the
        # cross-test failure recorded in docs/post-mortems/2026-09-25-polish-pass.md and made
        # no difference, so it buys nothing and can only collide.
        port=0,
        # `OCTOPUS_TEST_CALLBACK_LOG=info` turns the access log on, which is how
        # you find out whether a CLI that reported a namespace as unreachable
        # ever actually asked for it.
        log_level=os.environ.get("OCTOPUS_TEST_CALLBACK_LOG", "warning"),
        lifespan="off",
        # Most callback hosts need HTTP only. Ordinary-chat evaluations opt in
        # to the same WebSocket transport used by the production composer.
        ws="auto" if websocket else "none",
    )
    server = uvicorn.Server(config)

    release: asyncio.Event | None = None
    owner: asyncio.Task | None = None
    task: asyncio.Task | None = None
    try:
        if mcp:
            ready, release = asyncio.Event(), asyncio.Event()
            owner = asyncio.create_task(
                _own_namespaces(app, servers, ready, release), name="mcp-namespaces"
            )
            waiter = asyncio.create_task(ready.wait())
            try:
                done, _ = await asyncio.wait(
                    (owner, waiter), timeout=10.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if owner in done:
                    await owner  # Preserve the namespace startup error itself.
                    raise RuntimeError("MCP namespaces stopped before becoming ready")
                if waiter not in done:
                    raise TimeoutError("MCP namespaces never became ready")
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
        task = asyncio.create_task(server.serve())
        for _ in range(200):
            if task.done():
                await task
                raise RuntimeError("callback API server stopped before becoming ready")
            if server.started and server.servers:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - a stuck uvicorn is a real failure, not a skip
            raise RuntimeError("callback API server never started")
        port = server.servers[0].sockets[0].getsockname()[1]
        if mcp:
            await _await_namespaces(port, servers, readiness_token)
        return CallbackApi(
            port=port,
            app=app,
            _server=server,
            _task=task,
            _release=release,
            _owner=owner,
        )
    except BaseException as startup_error:
        # No handle has reached the caller yet, so startup owns teardown too.
        # Stop HTTP before releasing the namespace lifespan, as stop() does.
        _set_sse_shutdown_latch(True)
        try:
            server.should_exit = True
            for pending in (task, owner):
                if pending is owner and release is not None:
                    release.set()
                if pending is None:
                    continue
                try:
                    await asyncio.wait_for(pending, timeout=10.0)
                except (Exception, asyncio.CancelledError) as cleanup_error:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    if cleanup_error is not startup_error:
                        startup_error.add_note(
                            f"Callback startup cleanup: {type(cleanup_error).__name__}"
                        )
        finally:
            _set_sse_shutdown_latch(False)
        raise


async def _await_namespaces(port: int, servers: dict[str, Any], token: str | None = None) -> None:
    """Don't hand out the port until every namespace has answered in full.

    Cheap — eight in-process requests, a few milliseconds — and worth it: a
    namespace that mounts but does not serve is otherwise discovered by a real
    CLI 25 seconds later, as a turn in which the model mysteriously had no
    tools. Here it is a named failure at setup, on the namespace that broke.

    A *complete* body is the assertion, because the way this has actually
    failed is a 200 with a truncated one — see `_seize_sse_shutdown_latch`.
    """
    import httpx

    from server.mcp_identity import mint

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "callback-api-readiness", "version": "1"},
        },
    }
    headers = {
        "Authorization": f"Bearer {token or mint('callback-api-readiness')}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        for name in servers:
            url = f"http://127.0.0.1:{port}/mcp/{name}/mcp"
            last: Exception | None = None
            for _ in range(20):
                try:
                    reply = await client.post(url, json=initialize, headers=headers)
                    # A complete body is the point: a truncated one is exactly
                    # the failure this exists to keep out of the tests.
                    if reply.status_code == 200 and reply.content:
                        break
                    last = RuntimeError(
                        f"{reply.status_code}, {len(reply.content)} bytes"
                    )
                except Exception as e:  # httpx raises on a truncated response
                    last = e
                await asyncio.sleep(0.1)
            else:  # pragma: no cover - a namespace that never serves is a failure
                raise RuntimeError(f"mcp/{name} never became ready: {last}")
