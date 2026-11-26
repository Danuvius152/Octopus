"""Acceptance failures must stay failures even when the test command exits zero."""

import errno
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import tomllib
from types import SimpleNamespace

import pytest

from scripts import codex_live


@pytest.fixture
def clean_pytest_selection(monkeypatch):
    for selector in codex_live.PYTEST_SELECTION_ENVIRONMENT:
        monkeypatch.delenv(selector, raising=False)


def test_execute_reports_progress_during_a_quiet_child_and_retains_its_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(codex_live, "PROGRESS_INTERVAL_SECONDS", 0.04)
    log = tmp_path / "pytest.log"
    code = codex_live.execute(
        [sys.executable, "-c", "import time; time.sleep(0.2); print('child output'); raise SystemExit(7)"],
        env=os.environ.copy(), log=log, cwd=tmp_path,
    )
    assert code == 7
    output = capsys.readouterr().out
    assert "Still running pytest" in output and "child output" not in output
    assert log.read_text() == "child output\n"


def test_execute_reaps_the_child_when_the_start_message_cannot_be_written(tmp_path, monkeypatch):
    popen = codex_live.subprocess.Popen
    children = []

    def capture_child(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def broken_output(*args, **kwargs):
        raise BrokenPipeError("output closed")

    monkeypatch.setattr(codex_live.subprocess, "Popen", capture_child)
    monkeypatch.setattr(codex_live, "print", broken_output, raising=False)
    try:
        with pytest.raises(BrokenPipeError):
            codex_live.execute([sys.executable, "-c", "import time; time.sleep(30)"],
                               env=os.environ.copy(), log=tmp_path / "pytest.log", cwd=tmp_path)
        assert len(children) == 1 and children[0].returncode is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)


def _receive_headers(connection):
    connection.settimeout(2)
    headers = b""
    while not headers.endswith(b"\r\n\r\n"):
        block = connection.recv(1)
        assert block, "CONNECT closed before its headers completed"
        headers += block
    return headers


def test_file_store_configuration_preserves_tables_and_host_configuration(tmp_path):
    source = tmp_path / "config.toml"
    original = 'model="selected-model"\ncli_auth_credentials_store="auto"\n[features]\nexample=true\n'
    source.write_text(original)
    config = tomllib.loads(codex_live.isolated_config(source))
    assert config["cli_auth_credentials_store"] == "file"
    assert config["model"] == "selected-model" and config["features"]["example"]
    assert config["model_reasoning_effort"] == "low"
    assert source.read_text() == original


@pytest.mark.parametrize("original", [
    '"model_reasoning_effort" = "high"\n"cli_auth_credentials_store" = "auto"\n',
    "'model_reasoning_effort' = 'high'\n'cli_auth_credentials_store' = 'auto'\n",
    'developer_instructions = """Keep these instructions:\nmodel_reasoning_effort = "high"\n[not_a_table]\n"""\n',
    "developer_instructions = '''Keep these instructions:\nmodel_reasoning_effort = 'high'\n[not_a_table]\n'''\n",
])
@pytest.mark.parametrize("model,service_tier", [(None, None), ("test-model", "fast")])
def test_configuration_overrides_preserve_all_other_toml_values(tmp_path, original, model, service_tier):
    source = tmp_path / "config.toml"
    original += 'model = "selected-model"\n[features]\nexample = true\nplugins = true\n'
    source.write_text(original)
    expected = tomllib.loads(original)
    expected.update(cli_auth_credentials_store="file", model_reasoning_effort="low")
    expected["features"]["plugins"] = False
    if model:
        expected["model"] = model
    if service_tier:
        expected["service_tier"] = service_tier
    assert tomllib.loads(codex_live.isolated_config(source, model=model, service_tier=service_tier)) == expected
    assert source.read_text() == original


