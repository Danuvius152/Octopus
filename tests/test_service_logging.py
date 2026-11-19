"""Request logs at Octopus's actual Uvicorn and managed service entries."""

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.sync.client import connect

from server.service_installation import environment_path
from tests.installation_settings import installation_settings
from tests.installation_settings import provider_homes as provider_homes
from tests.service_supervisor_support import (
    is_service_launch_option,
    isolated_launch_environment,
    isolated_unit,
    ready,
    setup_account,
    systemctl,
    wait_for,
)

REPO = Path(__file__).resolve().parents[1]
FILE_PATH = "/api/sessions/absent%3Fpreview/files"
INVALID_BEARER = "isolated-invalid-account-bearer"
DENIED_QUERY = "isolated-denied-handshake-query"
FAILING_BEARER = "isolated-account-lookup-failure"
HOST_KEY_SENTINEL = "synthetic-ambient-master-key"


@pytest.fixture(autouse=True)
def inherited_launch_environment(monkeypatch):
    monkeypatch.setenv("octopus_master_key", HOST_KEY_SENTINEL)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    for name in tuple(os.environ):
        if name.upper() == "NO_PROXY":
            monkeypatch.delenv(name)


@pytest.fixture
def account_lookup_fault(tmp_path):
    # Both launchers keep their production entry points. Only the synthetic
    # account lookup below fails in the disposable service processes.
    (tmp_path / "sitecustomize.py").write_text(
        "from server.users import UserManager\n"
        "resolve_token = UserManager.resolve_token\n"
        "async def resolve_with_failure(self, token, *, touch=True):\n"
        f"    if token == {FAILING_BEARER!r}:\n"
        "        raise RuntimeError('Injected account lookup error')\n"
        "    return await resolve_token(self, token, touch=touch)\n"
        "UserManager.resolve_token = resolve_with_failure\n"
    )
    return {"PYTHONPATH": os.pathsep.join((str(tmp_path), str(REPO)))}


def exercise_requests(config):
    bearer = setup_account(config)
    base_url = f"http://127.0.0.1:{config.port}"
    # Keep the log cases independent because an unhandled ASGI error closes
    # the connection after the 500 response.
    with httpx.Client(
        base_url=base_url, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        assert client.get("/api/auth/invites", params={"token": bearer}).status_code == 200
        assert client.get("/api/auth/invites", params={"token": INVALID_BEARER}).status_code == 401
        assert client.get("/api/auth/invites", params={"token": FAILING_BEARER}).status_code == 500
        missing = client.get(FILE_PATH, params={"path": "note.md", "token": bearer})
        assert missing.status_code == 404

    socket_url = f"ws://127.0.0.1:{config.port}/ws"
    with connect(f"{socket_url}?token={bearer}", proxy=None, open_timeout=2) as websocket:
        websocket.send("invalid JSON")
        assert json.loads(websocket.recv(timeout=2))["message"] == "Invalid JSON"
    with connect(f"{socket_url}?token={INVALID_BEARER}", proxy=None, open_timeout=2) as websocket:
        with pytest.raises(ConnectionClosedError) as rejected:
            websocket.recv(timeout=2)
        assert rejected.value.rcvd is not None
        assert rejected.value.rcvd.code == 4001
    with pytest.raises(InvalidStatus) as denied:
        with connect(f"{socket_url}?probe={DENIED_QUERY}", proxy=None, open_timeout=2):
            pytest.fail("WebSocket without a credential was accepted")
    assert denied.value.response.status_code == 403
    with pytest.raises(InvalidStatus) as failed:
        with connect(f"{socket_url}?token={FAILING_BEARER}", proxy=None, open_timeout=2):
            pytest.fail("WebSocket with an account lookup error was accepted")
    assert failed.value.response.status_code == 500
    return bearer


def assert_request_logs(logs, bearer):
    if any(value in logs for value in (bearer, INVALID_BEARER, DENIED_QUERY, FAILING_BEARER)):
        pytest.fail("A synthetic query credential reached the server logs", pytrace=False)
    for path, status in (
        ("/api/auth/invites", 200),
        ("/api/auth/invites", 401),
        ("/api/auth/invites", 500),
        (FILE_PATH, 404),
    ):
        assert re.search(rf'"GET {re.escape(path)} HTTP/[\d.]+" {status}\b', logs)
    assert logs.count('"WebSocket /ws" [accepted]') == 2
    assert '"WebSocket /ws" 403' in logs
    assert "WebSocket /ws handshake failed" in logs


@pytest.mark.parametrize(
    "log_options",
    [(), ("--log-level", "trace"), ("--log-level", "trace", "--ws", "websockets")],
    ids=["default", "trace", "legacy-trace"],
)
def test_direct_uvicorn_entry_redacts_request_logs(tmp_path, log_options, account_lookup_fault):
    with isolated_launch_environment() as env:
        config, config_file = installation_settings(tmp_path)
    env.update(account_lookup_fault)
    env.update({f"OCTOPUS_{name.upper()}": json.dumps(value) if isinstance(value, bool) else str(value)
                for name, value in json.loads(config_file.read_text()).items()})
    env.update({"OCTOPUS_AUTH_TOKEN": "isolated-setup-proof", "OCTOPUS_ENV_FILE": os.devnull})
    config.auth_token = env["OCTOPUS_AUTH_TOKEN"]
    with socket.socket() as listener, (tmp_path / "uvicorn.log").open("w+") as log:
        listener.bind((config.host, 0))
        config.port = listener.getsockname()[1]
        command = [sys.executable, "-m", "uvicorn", "server.main:app",
                   "--app-dir", str(REPO), "--fd", str(listener.fileno()), *log_options]
        process = subprocess.Popen(command, cwd=tmp_path, env=env, pass_fds=(listener.fileno(),),
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_for(lambda: ready(config), timeout=10)
            assert Path(config.master_key_file).is_file()
            bearer = exercise_requests(config)
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.seek(0)
        logs = log.read()
        assert_request_logs(logs, bearer)
        if log_options:
            assert "< GET /ws HTTP/1.1" in logs
            for path in ("/api/auth/invites", "/ws"):
                assert re.search(rf"Started scope=.*'path': '{re.escape(path)}'.*'query_string': '<\.\.\.>'", logs)


@pytest.mark.real
@pytest.mark.supervisor
def test_managed_entry_redacts_request_logs(tmp_path, account_lookup_fault):
    inherited = {**account_lookup_fault, "octopus_master_key": HOST_KEY_SENTINEL}
    manager_names = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", systemctl("show-environment").stdout, re.M))
    # The generated environment file owns the uppercase OCTOPUS settings.
    unset = sorted(
        name for name in manager_names | inherited.keys()
        if is_service_launch_option(name) and not (name.startswith("OCTOPUS_") and name.isupper())
    )
    with (
        isolated_launch_environment(),
        isolated_unit(
            tmp_path, configuration={"auth_token": "isolated-setup-proof"},
            inherited_environment=inherited, unset_environment=unset,
        ) as (unit, config, _),
    ):
        wait_for(lambda: ready(config))
        assert Path(config.master_key_file).is_file()
        if HOST_KEY_SENTINEL in environment_path(config).read_text():
            pytest.fail("The temporary environment file contains the ambient key", pytrace=False)
        bearer = exercise_requests(config)
    logs = subprocess.run(["journalctl", "--user", "-u", unit, "--no-pager"],
                          check=True, capture_output=True, text=True, timeout=10).stdout
    assert_request_logs(logs, bearer)
