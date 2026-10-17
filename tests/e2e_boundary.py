"""Fail-closed process/network boundary for the offline Playwright backend.

Installed before importing the application. The append-only audit is checked
by the parent runner *after* Playwright has stopped both backend processes;
catching a refusal in business code cannot turn a violation into a pass.
"""

import asyncio
import ipaddress
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

OFFLINE_CLI = Path(__file__).with_name("e2e_offline_cli.py").resolve()
OFFLINE_ARGV = [sys.executable, str(OFFLINE_CLI)]


def install(audit_path: Path):
    def record(kind, **fields):
        # No argv, environment, URL, prompt, bearer or request body in evidence.
        line = json.dumps({"kind": kind, **fields}) + "\n"
        with audit_path.open("a") as stream:
            stream.write(line)

    def refuse(reason):
        record("violation", reason=reason)
        raise RuntimeError(f"Offline E2E boundary: {reason}")

    def check_url(url):
        parsed = urlsplit(str(url))
        check_address((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)))

    def check_address(address):
        if not isinstance(address, tuple):
            return  # Unix-domain socket, not a remote callback.
        host, port = address[:2]
        if port == 8000:
            refuse("production_callback")
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            refuse("external_network")

    def audit(event, args):
        if event in {"subprocess.Popen", "os.posix_spawn", "os.exec"}:
            executable, argv = args[:2]
            check_process(executable, argv)
        elif event in {"os.system", "os.fork", "os.forkpty"}:
            refuse("process_dispatch")
        elif event == "socket.connect":
            check_address(args[1])
        elif event == "urllib.Request":
            check_url(args[0])

    def check_process(executable, argv):
        if os.fsdecode(executable) != sys.executable or list(argv) != OFFLINE_ARGV:
            refuse("process_dispatch")

    # uvloop can launch without subprocess.Popen's audit event. Intercept the
    # async API too; the offline server also explicitly uses the stdlib loop.
    async_exec = asyncio.create_subprocess_exec

    async def guarded_exec(*argv, **kwargs):
        check_process(kwargs.get("executable") or argv[0], argv)
        process = await async_exec(*argv, **kwargs)
        record("offline_dispatch")
        return process

    async def guarded_shell(*args, **kwargs):
        refuse("process_dispatch")

    asyncio.create_subprocess_exec = guarded_exec
    asyncio.create_subprocess_shell = guarded_shell
    # Non-removable hooks cover synchronous/low-level dispatch and sockets.
    sys.addaudithook(audit)
    http = httpx.HTTPTransport.handle_request
    async_http = httpx.AsyncHTTPTransport.handle_async_request

    def guarded_http(self, request):
        check_url(request.url)  # Before a proxy can hide the destination port.
        return http(self, request)

    async def guarded_async_http(self, request):
        check_url(request.url)
        return await async_http(self, request)

    httpx.HTTPTransport.handle_request = guarded_http
    httpx.AsyncHTTPTransport.handle_async_request = guarded_async_http
    record("guard_installed")
    return record
