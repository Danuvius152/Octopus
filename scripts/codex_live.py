"""Run Codex acceptance with one selected login, isolation and network evidence."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))

from scripts.check_runner import PYTEST_SELECTION_ENVIRONMENT  # noqa: E402

SELECTION = "real and (real_codex or codex_bin) and not real_claude and not knowledge_eval and not wiki_perf"
PROGRESS_INTERVAL_SECONDS = 15
UPSTREAM_TIMEOUT = 15


def identity(path: Path) -> tuple:
    target = path.resolve(strict=True)
    info = target.stat()
    return str(target), info.st_dev, info.st_ino, hashlib.sha256(target.read_bytes()).hexdigest()


def _toml_value(value: object) -> str:
    """Encode tomllib's value types inline, without interpreting source lines."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_value(key)} = {_toml_value(item)}" for key, item in value.items()) + " }"
    raise TypeError(f"Unsupported TOML value: {type(value).__name__}")


def isolated_config(source: Path, *, model: str | None = None, service_tier: str | None = None) -> str:
    config = tomllib.loads(source.read_text() if source.exists() else "")
    overrides = {"cli_auth_credentials_store": "file", "model_reasoning_effort": "low"}
    if model:
        overrides["model"] = model
    if service_tier:
        overrides["service_tier"] = service_tier
    # Rewrite only the isolated copy. Quoted keys, multiline instructions and
    # tables are values of the parsed document, never candidates for line edits.
    config.update(overrides)
    return "".join(f"{_toml_value(key)} = {_toml_value(value)}\n" for key, value in config.items())


def wait_for_socket(connection: socket.socket, deadline: float | None, *, write: bool = False) -> None:
    timeout = None if deadline is None else max(0, deadline - monotonic())
    read_ready, write_ready, _ = select.select(
        [] if write else [connection], [connection] if write else [], [], timeout,
    )
    if not read_ready and not write_ready:
        raise TimeoutError("Timed out waiting for the tunnel socket")


class AuditProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: str | None, audit: Path, max_bytes: int | None = None):
        super().__init__(("127.0.0.1", 0), ProxyRequest)
        self.upstream = urlsplit(upstream) if upstream else None
        if self.upstream and (self.upstream.scheme != "http" or not self.upstream.hostname
                              or self.upstream.username or self.upstream.password):
            self.server_close()
            raise ValueError("Upstream must be an HTTP proxy without embedded credentials")
        self.audit = audit
        self.records: list[dict] = []
        self.lock = threading.Lock()
        # Register before starting a handler, so even an upstream handshake
        # which has not produced an audit row prevents a successful snapshot.
        self.requests: dict[socket.socket, socket.socket | None] = {}
        self.closing = False
        self.unsettled = 0
        self.stop_reason: str | None = None
        self.max_bytes = max_bytes
        self.bytes_from_client = 0
        self.bytes_from_upstream = 0

    def process_request(self, request, client_address):
        with self.lock:
            accepting = self._accepting_locked()
            if accepting:
                self.requests[request] = None
        if not accepting:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.lock:
                self.requests.pop(request, None)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.lock:
                self.requests.pop(request, None)

    def _accepting_locked(self) -> bool:
        return not self.closing and self.stop_reason is None

    def _connections_locked(self, excluding: socket.socket | None = None) -> list[socket.socket]:
        return [connection for client, upstream in self.requests.items() if client is not excluding
                for connection in (client, upstream) if connection is not None]

    @staticmethod
    def _shutdown(connections: list[socket.socket]) -> None:
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def finish_audit(self) -> None:
        # Called after the accept loop stops. Unsettled work remains a failure
        # even if closing its sockets lets the daemon handler retire afterward.
        with self.lock:
            self.closing = True
            self.unsettled = len(self.requests)
            connections = self._connections_locked()
        self._shutdown(connections)
        for connection in connections:
            connection.close()

    def summary(self) -> dict:
        with self.lock:
            return {"tunnels": sum(row["outcome"] == "forwarded" for row in self.records),
                    "client_closed": sum(row["outcome"] == "client_closed" for row in self.records),
                    "authentication_attempts": sum(row["outcome"] == "authentication_blocked" for row in self.records),
                    "errors": sum(row["outcome"] not in ("forwarded", "client_closed") for row in self.records),
                    "unsettled": self.unsettled,
                    "stop_reason": self.stop_reason,
                    "bytes_from_client": self.bytes_from_client,
                    "bytes_from_upstream": self.bytes_from_upstream,
                    "max_bytes": self.max_bytes}

    def byte_counts(self) -> tuple[int, int]:
        with self.lock:
            return self.bytes_from_client, self.bytes_from_upstream

    def _record_locked(self, host: str, port: int, outcome: str, method: str,
                       client: socket.socket | None) -> list[socket.socket]:
        row = {"method": method, "host": host, "port": port, "outcome": outcome}
        try:
            with self.audit.open("a") as stream:
                stream.write(json.dumps(row) + "\n")
        except OSError:
            if outcome in ("forwarded", "client_closed"):
                row["outcome"] = outcome = "audit_write_error"
        self.records.append(row)
        if outcome in ("forwarded", "client_closed") or self.stop_reason is not None:
            return []
        self.stop_reason = outcome
        # Leave the reporting handler in charge of its own sockets and any
        # pending HTTP response, including the original authentication 403.
        return self._connections_locked(excluding=client)

    def record(self, host: str, port: int, outcome: str, *, method: str = "CONNECT",
               client: socket.socket | None = None) -> bool:
        with self.lock:
            if not self._accepting_locked():
                return False
            connections = self._record_locked(host, port, outcome, method, client)
            accepting = self._accepting_locked()
        self._shutdown(connections)
        return accepting

    def block_authentication(self, host: str, port: int, method: str, client: socket.socket) -> bool:
        with self.lock:
            if host != "auth.openai.com" and not host.endswith(".auth.openai.com"):
                return False
            connections = [] if self.closing else self._record_locked(host, port, "authentication_blocked", method, client)
        self._shutdown(connections)
        return True

    def connect_upstream(self, client: socket.socket, address: tuple[str, int]) -> socket.socket | None:
        last_error = OSError("No upstream addresses")
        for family, kind, protocol, _, endpoint in socket.getaddrinfo(*address, type=socket.SOCK_STREAM):
            upstream = None
            connected = False
            try:
                upstream = socket.socket(family, kind, protocol)
                upstream.setblocking(False)
                with self.lock:
                    if not self._accepting_locked():
                        return None
                    self.requests[client] = upstream
                    status = upstream.connect_ex(endpoint)
                if status in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
                    wait_for_socket(upstream, monotonic() + UPSTREAM_TIMEOUT, write=True)
                    status = upstream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if status:
                    raise OSError(status, os.strerror(status))
                with self.lock:
                    connected = self._accepting_locked()
                return upstream if connected else None
            except OSError as error:
                last_error = error
            finally:
                if upstream is not None and not connected:
                    upstream.close()
        raise last_error

    def send(self, connection: socket.socket, data: bytes, *, timeout: float | None) -> bool:
        remaining = memoryview(data)
        deadline = None if timeout is None else monotonic() + timeout
        while remaining:
            with self.lock:
                if not self._accepting_locked():
                    return False
                try:
                    sent = connection.send(remaining, socket.MSG_DONTWAIT)
                except BlockingIOError:
                    sent = None
            if sent is None:
                wait_for_socket(connection, deadline, write=True)
                continue
            if sent == 0:
                raise ConnectionResetError("Tunnel socket closed during send")
            remaining = remaining[sent:]
        return True

    def receive_payload(self, host: str, port: int, client: socket.socket, incoming: socket.socket) -> bytes:
        with self.lock:
            if not self._accepting_locked():
                return b""
            data = incoming.recv(65536, socket.MSG_DONTWAIT)
            if incoming is client:
                self.bytes_from_client += len(data)
            else:
                self.bytes_from_upstream += len(data)
            if self.max_bytes is None or self.bytes_from_client + self.bytes_from_upstream <= self.max_bytes:
                return data
            connections = self._record_locked(host, port, "budget_exceeded", "CONNECT", client)
        self._shutdown(connections)
        return b""

    def start_process(self, argv: list[str], **kwargs) -> subprocess.Popen | None:
        with self.lock:
            if not self._accepting_locked():
                return None
            return subprocess.Popen(argv, **kwargs)