def test_isolated_configuration_preserves_toml_tables_arrays_and_scalar_types(tmp_path):
    source = tmp_path / "config.toml"
    original = r'''
"model\u005freasoning_effort" = "high"
"key.with.dots" = "Unicode 中文, control \u007f, quote \", slash \\, newline \n"
count = 4
ratio = 0.25
unlimited = inf
negative = -inf
unknown = nan
timestamp = 2026-11-13T20:00:00.123456+08:00
local_timestamp = 2026-11-13T20:00:00
day = 2026-11-13
clock = 20:00:00.125
matrix = [[1, 2], [3, 4]]
mixed = [1, "text", false, { "quoted.key" = "value" }]
[empty]
[profiles."name.with.dots"]
model_reasoning_effort = "high"
enabled = true
[[entries]]
name = "first"
[[entries]]
name = "second"
'''
    source.write_text(original)
    expected = tomllib.loads(original)
    expected.update(cli_auth_credentials_store="file", model_reasoning_effort="low")
    expected["features"] = {"plugins": False}
    result = tomllib.loads(codex_live.isolated_config(source))
    assert math.isnan(result.pop("unknown")) and math.isnan(expected.pop("unknown"))
    assert result == expected
    assert source.read_text() == original


@pytest.mark.parametrize("host", ["auth.openai.com", "AUTH.OPENAI.COM.", "nested.auth.openai.com"])
def test_authentication_connect_never_reaches_upstream(tmp_path, host):
    # An upstream listener would accept immediately if the guard forwarded it.
    with socket.socket() as upstream:
        upstream.bind(("127.0.0.1", 0))
        upstream.listen()
        upstream.settimeout(.1)
        with codex_live.proxy_server(f"http://127.0.0.1:{upstream.getsockname()[1]}", tmp_path / "audit") as proxy:
            with socket.create_connection(proxy.server_address, timeout=2) as client:
                client.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
                assert b"403" in client.recv(1024)
            with pytest.raises(TimeoutError):
                upstream.accept()
            assert proxy.records == [{"method": "CONNECT", "host": host.lower().rstrip("."),
                                      "port": 443, "outcome": "authentication_blocked"}]


def test_tunnel_forwards_opaque_bytes_and_logs_only_authority(tmp_path):
    with socket.socket() as upstream:
        upstream.bind(("127.0.0.1", 0))
        upstream.listen()
        upstream.settimeout(2)

        def echo():
            connection, _ = upstream.accept()
            with connection:
                headers = _receive_headers(connection)
                assert headers.startswith(b"CONNECT models.example:443 ")
                connection.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                connection.sendall(connection.recv(1024))

        thread = threading.Thread(target=echo)
        thread.start()
        audit = tmp_path / "audit"
        with codex_live.proxy_server(f"http://127.0.0.1:{upstream.getsockname()[1]}", audit) as proxy:
            with socket.create_connection(proxy.server_address, timeout=2) as client:
                client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                headers = _receive_headers(client)
                assert b"200" in headers
                client.sendall(b"opaque-secret-payload")
                assert client.recv(1024) == b"opaque-secret-payload"
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert "opaque-secret" not in audit.read_text()
        assert json.loads(audit.read_text())["host"] == "models.example"


