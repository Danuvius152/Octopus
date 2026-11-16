"""Run the commit gates, optionally retaining evidence from this execution."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.python_environment import inspect_environment


@dataclass(frozen=True)
class Gate:
    id: str
    name: str
    argv: tuple[str, ...]
    cwd: Path
    scope: str
    report: str | None = None
    quiet: bool = False


class Interrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".results-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def source_identity(root: Path) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root)

    digest = hashlib.sha256()
    files = sorted(set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")) - {b""})
    for name in files:
        path = root / os.fsdecode(name)
        try:
            info = path.lstat()
        except FileNotFoundError:
            identity: Any = "missing"
        else:
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                raise ValueError(f"Unsupported source entry: {path}")
            identity = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), content]
        digest.update(json.dumps([os.fsdecode(name), identity], ensure_ascii=True).encode() + b"\n")
    changes = git("status", "--porcelain=v1", "-z").decode(errors="surrogateescape").split("\0")
    config_names = {"pyproject.toml", "constraints-python.txt", "constraints-wiki.txt", "lefthook.yml",
                    "web/package.json", "web/bun.lock", "web/eslint.config.js", "web/vite.config.ts", "web/playwright.config.ts"}
    configs = {os.fsdecode(name): hashlib.sha256((root / os.fsdecode(name)).read_bytes()).hexdigest()
               for name in files if os.fsdecode(name) in config_names or os.fsdecode(name).startswith("web/tsconfig")
               if (root / os.fsdecode(name)).is_file()}
    return {"head": git("rev-parse", "HEAD").decode().strip(), "dirty": any(changes),
            "changes": [row for row in changes if row], "files": len(files),
            "working_tree_sha256": digest.hexdigest(), "configuration_sha256": configs}


def environment_identity(root: Path) -> dict[str, Any]:
    python = inspect_environment(sys.executable)
    python["packages"] = {name: row["version"] for name, row in python["packages"].items()}
    tools = {}
    for name in ("bun", "node", "lefthook"):
        path = shutil.which(name)
        if path is None:
            tools[name] = {"path": None}
            continue
        result = subprocess.run([path, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        tools[name] = {"path": path, "resolved_path": str(Path(path).resolve()),
                       "version": result.stdout.strip(), "returncode": result.returncode}
    bun = shutil.which("bun")
    packages = None
    if bun is not None:
        result = subprocess.run([bun, "pm", "ls", "--all"], cwd=root / "web", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        packages = {"returncode": result.returncode, "installed_tree": result.stdout}
    return {"python": python, "tools": tools, "frontend_dependencies": packages,
            "variables": {key: os.environ.get(key) for key in (
                "CI", "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED", "PLAYWRIGHT_HOST_PLATFORM_OVERRIDE",
                "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "NODE_OPTIONS")}}


def capture_inputs(root: Path) -> dict[str, Any]:
    return {"source": source_identity(root), "environment": environment_identity(root)}


def native_counts(path: Path, kind: str) -> dict[str, int]:
    def count(value: Any) -> int:
        if isinstance(value, bool) or not str(value).isdigit():
            raise ValueError("Invalid native test count")
        return int(value)

    if kind == "pytest":
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("./testsuite"))
        if not suites:
            raise ValueError("No JUnit test suites")
        result = {"total": 0, "failed": 0, "errors": 0, "skipped": 0}
        for suite in suites:
            for key, attribute in (("total", "tests"), ("failed", "failures"), ("errors", "errors"), ("skipped", "skipped")):
                result[key] += count(suite.attrib[attribute])
        result["passed"] = result["total"] - result["failed"] - result["errors"] - result["skipped"]
        if result["passed"] < 0:
            raise ValueError("Inconsistent JUnit test counts")
        return result
    data = json.loads(path.read_text())
    return {key: count(data[field]) for key, field in (
        ("total", "numTotalTests"), ("passed", "numPassedTests"), ("failed", "numFailedTests"),
        ("skipped", "numPendingTests"), ("todo", "numTodoTests"))}


def subreaper(value: int | None = None) -> int:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "Check execution requires Linux child subreaper support")
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot read child subreaper setting")
    if value is not None and libc.prctl(36, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot set child subreaper setting")
    return previous.value


def children() -> set[int]:
    result = set()
    for task in Path("/proc/self/task").iterdir():
        try:
            result.update(map(int, (task / "children").read_text().split()))
        except FileNotFoundError:
            continue  # A thread can exit while enumerating its children.
    return result


class GateChildren:
    """Own one sequential gate, including orphans that create their own session."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.pending: int | None = None
        self.deferred = True
        self.previous: int | None = None
        self.handlers: dict[int, Any] = {}

    def interrupt(self, signum, frame):
        self.pending = self.pending or signum
        if not self.deferred:
            raise Interrupted(self.pending)

    def __enter__(self):
        self.handlers = {sig: signal.signal(sig, self.interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
                raise OSError("Check execution requires default SIGCHLD handling to retain real child exit status")
            self.previous = subreaper()
            if children():
                raise OSError("Check execution requires no existing child processes")
            subreaper(1)
            if self.pending:
                raise Interrupted(self.pending)
            return self
        except BaseException:
            self.restore()
            raise

    def spawn(self, argv, **kwargs):
        if self.pending:
            raise Interrupted(self.pending)
        # Defer cancellation until Popen has assigned the owned child handle.
        self.process = subprocess.Popen(argv, **kwargs)
        self.deferred = False
        if self.pending:
            raise Interrupted(self.pending)
        return self.process

    def retire(self):
        active = not self.deferred
        self.deferred = True
        deadline = time.monotonic() + 5
        notified: set[int] = set()
        try:
            while True:
                # Popen alone owns the original child's real exit status.
                if self.process is not None:
                    self.process.poll()
                    if self.process.returncode is not None:
                        notified.discard(self.process.pid)
                remaining = children()
                for pid in remaining:
                    if self.process is None or pid != self.process.pid:
                        reaped, _ = os.waitpid(pid, os.WNOHANG)
                        notified.discard(reaped)
                remaining = children()
                notified.intersection_update(remaining)
                if not remaining:
                    break
                signum = signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL
                for pid in remaining:
                    # Repeated TERM can reenter a child's graceful shutdown
                    # handler. Notify each living direct child once per grace.
                    if signum == signal.SIGTERM and pid in notified:
                        continue
                    try:
                        os.kill(pid, signum)
                        notified.add(pid)
                    except ProcessLookupError:
                        pass
                if time.monotonic() > deadline + 5:
                    raise OSError("Cannot confirm all gate descendants have retired")
                time.sleep(0.02)
        finally:
            self.deferred = not active
        if active and self.pending:
            raise Interrupted(self.pending)

    def restore(self):
        try:
            if self.previous is not None:
                subreaper(self.previous)
        finally:
            for sig, previous in self.handlers.items():
                signal.signal(sig, previous)

    def __exit__(self, kind, error, traceback):
        self.deferred = True
        try:
            self.retire()
        except BaseException:
            # Keep adoption enabled if retirement could not be confirmed.
            self.previous = None
            raise
        finally:
            self.restore()
        if self.pending:
            raise Interrupted(self.pending)


def write_all(stream, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError("Output stream stopped before all bytes were written")
        remaining = remaining[written:]


def run_gate(gate: Gate, output: Path | None, row: dict[str, Any]) -> None:
    argv = list(gate.argv)
    report = None
    if output is not None and gate.report is not None:
        report = output / (gate.id + (".xml" if gate.report == "pytest" else ".json"))
        argv += ([f"--junitxml={report}"] if gate.report == "pytest" else
                 ["--reporter=default", "--reporter=json", f"--outputFile={report}"])
    row.update(argv=argv, cwd=str(gate.cwd), scope=gate.scope, started_at=now(), returncode=None,
               status="incomplete", counts=None, evidence_error=None, execution_error=None,
               log=None, report=str(report) if report else None)
    row["selection_environment"] = {key: os.environ.get(key) for key in (
        "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "NODE_OPTIONS")}
    additional = row["selection_environment"]["PYTEST_ADDOPTS"]
    if gate.report == "pytest" and additional:
        row["scope"] += f"; inherited PYTEST_ADDOPTS={additional}"
    started = time.monotonic()
    owner = GateChildren()
    try:
        with owner:
            if output is None:
                try:
                    process = owner.spawn(argv, cwd=gate.cwd, stdout=subprocess.DEVNULL if gate.quiet else None)
                except OSError as error:
                    row.update(status="failed", execution_error=str(error))
                    print(error, file=sys.stderr)
                    return
                row["returncode"] = process.wait()
            else:
                log = output / (gate.id + ".log")
                row["log"] = str(log)
                with log.open("wb", buffering=0) as stream:
                    try:
                        process = owner.spawn(argv, cwd=gate.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    except OSError as error:
                        row.update(status="failed", execution_error=str(error))
                        write_all(stream, (str(error) + "\n").encode())
                        print(error, file=sys.stderr)
                        return
                    with process.stdout, selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            if selector.select(timeout=0.1):
                                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                                if not chunk:
                                    break
                                write_all(stream, chunk)
                                if not gate.quiet:
                                    write_all(sys.stdout.buffer, chunk)
                                    sys.stdout.buffer.flush()
                            if process.poll() is not None:
                                # A descendant may still hold stdout after the
                                # original child exits. Retire it, then drain.
                                owner.retire()
                    row["returncode"] = process.wait()
                    stream.flush()
                    os.fsync(stream.fileno())
        row["status"] = "passed" if row["returncode"] == 0 else "failed"
        if report is not None:
            try:
                row["counts"] = native_counts(report, gate.report)
            except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
                row["evidence_error"] = f"Native report unavailable: {error}"
        if gate.id == "pytest" and additional:
            error = "Inherited PYTEST_ADDOPTS prevents certifying the full gate; rerun with it unset"
            row["evidence_error"] = "; ".join(filter(None, (row["evidence_error"], error)))
    except Interrupted:
        row["status"] = "interrupted"
        raise
    finally:
        if owner.process is not None:
            row["returncode"] = owner.process.returncode
        row.update(finished_at=now(), duration_seconds=time.monotonic() - started)


def run_checks(root: Path, gates: list[Gate], output: Path | None = None) -> int:
    if len({gate.id for gate in gates}) != len(gates) or any(not re.fullmatch(r"[a-z0-9-]+", gate.id) for gate in gates):
        raise ValueError("Gate identifiers must be unique safe filenames")
    if any(gate.report not in (None, "pytest", "vitest") for gate in gates):
        raise ValueError("Unknown native report format")
    if output is not None:
        output = output.resolve()
        if output.is_relative_to(root.resolve()) and (root / ".git").exists():
            if subprocess.run(["git", "check-ignore", "-q", str(output)], cwd=root).returncode:
                raise ValueError("Evidence must be outside the checkout or in a Git-ignored directory")
        output.mkdir(parents=True, exist_ok=False)
    report = {"format_version": 1, "state": "incomplete", "success": False, "started_at": now(),
              "finished_at": None, "inputs_before": None, "inputs_after": None, "inputs_changed": None,
              "gates": [], "evidence_errors": [], "exit_code": None}

    def save() -> None:
        if output is not None:
            atomic_json(output / "results.json", report)

    def interrupt(signum, frame):
        raise Interrupted(signum)

    handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    exit_code = 1
    try:
        save()
        if output is not None:
            report["inputs_before"] = capture_inputs(root)
            save()
        for gate in gates:
            print(f"\n── {gate.name}", flush=True)
            row = {"id": gate.id, "name": gate.name, "status": "incomplete"}
            report["gates"].append(row)
            save()
            try:
                run_gate(gate, output, row)
            except OSError as error:
                row["evidence_error"] = str(error)
                raise
            save()
        if output is not None:
            report["inputs_after"] = capture_inputs(root)
            report["inputs_changed"] = report["inputs_before"] != report["inputs_after"]
            if report["inputs_changed"]:
                report["evidence_errors"].append("Verification inputs changed during execution")
        report["state"] = "complete"
        report["success"] = all(row["status"] == "passed" and not row["evidence_error"] for row in report["gates"]) and not report["evidence_errors"]
        exit_code = 0 if report["success"] else 1
    except Interrupted as error:
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        report["state"] = "interrupted"
        exit_code = 128 + error.signum
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["evidence_errors"].append(str(error))
        print(f"Verification incomplete: {error}", file=sys.stderr)
    finally:
        report.update(finished_at=now(), exit_code=exit_code)
        try:
            save()
        except OSError as error:
            exit_code = 1
            print(f"Cannot save verification evidence: {error}", file=sys.stderr)
        for sig, previous in handlers.items():
            signal.signal(sig, previous)
    print("\n── summary", flush=True)
    for row in report["gates"]:
        print(f"  {row['status']}  {row['name']}" + (f" — {row['evidence_error']}" if row.get("evidence_error") else ""))
    for error in report["evidence_errors"]:
        print(f"  evidence error: {error}", file=sys.stderr)
    if output is not None:
        print(f"Evidence: {output / 'results.json'}")
    return exit_code


def commit_gates(root: Path, e2e: bool) -> list[Gate]:
    python = str(root / ".venv/bin/python")
    backend = "tests/; not real and not knowledge_eval and not wiki_perf"
    gates = [
        Gate("python-pins", "Python dependency pins", (python, "scripts/python_environment.py", "check", "--extra", "test", "--extra", "dev"), root, "runtime + test + dev", quiet=True),
        Gate("hook", "commit hook installed", (str(root / "scripts/setup-hooks.sh"), "--check"), root, "pre-commit installation"),
        Gate("ruff", "backend lint (ruff)", (str(root / ".venv/bin/ruff"), "check", "."), root, "repository Python"),
        Gate("test-imports", "test support imports", (python, "scripts/check_test_imports.py"), root, "test import baseline"),
        Gate("mypy", "backend types (mypy)", (str(root / ".venv/bin/mypy"),), root, "configured mypy selection"),
        Gate("pytest", "backend unit (hermetic release selection)", (str(root / ".venv/bin/pytest"), "tests/", "-m", "not real and not knowledge_eval and not wiki_perf", "-n", "4", "--dist=loadfile", "--max-worker-restart=0", "-q"), root, backend, "pytest"),
        Gate("eslint", "frontend lint (eslint)", ("bun", "run", "lint"), root / "web", "ESLint configuration"),
        Gate("vitest", "frontend unit (vitest)", ("bun", "run", "test"), root / "web", "Vitest configuration", "vitest"),
        Gate("tsc", "typecheck (tsc -b)", ("bun", "run", "typecheck"), root / "web", "app, node and E2E TypeScript projects"),
        Gate("contracts", "generated contracts in sync", (str(root / "scripts/check-contracts.sh"),), root, "OpenAPI TypeScript contracts"),
        Gate("docs-index", "docs indexes in sync", (python, "scripts/gen-docs-index.py", "--check"), root, "contract and plan indexes"),
    ]
    if e2e:
        gates.append(Gate("e2e", "e2e (playwright)", ("bun", "run", "test:e2e"), root / "web", "offline and live browser selection"))
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--e2e", action="store_true")
    parser.add_argument("--output", type=Path, help="new evidence directory outside the checkout or Git-ignored")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.e2e:
        print("skipped: e2e — run ./scripts/check.sh --e2e (needs signed-in CLIs)", flush=True)
    try:
        return run_checks(root, commit_gates(root, args.e2e), args.output)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