class ProxyRequest(BaseHTTPRequestHandler):
    server: AuditProxy

    def log_message(self, *_args):
        pass  # Never log request lines, headers, authentication or TLS bytes.

    def do_CONNECT(self):
        upstream = None
        host, port = "invalid", 0
        try:
            target = urlsplit("//" + self.path)
            if target.username or target.password or target.path or target.query or target.fragment:
                raise ValueError("Invalid CONNECT target")
            host, port = (target.hostname or "").lower().rstrip("."), target.port or 443
            if not host or not 1 <= port <= 65535:
                raise ValueError("Invalid CONNECT authority")
            if self.server.block_authentication(host, port, self.command, self.connection):
                self.send_error(403)
                return
            proxy = self.server.upstream
            address = (proxy.hostname, proxy.port or 80) if proxy else (host, port)
            upstream = self.server.connect_upstream(self.connection, address)
            if upstream is None:
                return
            if proxy:
                authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()
                if not self.server.send(upstream, request, timeout=UPSTREAM_TIMEOUT):
                    return
                response = bytearray()
                deadline = monotonic() + UPSTREAM_TIMEOUT
                while not response.endswith(b"\r\n\r\n") and len(response) < 16384:
                    try:
                        block = upstream.recv(1)
                    except BlockingIOError:
                        wait_for_socket(upstream, deadline)
                        continue
                    if not block:
                        raise OSError("Upstream closed handshake")
                    response.extend(block)
                    deadline = monotonic() + UPSTREAM_TIMEOUT
                if not bytes(response).split(b"\r\n", 1)[0].split()[1:2] == [b"200"]:
                    raise OSError("Upstream refused tunnel")
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            if not self.server.record(host, port, "forwarded", client=self.connection):
                return
            sockets = (self.connection, upstream)
            while True:
                readable, _, _ = select.select(sockets, [], [], 300)
                if not readable:
                    return
                for incoming in readable:
                    try:
                        data = self.server.receive_payload(host, port, self.connection, incoming)
                    except BlockingIOError:
                        continue
                    except ConnectionResetError:
                        if incoming is not self.connection:
                            raise
                        self.server.record(host, port, "client_closed", client=self.connection)
                        return
                    if not data:
                        return
                    outgoing = upstream if incoming is self.connection else self.connection
                    try:
                        timeout = UPSTREAM_TIMEOUT if outgoing is upstream else None
                        if not self.server.send(outgoing, data, timeout=timeout):
                            return
                    except (BrokenPipeError, ConnectionResetError):
                        if outgoing is not self.connection:
                            raise
                        self.server.record(host, port, "client_closed", client=self.connection)
                        return
        except (OSError, ValueError):
            self.server.record(host, port, "proxy_error", client=self.connection)
            # A tunnel already acknowledged by the peer cannot receive HTTP errors.
            self.close_connection = True
        finally:
            if upstream:
                upstream.close()

    def do_GET(self):
        # Provider traffic must use a TLS tunnel whose destination we can audit.
        try:
            target = urlsplit(self.path)
            host, port = (target.hostname or "invalid").lower().rstrip("."), target.port or 80
        except ValueError:
            host, port = "invalid", 0
        if not self.server.block_authentication(host, port, self.command, self.connection):
            self.server.record(host, port, "unsupported_method", method=self.command, client=self.connection)
        self.send_error(403)

    do_POST = do_GET
    do_HEAD = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET


@contextmanager
def proxy_server(upstream: str | None, audit: Path, max_bytes: int | None = None):
    server = AuditProxy(upstream, audit, max_bytes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.finish_audit()
        server.server_close()


def pytest_summary(path: Path) -> dict:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    skipped = sum(case.find("skipped") is not None for case in cases)
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    return {"passed": len(cases) - skipped - failed, "skipped": skipped, "failed": failed}


def browser_summary(path: Path) -> dict:
    report = json.loads(path.read_text())
    stats = report["stats"]
    return {"passed": stats["expected"], "skipped": stats["skipped"],
            "failed": stats["unexpected"] + stats["flaky"] + len(report.get("errors", []))}


def accepted(summary: dict) -> bool:
    return summary["passed"] > 0 and summary["skipped"] == 0 and summary["failed"] == 0


def execute(argv: list[str], *, env: dict, log: Path, cwd: Path, proxy: AuditProxy | None = None) -> int | None:
    started = monotonic()
    launch = subprocess.Popen if proxy is None else proxy.start_process
    with log.open("w") as stream:
        process = launch(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        if process is None:
            return None
        with process:
            try:
                print(f"Running {log.stem}; output: {log}", flush=True)
                while True:
                    try:
                        return process.wait(timeout=PROGRESS_INTERVAL_SECONDS)
                    except subprocess.TimeoutExpired:
                        print(f"Still running {log.stem} ({monotonic() - started:.0f}s elapsed)", flush=True)
            finally:
                if process.poll() is None:
                    process.kill()


def traffic_line(scope: str, uploaded: int, downloaded: int) -> str:
    total_mib = (uploaded + downloaded) / (1024 * 1024)
    return f"Codex tunnel usage ({scope}): {total_mib:.2f} MiB ({uploaded:,} bytes up, {downloaded:,} bytes down)"


def run(args, *, max_bytes: int | None = None) -> int:
    source = Path(args.codex_home).expanduser().resolve(strict=True)
    auth = source / "auth.json"
    originals = {path: identity(path) if path.exists() else None for path in (auth, source / "config.toml")}
    if originals[auth] is None:
        raise ValueError("Selected login has no auth.json; no fallback login will be used")
    output = Path(args.output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    cache = Path.home() / ".cache" / "octopus-tests"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {"passed": False, "selected_home": str(source), "suites": {}, "execution": {}, "errors": []}

    def run_suite(name: str, argv: list[str], environment: dict[str, str], cwd: Path, report: Path,
                  proxy: AuditProxy) -> int | None:
        log = output / f"{name}.log"
        observation = {"argv": argv, "cwd": str(cwd), "log": str(log), "report": str(report)}
        result["execution"][name] = observation
        started = monotonic()
        before_up, before_down = proxy.byte_counts()
        try:
            code = execute(argv, env=environment, log=log, cwd=cwd, proxy=proxy)
            if code is None:
                del result["execution"][name]
                return None
            observation["exit_code"] = code
            after_up, after_down = proxy.byte_counts()
            uploaded, downloaded = after_up - before_up, after_down - before_down
            observation["network_bytes"] = {"bytes_from_client": uploaded, "bytes_from_upstream": downloaded}
            usage = traffic_line(name, uploaded, downloaded)
            with log.open("a") as stream:
                stream.write(f"\n{usage}\n")
            print(usage, flush=True)
            return code
        finally:
            observation["duration_seconds"] = monotonic() - started

    proxy = None
    try:
        if args.suite in ("pytest", "all"):
            selection_overrides = [key for key in PYTEST_SELECTION_ENVIRONMENT if os.environ.get(key)]
            if selection_overrides:
                raise ValueError(f"Unset {', '.join(selection_overrides)} to certify the requested pytest selection")
        with tempfile.TemporaryDirectory(prefix="codex-acceptance-", dir=cache) as temporary:
            runtime = Path(temporary)
            home = runtime / "selected"
            home.mkdir(mode=0o700)
            (home / "auth.json").symlink_to(auth.resolve(strict=True))
            (home / "config.toml").write_text(isolated_config(
                source / "config.toml", model=args.model, service_tier=args.service_tier))
            if (source / "models_cache.json").is_file():
                shutil.copy2(source / "models_cache.json", home / "models_cache.json")
            credentials = runtime / "credentials"
            credentials.mkdir()
            (credentials / "selected").symlink_to(home, target_is_directory=True)
            (runtime / "tmp").mkdir()
            environment = {**os.environ, "CODEX_HOME": str(home), "TMPDIR": str(runtime / "tmp"),
                           "OCTOPUS_CODEX_HOME_DIR": str(credentials), "PYTHONUNBUFFERED": "1"}
            environment["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + environment.get("PATH", "")
            # The linked login is the only provider identity, including in subprocesses.
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_ACCESS_TOKEN"):
                environment.pop(key, None)
            with proxy_server(args.upstream_proxy, output / "network.jsonl", max_bytes) as proxy:
                address = f"http://127.0.0.1:{proxy.server_port}"
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    environment[key] = address
                environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,localhost,::1"
                if args.suite in ("pytest", "all"):
                    report = output / "pytest.xml"
                    code = run_suite("pytest", [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-c", "pyproject.toml", "-n", "0",
                                    *(args.pytest_target or ["tests/"]), "--run-real",
                                    "--ignore=tests/test_codex_login_real.py", "-m", SELECTION, "-x",
                                    "-v", f"--junitxml={report}"],
                                     environment, ROOT, report, proxy)
                    if code is None:
                        raise RuntimeError("Codex pytest was stopped before launch")
                    summary = pytest_summary(report)
                    result["suites"]["pytest"] = {**summary, "exit_code": code}
                    if code or not accepted(summary):
                        raise RuntimeError("Codex pytest did not pass every selected case; inspect pytest.log")
                if args.suite in ("browser", "all"):
                    report = output / "browser.json"
                    environment["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report)
                    code = run_suite("browser", ["bun", "run", "test:e2e:llm", "codex-app-server.spec.ts", "--reporter=line,json"],
                                     environment, ROOT / "web", report, proxy)
                    if code is not None:
                        summary = browser_summary(report)
                        result["suites"]["browser"] = {**summary, "exit_code": code}
                        if code or not accepted(summary):
                            raise RuntimeError("Codex browser did not pass every selected case; inspect browser.log")
                result["passed"] = True
    except Exception as error:
        result["passed"] = False
        result["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        if proxy is not None:
            # The context has stopped accepting and retired or failed every
            # started handler before this single final network decision.
            result["network"] = proxy.summary()
            if not result["network"]["tunnels"] or result["network"]["errors"] or result["network"]["unsettled"]:
                result["passed"] = False
                result["errors"].append("Network audit missing, failed or contains unsettled handlers")
        try:
            result["selected_login_unchanged"] = all(
                (identity(path) if path.exists() else None) == original for path, original in originals.items())
        except OSError:
            result["selected_login_unchanged"] = False
        if not result["selected_login_unchanged"]:
            result["passed"] = False
            result["errors"].append("Selected login/configuration changed during acceptance")
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        if proxy is not None:
            print(traffic_line("run", result["network"]["bytes_from_client"],
                               result["network"]["bytes_from_upstream"]), flush=True)
        print(json.dumps(result, indent=2), flush=True)
    return 0 if result["passed"] else 1


def _mib_to_bytes(value: str) -> int:
    try:
        mib = int(value)
    except ValueError:
        mib = 0
    if mib <= 0:
        raise argparse.ArgumentTypeError("expected a positive whole number of MiB")
    return mib * 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME"), required=not os.environ.get("CODEX_HOME"))
    parser.add_argument("--output", required=True, help="New evidence directory; an existing directory is refused")
    parser.add_argument("--suite", choices=("pytest", "browser", "all"), default="all")
    parser.add_argument("--pytest-target", action="append", help="Optional focused test path/node id; default is the full live selection")
    parser.add_argument("--model")
    parser.add_argument("--service-tier")
    parser.add_argument("--upstream-proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    parser.add_argument("--max-mib", dest="max_bytes", type=_mib_to_bytes,
                        default=os.environ.get("OCTOPUS_CODEX_LIVE_MAX_MIB"),
                        help="Stop forwarding after this many observed tunnel MiB")
    args = parser.parse_args()
    return run(args, max_bytes=args.max_bytes)


if __name__ == "__main__":
    raise SystemExit(main())