def test_network_failure_stops_further_upstream_dials(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def refuse_first_tunnel():
            connection, _ = listener.accept()
            with connection:
                _receive_headers(connection)
                connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

        thread = threading.Thread(target=refuse_first_tunnel)
        thread.start()
        try:
            address = f"http://127.0.0.1:{listener.getsockname()[1]}"
            with codex_live.proxy_server(address, tmp_path / "audit") as proxy:
                request = b"CONNECT models.example:443 HTTP/1.1\r\n\r\n"
                with socket.create_connection(proxy.server_address, timeout=2) as client:
                    client.sendall(request)
                    assert client.recv(1024) == b""
                assert proxy.summary()["errors"] == 1
                with socket.create_connection(proxy.server_address, timeout=2) as client:
                    try:
                        client.sendall(request)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # The proxy may already have closed the socket.
                    listener.settimeout(.1)
                    with pytest.raises(TimeoutError):
                        listener.accept()
                assert proxy.summary()["errors"] == 1
        finally:
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_first_audit_error_closes_active_tunnel_and_blocks_an_admitted_handler(tmp_path, monkeypatch):
    hold, reached, resume, retired = (threading.Event() for _ in range(4))
    getaddrinfo = socket.getaddrinfo
    handler = codex_live.AuditProxy.process_request_thread

    def hold_before_dial(host, port, *args, **kwargs):
        if port == upstream_port and hold.is_set():
            reached.set()
            assert resume.wait(3)
        return getaddrinfo(host, port, *args, **kwargs)

    def observe_retirement(self, request, client_address):
        handler(self, request, client_address)
        if client_address == pending_address:
            retired.set()

    monkeypatch.setattr(socket, "getaddrinfo", hold_before_dial)
    monkeypatch.setattr(codex_live.AuditProxy, "process_request_thread", observe_retirement)
    pending_address = None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        upstream_port = listener.getsockname()[1]
        address = f"http://127.0.0.1:{upstream_port}"
        with codex_live.proxy_server(address, tmp_path / "audit") as proxy:
            with socket.create_connection(proxy.server_address, timeout=2) as active:
                active.sendall(b"CONNECT active.example:443 HTTP/1.1\r\n\r\n")
                upstream, _ = listener.accept()
                with upstream:
                    _receive_headers(upstream)
                    upstream.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                    assert b"200" in _receive_headers(active)
                    active.sendall(b"x")
                    assert upstream.recv(1) == b"x"
                    with socket.create_connection(proxy.server_address, timeout=2) as pending:
                        pending_address = pending.getsockname()
                        hold.set()
                        pending.sendall(b"CONNECT pending.example:443 HTTP/1.1\r\n\r\n")
                        try:
                            assert reached.wait(2)
                            with socket.create_connection(proxy.server_address, timeout=2) as blocked:
                                blocked.sendall(b"CONNECT auth.openai.com:443 HTTP/1.1\r\n\r\n")
                                assert b"403" in _receive_headers(blocked)
                            assert active.recv(1) == b""
                            assert upstream.recv(1) == b""
                        finally:
                            resume.set()
                        assert retired.wait(2)
                        listener.settimeout(.1)
                        with pytest.raises(TimeoutError):
                            listener.accept()
                    assert [row["outcome"] for row in proxy.records] == ["forwarded", "authentication_blocked"]


def test_stopped_proxy_blocks_counted_payload_even_before_socket_shutdown(tmp_path, monkeypatch):
    waiting_to_send, release = threading.Event(), threading.Event()
    send = codex_live.AuditProxy.send

    def hold_payload(self, connection, data, *, timeout):
        if data == b"held payload":
            waiting_to_send.set()
            assert release.wait(3)
        return send(self, connection, data, timeout=timeout)

    monkeypatch.setattr(codex_live.AuditProxy, "send", hold_payload)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        with codex_live.proxy_server(address, tmp_path / "audit") as proxy:
            with socket.create_connection(proxy.server_address, timeout=2) as client:
                client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                upstream, _ = listener.accept()
                with upstream:
                    _receive_headers(upstream)
                    upstream.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                    assert b"200" in _receive_headers(client)
                    client.sendall(b"held payload")
                    try:
                        assert waiting_to_send.wait(2)
                        with monkeypatch.context() as deferred:
                            deferred.setattr(proxy, "_shutdown", lambda connections: None)
                            proxy.record("other.example", 443, "proxy_error")
                        assert proxy.byte_counts() == (len(b"held payload"), 0)
                    finally:
                        release.set()
                    assert upstream.recv(64) == b""
    assert proxy.byte_counts() == (len(b"held payload"), 0)


def test_recognized_authentication_is_recorded_before_freeze_after_first_error(tmp_path, monkeypatch):
    recognized, release, freezing, frozen = (threading.Event() for _ in range(4))
    record_locked = codex_live.AuditProxy._record_locked
    block_authentication = codex_live.AuditProxy.block_authentication

    def simulate_prior_first_error(self, host, port, method, client):
        self.record("other.example", 443, "proxy_error", client=client)
        return block_authentication(self, host, port, method, client)

    def hold_record(self, host, port, outcome, method, client):
        if outcome == "authentication_blocked":
            recognized.set()
            assert release.wait(3)
        return record_locked(self, host, port, outcome, method, client)

    monkeypatch.setattr(codex_live.AuditProxy, "block_authentication", simulate_prior_first_error)
    monkeypatch.setattr(codex_live.AuditProxy, "_record_locked", hold_record)
    with codex_live.proxy_server(None, tmp_path / "audit") as proxy:
        def freeze_audit():
            freezing.set()
            proxy.finish_audit()
            frozen.set()

        thread = threading.Thread(target=freeze_audit)
        with socket.create_connection(proxy.server_address, timeout=2) as client:
            client.sendall(b"CONNECT auth.openai.com:443 HTTP/1.1\r\n\r\n")
            assert recognized.wait(2)
            thread.start()
            try:
                assert freezing.wait(2)
                assert not frozen.wait(0.05)
            finally:
                release.set()
                thread.join(timeout=2)
            assert frozen.is_set()
            assert proxy.summary()["authentication_attempts"] == 1
            assert proxy.summary()["stop_reason"] == "proxy_error"
    assert [row["outcome"] for row in proxy.records] == ["proxy_error", "authentication_blocked"]


def test_upstream_falls_back_when_first_address_cannot_make_a_socket(tmp_path, monkeypatch):
    with socket.socket() as listener, codex_live.proxy_server(None, tmp_path / "audit") as proxy:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        address = listener.getsockname()
        with socket.create_connection(proxy.server_address, timeout=2) as client:
            create_socket = socket.socket

            def getaddrinfo(*args, **kwargs):
                return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", address[1], 0, 0)),
                        (socket.AF_INET, socket.SOCK_STREAM, 0, "", address)]

            def create_supported_socket(family=socket.AF_INET, *args, **kwargs):
                if family == socket.AF_INET6:
                    raise OSError(errno.EAFNOSUPPORT, "IPv6 unsupported")
                return create_socket(family, *args, **kwargs)

            monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
            monkeypatch.setattr(socket, "socket", create_supported_socket)
            client.sendall(f"CONNECT 127.0.0.1:{address[1]} HTTP/1.1\r\n\r\n".encode())
            with listener.accept()[0]:
                assert b"200" in _receive_headers(client)
    assert proxy.summary()["errors"] == 0


