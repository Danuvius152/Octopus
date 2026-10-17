"""The hermetic tier must never pay a model or reach the live callback API."""

import os
import re
import socket
import subprocess
from pathlib import Path

import httpx


class Guard(list):
    """Violations plus explicitly registered executable test doubles."""

    def __init__(self):
        super().__init__()
        self.fake_executables = set()

    def allow_fake_executable(self, path):
        self.fake_executables.add(Path(path).resolve())


def install(monkeypatch):
    violations = Guard()
    spawn = subprocess.Popen.__init__
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    http = httpx.HTTPTransport.handle_request
    async_http = httpx.AsyncHTTPTransport.handle_async_request

    def forbidden(message):
        violations.append(message)
        raise AssertionError(message)

    def guarded_spawn(self, args, *positional, **kwargs):
        command = os.fsdecode(args) if isinstance(args, (str, bytes)) else os.fsdecode(args[0])
        executable = os.fsdecode(kwargs.get("executable") or command)
        shell = kwargs.get("shell") or os.path.basename(executable) in {"sh", "bash", "zsh"}
        script = " ".join(os.fsdecode(part) for part in args) if shell and not isinstance(args, (str, bytes)) else command
        is_cli = os.path.basename(executable) in {"claude", "codex"} or (shell and re.search(r"(?:^|[/\s])(?:claude|codex)(?:[\s\x00]|$)", script))
        if is_cli and Path(executable).resolve() not in violations.fake_executables:
            forbidden("Hermetic test attempted a real model CLI; replace its execution boundary or mark the test real")
        return spawn(self, args, *positional, **kwargs)

    def checked(method, self, address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 8000:
            forbidden("Hermetic test attempted the production callback port 8000; use an isolated callback server")
        return method(self, address)

    def check_url(request):
        if request.url.port == 8000:
            forbidden("Hermetic test attempted the production callback port 8000; use an isolated callback server")

    def guarded_http(self, request):
        check_url(request)
        return http(self, request)

    async def guarded_async_http(self, request):
        check_url(request)
        return await async_http(self, request)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_spawn)
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: checked(connect, self, address))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: checked(connect_ex, self, address))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_async_http)
    return violations
