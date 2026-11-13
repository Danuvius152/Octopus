"""Acceptance failures must stay failures even when the test command exits zero."""

import json
import math
import socket
import struct
import threading
import tomllib
from types import SimpleNamespace

import pytest

from scripts import codex_live


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
    original += 'model = "selected-model"\n[features]\nexample = true\n'
    source.write_text(original)
    expected = tomllib.loads(original)
    expected.update(cli_auth_credentials_store="file", model_reasoning_effort="low")
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
            if write_phase.is_set():
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
            record(self, host, port, result, **kwargs)
            if result != "forwarded":
                settled.set()

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
            record(self, host, port, outcome, **kwargs)
            if outcome == "proxy_error":
                settled.set()

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


@pytest.mark.parametrize("failure", ["none", "client_closed", "only_client_closed", "source_changed", "all_skipped", "auth_attempt", "no_network", "command_failed"])
def test_runner_applies_hard_success_conditions_and_preserves_evidence(tmp_path, monkeypatch, failure):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text("stand-in-login")
    (source / "config.toml").write_text('cli_auth_credentials_store="auto"\n')
    proxies = []
    original_proxy = codex_live.AuditProxy

    def keep_proxy(*args):
        proxy = original_proxy(*args)
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(codex_live, "AuditProxy", keep_proxy)

    def stand_in(argv, *, env, log, cwd):
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
            proxies[0].record("auth.openai.com" if failure == "auth_attempt" else "models.example", 443,
                              "authentication_blocked" if failure == "auth_attempt" else "forwarded")
        if failure in ("client_closed", "only_client_closed"):
            proxies[0].record("models.example", 443, "client_closed")
        log.write_text("stand-in results\n")
        return 1 if failure == "command_failed" else 0

    monkeypatch.setattr(codex_live, "execute", stand_in)
    args = SimpleNamespace(codex_home=str(source), output=str(tmp_path / "evidence"), model=None,
                           service_tier=None, upstream_proxy=None, suite="pytest", pytest_target=None)
    assert codex_live.run(args) == (0 if failure in ("none", "client_closed") else 1)
    result = json.loads((tmp_path / "evidence/result.json").read_text())
    assert result["passed"] is (failure in ("none", "client_closed"))
    assert result["selected_login_unchanged"] is (failure != "source_changed")
    assert (tmp_path / "evidence/pytest.log").is_file()
    if failure == "none":
        assert (source / "auth.json").read_text() == "stand-in-login"


def test_existing_evidence_directory_is_not_reused(tmp_path):
    (tmp_path / "auth.json").write_text("stand-in")
    with pytest.raises(FileExistsError):
        codex_live.run(SimpleNamespace(codex_home=str(tmp_path), output=str(tmp_path)))


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
                    else:
                        held.set()
                        release.wait(5)

        def completed_suite(argv, *, env, log, cwd):
            address = codex_live.urlsplit(env["HTTPS_PROXY"])
            for index in range(2):
                client = socket.create_connection((address.hostname, address.port), timeout=2)
                clients.append(client)
                client.sendall(b"CONNECT models.example:443 HTTP/1.1\r\n\r\n")
                if index == 0:
                    headers = _receive_headers(client)
                    assert b"200" in headers
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
        finally:
            release.set()
            for client in clients:
                client.close()
            thread.join(timeout=3)
            assert not thread.is_alive()