def test_slow_upstream_handshake_gets_a_new_read_timeout_after_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_live, "UPSTREAM_TIMEOUT", 1.0)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        upstream_address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        with codex_live.proxy_server(upstream_address, tmp_path / "audit") as proxy:
            with socket.create_connection(proxy.server_address, timeout=2) as client:
                client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                with listener.accept()[0] as upstream:
                    _receive_headers(upstream)
                    for part in (b"HTTP/1.1 ", b"200 ", b"OK\r\n", b"\r\n"):
                        time.sleep(0.35)
                        upstream.sendall(part)
                    assert b"200" in _receive_headers(client)
    assert proxy.summary()["errors"] == 0


def test_client_backpressure_waits_without_upstream_timeout(tmp_path, monkeypatch):
    sender, receiver = socket.socketpair()
    with sender, receiver, codex_live.proxy_server(None, tmp_path / "audit") as proxy:
        receiver.settimeout(2)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        buffered = 0
        with pytest.raises(BlockingIOError):
            while True:
                buffered += sender.send(b"a" * 4096, socket.MSG_DONTWAIT)

        wait = codex_live.wait_for_socket
        waits = []

        def release_after_wait(connection, deadline, *, write=False):
            waits.append((deadline, write))
            assert receiver.recv(buffered, socket.MSG_WAITALL) == b"a" * buffered
            wait(connection, deadline, write=write)

        monkeypatch.setattr(codex_live, "wait_for_socket", release_after_wait)
        assert proxy.send(sender, b"resumed", timeout=None)
        assert receiver.recv(7, socket.MSG_WAITALL) == b"resumed"
        assert waits == [(None, True)]


