"""Run Codex acceptance with one selected login, isolation and network evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import tomllib
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SELECTION = "real and (real_codex or codex_bin) and not real_claude and not knowledge_eval and not wiki_perf"


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


class AuditProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: str | None, audit: Path):
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

    def process_request(self, request, client_address):
        with self.lock:
            self.requests[request] = None
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

    def attach_upstream(self, client: socket.socket, upstream: socket.socket) -> None:
        with self.lock:
            if not self.closing:
                self.requests[client] = upstream
                return
        upstream.close()
        raise OSError("Audit closed before the upstream connection settled")

    def finish_audit(self) -> None:
        # Called after the accept loop stops. Unsettled work remains a failure
        # even if closing its sockets lets the daemon handler retire afterward.
        with self.lock:
            self.closing = True
            self.unsettled = len(self.requests)
            connections = [*self.requests, *(value for value in self.requests.values() if value is not None)]
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def summary(self) -> dict:
        with self.lock:
            return {"tunnels": sum(row["outcome"] == "forwarded" for row in self.records),
                    "client_closed": sum(row["outcome"] == "client_closed" for row in self.records),
                    "authentication_attempts": sum(row["outcome"] == "authentication_blocked" for row in self.records),
                    "errors": sum(row["outcome"] not in ("forwarded", "client_closed") for row in self.records),
                    "unsettled": self.unsettled}

    def record(self, host: str, port: int, outcome: str, *, method: str = "CONNECT") -> None:
        row = {"method": method, "host": host, "port": port, "outcome": outcome}
        with self.lock:
            self.records.append(row)
            with self.audit.open("a") as stream:
                stream.write(json.dumps(row) + "\n")


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
            if host == "auth.openai.com" or host.endswith(".auth.openai.com"):
                self.server.record(host, port, "authentication_blocked")
                self.send_error(403)
                return
            proxy = self.server.upstream
            address = (proxy.hostname, proxy.port or 80) if proxy else (host, port)
            upstream = socket.create_connection(address, timeout=15)
            self.server.attach_upstream(self.connection, upstream)
            if proxy:
                authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                upstream.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
                response = bytearray()
                while not response.endswith(b"\r\n\r\n") and len(response) < 16384:
                    block = upstream.recv(1)
                    if not block:
                        raise OSError("Upstream closed handshake")
                    response.extend(block)
                if not bytes(response).split(b"\r\n", 1)[0].split()[1:2] == [b"200"]:
                    raise OSError("Upstream refused tunnel")
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            self.server.record(host, port, "forwarded")
            sockets = (self.connection, upstream)
            while True:
                readable, _, _ = select.select(sockets, [], [], 300)
                if not readable:
                    return
                for incoming in readable:
                    try:
                        data = incoming.recv(65536)
                    except ConnectionResetError:
                        if incoming is not self.connection:
                            raise
                        self.server.record(host, port, "client_closed")
                        return
                    if not data:
                        return
                    outgoing = upstream if incoming is self.connection else self.connection
                    try:
                        outgoing.sendall(data)
                    except (BrokenPipeError, ConnectionResetError):
                        if outgoing is not self.connection:
                            raise
                        self.server.record(host, port, "client_closed")
                        return
        except (OSError, ValueError):
            self.server.record(host, port, "proxy_error")
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
        outcome = "authentication_blocked" if host == "auth.openai.com" or host.endswith(".auth.openai.com") else "unsupported_method"
        self.server.record(host, port, outcome, method=self.command)
        self.send_error(403)

    do_POST = do_GET
    do_HEAD = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET


@contextmanager
def proxy_server(upstream: str | None, audit: Path):
    server = AuditProxy(upstream, audit)
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


def execute(argv: list[str], *, env: dict, log: Path, cwd: Path) -> int:
    print(f"Running {log.stem}; output: {log}", flush=True)
    with log.open("w") as stream:
        return subprocess.run(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
                              check=False).returncode


def run(args) -> int:
    source = Path(args.codex_home).expanduser().resolve(strict=True)
    auth = source / "auth.json"
    originals = {path: identity(path) if path.exists() else None for path in (auth, source / "config.toml")}
    if originals[auth] is None:
        raise ValueError("Selected login has no auth.json; no fallback login will be used")
    output = Path(args.output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    cache = Path.home() / ".cache" / "octopus-tests"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {"passed": False, "selected_home": str(source), "suites": {}, "errors": []}
    proxy = None
    try:
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
            with proxy_server(args.upstream_proxy, output / "network.jsonl") as proxy:
                address = f"http://127.0.0.1:{proxy.server_port}"
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    environment[key] = address
                environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,localhost,::1"
                if args.suite in ("pytest", "all"):
                    report = output / "pytest.xml"
                    code = execute([str(ROOT / ".venv/bin/python"), "-m", "pytest",
                                    *(args.pytest_target or ["tests/"]), "--run-real",
                                    "--ignore=tests/test_codex_login_real.py", "-m", SELECTION,
                                    "-v", f"--junitxml={report}"],
                                   env=environment, log=output / "pytest.log", cwd=ROOT)
                    summary = pytest_summary(report)
                    result["suites"]["pytest"] = {**summary, "exit_code": code}
                    if code or not accepted(summary):
                        raise RuntimeError("Codex pytest did not pass every selected case; inspect pytest.log")
                if args.suite in ("browser", "all"):
                    report = output / "browser.json"
                    environment["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report)
                    code = execute(["bun", "run", "test:e2e:llm", "codex-app-server.spec.ts", "--reporter=line,json"],
                                   env=environment, log=output / "browser.log", cwd=ROOT / "web")
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
        print(json.dumps(result, indent=2), flush=True)
    return 0 if result["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME"), required=not os.environ.get("CODEX_HOME"))
    parser.add_argument("--output", required=True, help="New evidence directory; an existing directory is refused")
    parser.add_argument("--suite", choices=("pytest", "browser", "all"), default="all")
    parser.add_argument("--pytest-target", action="append", help="Optional focused test path/node id; default is the full live selection")
    parser.add_argument("--model")
    parser.add_argument("--service-tier")
    parser.add_argument("--upstream-proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