@pytest.mark.parametrize("excess_direction", ["client", "upstream"])
def test_tunnel_budget_counts_both_directions_and_refuses_excess(tmp_path, excess_direction):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        audit = tmp_path / "audit"
        with codex_live.proxy_server(address, audit, max_bytes=5) as proxy:
            with socket.create_connection(proxy.server_address, timeout=2) as client:
                client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                upstream, _ = listener.accept()
                with upstream:
                    _receive_headers(upstream)
                    upstream.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                    assert b"200" in _receive_headers(client)
                    client.sendall(b"abc")
                    assert upstream.recv(3, socket.MSG_WAITALL) == b"abc"
                    upstream.sendall(b"de")
                    assert client.recv(2, socket.MSG_WAITALL) == b"de"
                    sender = client if excess_direction == "client" else upstream
                    sender.sendall(b"f")
                    assert client.recv(1) == upstream.recv(1) == b""
        summary = proxy.summary()
        assert summary["bytes_from_client"] == 3 + (excess_direction == "client")
        assert summary["bytes_from_upstream"] == 2 + (excess_direction == "upstream")
        assert summary["max_bytes"] == 5
        assert summary["stop_reason"] == "budget_exceeded" and summary["errors"] == 1
        assert [json.loads(line)["outcome"] for line in audit.read_text().splitlines()] == [
            "forwarded", "budget_exceeded",
        ]


@pytest.mark.parametrize("endpoint,outcome", [("client", "client_closed"), ("upstream", "proxy_error"),
                                             ("client_write", "client_closed"), ("upstream_write", "proxy_error")])
def test_established_tunnel_reset_is_attributed_to_its_actual_endpoint(tmp_path, monkeypatch, endpoint, outcome):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        accepted, release, settled = {}, threading.Event(), threading.Event()
        write_phase = threading.Event()
        select = codex_live.select.select

        def ready_for_write(read, write, exceptional, timeout):
            ready = select(read, write, exceptional, timeout)
            if write_phase.is_set() and len(read) == 2:
                # Exercise the legal readiness order which receives one side's
                # data before noticing that the destination peer has closed.
                peer = read[1] if endpoint == "client_write" else read[0]
                return select([peer], write, exceptional, timeout)
            return ready

        if endpoint.endswith("_write"):
            monkeypatch.setattr(codex_live.select, "select", ready_for_write)

        def upstream():
            connection, _ = listener.accept()
            accepted["socket"] = connection
            with connection:
                _receive_headers(connection)
                connection.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                connection.sendall(connection.recv(1024))
                if release.wait(3) and endpoint == "client_write":
                    connection.sendall(b"reply")

        record = codex_live.AuditProxy.record

        def observe(self, host, port, result, **kwargs):
            accepting = record(self, host, port, result, **kwargs)
            if result != "forwarded":
                settled.set()
            return accepting

        monkeypatch.setattr(codex_live.AuditProxy, "record", observe)
        thread = threading.Thread(target=upstream)
        thread.start()
        try:
            with codex_live.proxy_server(f"http://127.0.0.1:{listener.getsockname()[1]}", tmp_path / "audit") as proxy:
                with socket.create_connection(proxy.server_address, timeout=2) as client:
                    client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                    header = _receive_headers(client)
                    assert b"200" in header
                    client.sendall(b"opaque")
                    assert client.recv(1024) == b"opaque"
                    if endpoint.endswith("_write"):
                        write_phase.set()
                    closing = client if endpoint.startswith("client") else accepted["socket"]
                    closing.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    closing.close()
                    if endpoint == "client_write":
                        release.set()
                    elif endpoint == "upstream_write":
                        client.sendall(b"request")
                    assert settled.wait(2), "The peer reset must reach the original tunnel handler"
                assert proxy.records == [
                    {"method": "CONNECT", "host": "models.example", "port": 443, "outcome": "forwarded"},
                    {"method": "CONNECT", "host": "models.example", "port": 443, "outcome": outcome},
                ]
        finally:
            release.set()
            thread.join(timeout=3)
            assert not thread.is_alive()


@pytest.mark.parametrize("failure", ["upstream_handshake", "client_ack"])
def test_failed_connect_never_counts_as_a_forwarded_or_client_closed_tunnel(tmp_path, monkeypatch, failure):
    arrived, release, settled = threading.Event(), threading.Event(), threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def upstream():
            connection, _ = listener.accept()
            with connection:
                _receive_headers(connection)
                arrived.set()
                if release.wait(3) and failure == "client_ack":
                    connection.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                else:
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

        record = codex_live.AuditProxy.record

        def observe(self, host, port, outcome, **kwargs):
            accepting = record(self, host, port, outcome, **kwargs)
            if outcome == "proxy_error":
                settled.set()
            return accepting

        monkeypatch.setattr(codex_live.AuditProxy, "record", observe)
        thread = threading.Thread(target=upstream)
        thread.start()
        try:
            with codex_live.proxy_server(f"http://127.0.0.1:{listener.getsockname()[1]}", tmp_path / "audit") as proxy:
                with socket.create_connection(proxy.server_address, timeout=2) as client:
                    client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                    assert arrived.wait(2)
                    if failure == "client_ack":
                        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        client.close()
                    release.set()
                    assert settled.wait(2)
                assert proxy.records == [{"method": "CONNECT", "host": "models.example", "port": 443, "outcome": "proxy_error"}]
        finally:
            release.set()
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_report_requires_actual_passes_without_skips_or_hidden_failures(tmp_path):
    report = tmp_path / "results.xml"
    for body, wanted in [("", False), ('<testcase><skipped/></testcase>', False),
                         ('<testcase><error/></testcase>', False), ('<testcase/>', True),
                         ('<testcase/><testcase><skipped/></testcase>', False)]:
        report.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
        assert codex_live.accepted(codex_live.pytest_summary(report)) is wanted
    browser = tmp_path / "browser.json"
    browser.write_text(json.dumps({"stats": {"expected": 1, "unexpected": 0, "skipped": 0, "flaky": 0},
                                   "errors": [{"message": "teardown failed"}]}))
    assert not codex_live.accepted(codex_live.browser_summary(browser))


@pytest.mark.parametrize("failure", [
    "none", "client_closed", "only_client_closed", "source_changed", "all_skipped", "auth_attempt",
    "late_audit_error", "no_network", "command_failed", "summary_output_closed",
])
@pytest.mark.usefixtures("clean_pytest_selection")
def test_runner_applies_hard_success_conditions_and_preserves_evidence(tmp_path, monkeypatch, failure):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text("stand-in-login")
    (source / "config.toml").write_text('cli_auth_credentials_store="auto"\n')
    execute = codex_live.execute

    def no_child(*args, **kwargs):
        pytest.fail("A latched audit failure must prevent launching the browser")

    monkeypatch.setattr(codex_live.subprocess, "Popen", no_child)

    def stand_in(argv, *, env, log, cwd, proxy):
        if "pytest" not in argv:
            if failure == "late_audit_error":
                proxy.record("models.example", 443, "proxy_error")
            return execute(argv, env=env, log=log, cwd=cwd, proxy=proxy)
        assert "pytest" in argv and "-x" in argv
        home = codex_live.Path(env["CODEX_HOME"])
        assert (home / "auth.json").resolve() == source / "auth.json"
        assert tomllib.loads((home / "config.toml").read_text())["cli_auth_credentials_store"] == "file"
        assert env["HTTPS_PROXY"] == env["https_proxy"]
        report = codex_live.Path(next(value.removeprefix("--junitxml=") for value in argv if value.startswith("--junitxml=")))
        body = '<testcase><skipped/></testcase>' if failure == "all_skipped" else '<testcase/>'
        report.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
        if failure == "source_changed":
            (source / "auth.json").write_text("changed-by-child")
        if failure not in ("no_network", "only_client_closed"):
            if failure == "auth_attempt":
                proxy.record("auth.openai.com", 443, "authentication_blocked")
            else:
                proxy.record("models.example", 443, "forwarded")
        if failure in ("client_closed", "only_client_closed"):
            proxy.record("models.example", 443, "client_closed")
        log.write_text("stand-in results\n")
        return 1 if failure == "command_failed" else 0

    monkeypatch.setattr(codex_live, "execute", stand_in)
    args = SimpleNamespace(codex_home=str(source), output=str(tmp_path / "evidence"), model=None,
                           service_tier=None, upstream_proxy=None,
                           suite="all" if failure in ("auth_attempt", "late_audit_error") else "pytest", pytest_target=None)
    passed = failure in ("none", "client_closed", "summary_output_closed")
    if failure == "summary_output_closed":
        def broken_summary(message, **kwargs):
            if message.startswith("Codex tunnel usage (run)"):
                raise BrokenPipeError("output closed")

        monkeypatch.setattr(codex_live, "print", broken_summary, raising=False)
        with pytest.raises(BrokenPipeError):
            codex_live.run(args)
    else:
        assert codex_live.run(args) == (0 if passed else 1)
    result = json.loads((tmp_path / "evidence/result.json").read_text())
    assert result["passed"] is passed
    assert result["selected_login_unchanged"] is (failure != "source_changed")
    assert (tmp_path / "evidence/pytest.log").is_file()
    assert "Codex tunnel usage (pytest)" in (tmp_path / "evidence/pytest.log").read_text()
    assert set(result["execution"]) == {"pytest"}
    if failure == "none":
        assert (source / "auth.json").read_text() == "stand-in-login"
    if failure == "auth_attempt":
        assert result["network"]["stop_reason"] == "authentication_blocked"
    if failure == "late_audit_error":
        assert result["network"]["stop_reason"] == "proxy_error"


def test_existing_evidence_directory_is_not_reused(tmp_path):
    (tmp_path / "auth.json").write_text("stand-in")
    with pytest.raises(FileExistsError):
        codex_live.run(SimpleNamespace(codex_home=str(tmp_path), output=str(tmp_path)))


@pytest.mark.parametrize("selection", ["overlap", "deselected", "unknown-node", "bad-import"])
@pytest.mark.usefixtures("clean_pytest_selection")
def test_real_pytest_certifies_all_targets_without_masking_native_errors(
    tmp_path, monkeypatch, selection,
):
    source = tmp_path / "login"
    source.mkdir()
    (source / "auth.json").write_text("stand-in")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".venv").symlink_to(codex_live.ROOT / ".venv", target_is_directory=True)
    (checkout / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["real", "real_codex", "codex_bin", "real_claude", "knowledge_eval", "wiki_perf"]\n'
    )
    (checkout / "conftest.py").write_text(
        'def pytest_addoption(parser):\n    parser.addoption("--run-real", action="store_true")\n'
    )
    (checkout / "tests").mkdir()
    (checkout / "tests/test_capability.py").write_text(
        'import pytest\n'
        '@pytest.mark.real\n@pytest.mark.real_codex\n'
        'def test_eligible(): print("eligible capability executed")\n'
        'def test_offline(): pytest.fail("must be deselected")\n'
    )
    project = codex_live.ROOT
    monkeypatch.setattr(codex_live, "ROOT", checkout)
    execute = codex_live.execute

    def execute_local_pytest(argv, *, env, log, cwd, proxy):
        proxy.record("models.example", 443, "forwarded")
        return execute(argv, env={**env, "PYTHONPATH": str(project)}, log=log, cwd=cwd, proxy=proxy)

    monkeypatch.setattr(codex_live, "execute", execute_local_pytest)
    target = "tests/test_capability.py"
    eligible = f"{target}::test_eligible"
    deselected = f"{target}::test_offline"
    targets = [target, eligible]
    other = "tests/test_other.py"
    if selection == "deselected":
        targets.append(deselected)
    elif selection == "unknown-node":
        (checkout / other).write_text("def test_present(): pass\n")
        targets.append(f"{other}::test_misspelled")
    elif selection == "bad-import":
        (checkout / other).write_text("import octopus_missing_live_fixture\n")
        targets.append(other)
    output = tmp_path / "evidence"
    args = SimpleNamespace(codex_home=str(source), output=str(output), model=None, service_tier=None,
                           upstream_proxy=None, suite="pytest", pytest_target=targets)
    assert codex_live.run(args) == int(selection != "overlap")
    result = json.loads((output / "result.json").read_text())
    assert result["selection"] == {"pytest": {"scope": "targets", "targets": targets}}
    report = output / "pytest-selection.json"
    log = (output / "pytest.log").read_text()
    if selection in {"overlap", "deselected"}:
        selected = json.loads(report.read_text())
        assert selected == {"selected": [eligible], "unmatched_targets": [deselected] if selection == "deselected" else []}
    else:
        assert not report.exists()
        expected = "not found:" if selection == "unknown-node" else "No module named 'octopus_missing_live_fixture'"
        assert expected in log
    if selection == "deselected":
        assert deselected in result["errors"][0]
        assert "test_eligible PASSED" not in log
    elif selection == "overlap":
        assert result["suites"]["pytest"]["passed"] == 1


@pytest.mark.parametrize("selector", codex_live.PYTEST_SELECTION_ENVIRONMENT)
@pytest.mark.usefixtures("clean_pytest_selection")
def test_direct_live_pytest_refuses_selection_overrides(tmp_path, monkeypatch, selector):
    (tmp_path / "auth.json").write_text("stand-in-login")
    monkeypatch.setenv(selector, "unapproved")

    def no_child(*args, **kwargs):
        pytest.fail("Selection overrides must be rejected before pytest starts")

    monkeypatch.setattr(codex_live, "execute", no_child)
    output = tmp_path / "evidence"
    args = SimpleNamespace(codex_home=str(tmp_path), output=str(output), model=None,
                           service_tier=None, upstream_proxy=None, suite="pytest", pytest_target=None)
    assert codex_live.run(args) == 1
    result = json.loads((output / "result.json").read_text())
    assert not result["passed"] and result["execution"] == {}
    assert selector in result["errors"][0]


@pytest.mark.usefixtures("clean_pytest_selection")
def test_runner_cannot_pass_while_a_started_proxy_handshake_is_unsettled(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text("stand-in-login")
    clients = []
    held, release = threading.Event(), threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def upstream():
            for index in range(2):
                connection, _ = listener.accept()
                with connection:
                    _receive_headers(connection)
                    if index == 0:
                        connection.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                        connection.sendall(connection.recv(1024))
                    else:
                        held.set()
                        release.wait(5)

        def completed_suite(argv, *, env, log, cwd, proxy):
            address = codex_live.urlsplit(env["HTTPS_PROXY"])
            for index in range(2):
                client = socket.create_connection((address.hostname, address.port), timeout=2)
                clients.append(client)
                client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                if index == 0:
                    headers = _receive_headers(client)
                    assert b"200" in headers
                    client.sendall(b"metered")
                    assert client.recv(7, socket.MSG_WAITALL) == b"metered"
                    client.close()
            assert held.wait(2), "The second CONNECT is blocked in the real upstream handshake"
            report = codex_live.Path(next(value.removeprefix("--junitxml=") for value in argv if value.startswith("--junitxml=")))
            report.write_text('<testsuites><testsuite><testcase/></testsuite></testsuites>')
            log.write_text("The selected suite completed successfully.\n")
            return 0

        monkeypatch.setattr(codex_live, "execute", completed_suite)
        thread = threading.Thread(target=upstream)
        thread.start()
        try:
            args = SimpleNamespace(codex_home=str(source), output=str(tmp_path / "evidence"), model=None,
                                   service_tier=None, suite="pytest", pytest_target=None,
                                   upstream_proxy=f"http://127.0.0.1:{listener.getsockname()[1]}")
            assert codex_live.run(args) == 1
            result = json.loads((tmp_path / "evidence/result.json").read_text())
            assert not result["passed"] and result["network"]["unsettled"] > 0
            assert result["execution"]["pytest"]["network_bytes"] == {
                "bytes_from_client": 7, "bytes_from_upstream": 7,
            }
            assert "7 bytes up, 7 bytes down" in (tmp_path / "evidence/pytest.log").read_text()
        finally:
            release.set()
            for client in clients:
                client.close()
            thread.join(timeout=3)
            assert not thread.is_alive()
