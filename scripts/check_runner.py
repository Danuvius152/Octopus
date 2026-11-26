"""Run full or scoped verification and optionally retain execution evidence."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
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
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .python_environment import inspect_environment

HERMETIC_PYTEST_SELECTION = "not real and not knowledge_eval and not wiki_perf"
PYTEST_SELECTION_ENVIRONMENT = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD")
OFFLINE_E2E_COUNTS = ("passed", "failed", "flaky", "skipped", "errors")
CODEX_LIVE_COUNTS = ("passed", "failed", "skipped")
TARGETED_CHECK_DIR = "OCTOPUS_TARGETED_CHECK_DIR"


@dataclass(frozen=True)
class Gate:
    id: str
    name: str
    argv: tuple[str, ...]
    cwd: Path
    scope: str
    report: str | None = None
    quiet: bool = False
    source_files: tuple[str, ...] = ()
    pytest_context: Literal["required", "diagnostic"] | None = None
    codex_targets: tuple[str, ...] = ()


class Interrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def targeted_directory() -> Path | None:
    directory = os.environ.get(TARGETED_CHECK_DIR)
    return Path(directory) if directory else None


def stop_if_requested(directory: Path | None) -> None:
    if directory is not None and (directory / "stop").exists():
        raise Interrupted(signal.SIGTERM)


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def admit_dispatch(directory: Path | None) -> Iterator[None]:
    if directory is None:
        yield
    else:
        with exclusive_lock(directory / "dispatch.lock"):
            stop_if_requested(directory)
            yield


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", errors="backslashreplace",
            dir=path.parent, prefix=".results-", delete=False,
        ) as stream:
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


def source_identity(root: Path, extra_files: tuple[str, ...] = ()) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root)

    digest = hashlib.sha256()
    git_files = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")) - {b""}
    linked_inputs = {}
    for name in extra_files:
        path = root / name
        try:
            resolved = path.resolve()
        except RuntimeError as error:
            raise ValueError(f"Focused pytest input has a symlink loop: {name}") from error
        if not resolved.is_relative_to(root):
            raise ValueError(f"Focused pytest input resolves outside the checkout: {name}")
        if resolved != path:
            linked_inputs[name] = str(resolved.relative_to(root))
    files = sorted(git_files.union(map(os.fsencode, (*extra_files, *linked_inputs.values()))))
    if linked_inputs:
        digest.update(json.dumps(linked_inputs, ensure_ascii=True, sort_keys=True).encode() + b"\n")
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
            "cpu_affinity_count": len(os.sched_getaffinity(0)),
            "variables": {key: os.environ.get(key) for key in (
                "CI", "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED", "PLAYWRIGHT_HOST_PLATFORM_OVERRIDE",
                *PYTEST_SELECTION_ENVIRONMENT, "PYTEST_XDIST_AUTO_NUM_WORKERS", "NODE_OPTIONS",
                "VITEST_MAX_WORKERS")}}


def capture_inputs(root: Path, extra_files: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"source": source_identity(root, extra_files), "environment": environment_identity(root)}


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


def release_counts(values: dict[str, Any], fields: tuple[str, ...]) -> dict[str, int]:
    counts = {field: values[field] for field in fields}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("Invalid release test count")
    return counts


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

    def __init__(self, *, term_grace: float = 5):
        self.process: subprocess.Popen | None = None
        self.term_grace = term_grace
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
            try:
                os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                pass
            else:
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

    def reap_exited_orphans(self):
        # Reap adopted descendants during the gate so their former process
        # groups can disappear while tests are still waiting for retirement.
        # Popen alone owns the original child's exit status.
        for pid in children():
            if self.process is None or pid != self.process.pid:
                os.waitpid(pid, os.WNOHANG)

    def fully_reaped(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            self.reap_exited_orphans()
            return False
        # After Popen has reaped its own child, an ECHILD result proves there
        # are no remaining children. The /proc snapshot is only for signalling.
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return True
        return False

    def wait(self, process: subprocess.Popen) -> int:
        while True:
            try:
                return process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                self.reap_exited_orphans()

    def retire(self):
        active = not self.deferred
        self.deferred = True
        deadline = time.monotonic() + self.term_grace
        notified: set[int] = set()
        try:
            while not self.fully_reaped():
                remaining = children()
                if self.process is not None and self.process.returncode is None:
                    remaining.add(self.process.pid)
                notified.intersection_update(remaining)
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


def run_gate(
    gate: Gate, output: Path | None, row: dict[str, Any], save_progress: Callable[[], None] | None = None,
) -> None:
    argv = list(gate.argv)
    report = None
    context = None
    environment = None
    if output is not None and gate.report is not None:
        report = output / (gate.id + (".xml" if gate.report == "pytest" else ".json"))
        if gate.report == "pytest":
            argv.append(f"--junitxml={report}")
            if gate.pytest_context:
                context = output / f"{gate.id}-execution.json"
                row["execution_context"] = str(context)
                if gate.pytest_context == "diagnostic":
                    python, entry = str(gate.cwd / ".venv/bin/python"), argv
                else:
                    python, *entry = argv
                launcher = Path(__file__).resolve().with_name("pytest_diagnostics.py")
                argv = [python, str(launcher), str(context), *entry]
        elif gate.report == "vitest":
            argv += ["--reporter=default", "--reporter=json", f"--outputFile={report}"]
            evidence = output / f"{gate.id}-diagnostics"
            row["diagnostics"] = str(evidence)
            environment = {**os.environ, "OCTOPUS_VITEST_EVIDENCE_DIR": str(evidence)}
        elif gate.report == "offline-e2e":
            evidence = output / gate.id
            report = evidence / "result.json"
            environment = {**os.environ, "OCTOPUS_E2E_EVIDENCE_DIR": str(evidence)}
        else:
            evidence = output / gate.id
            report = evidence / "result.json"
            argv += ["--output", str(evidence)]
    elif gate.report == "vitest" and "OCTOPUS_VITEST_EVIDENCE_DIR" in os.environ:
        environment = dict(os.environ)
        del environment["OCTOPUS_VITEST_EVIDENCE_DIR"]
    row.update(argv=argv, cwd=str(gate.cwd), scope=gate.scope, started_at=now(), returncode=None,
               status="incomplete", counts=None, evidence_error=None, execution_error=None,
               log=None, report=str(report) if report else None)
    if gate.source_files:
        row["source_files"] = list(gate.source_files)
    row["selection_environment"] = {key: os.environ.get(key) for key in (*PYTEST_SELECTION_ENVIRONMENT, "NODE_OPTIONS")}
    additional = row["selection_environment"]["PYTEST_ADDOPTS"]
    if gate.report == "pytest" and additional:
        row["scope"] += f"; inherited PYTEST_ADDOPTS={additional}"
    started = time.monotonic()
    owner = GateChildren()
    directory = targeted_directory()
    try:
        inherited = [key for key in PYTEST_SELECTION_ENVIRONMENT if row["selection_environment"][key]]
        blocked = []
        if gate.id in ("pytest", "supervisor", "codex-live-pytest"):
            blocked = inherited
        elif gate.id == "pytest-focused" and gate.pytest_context == "required":
            blocked = [key for key in inherited if key != "PYTEST_ADDOPTS"]
        if blocked:
            error = f"Unset {', '.join(blocked)} to certify the requested pytest selection"
            row.update(status="failed", evidence_error=error, report=None)
            return
        with owner:
            if output is None:
                try:
                    with admit_dispatch(directory):
                        process = owner.spawn(argv, cwd=gate.cwd, env=environment,
                                              stdout=subprocess.DEVNULL if gate.quiet else None)
                except OSError as error:
                    row.update(status="failed", execution_error=str(error))
                    print(error, file=sys.stderr)
                    return
                row["returncode"] = owner.wait(process)
            else:
                log = output / (gate.id + ".log")
                row["log"] = str(log)
                if save_progress is not None:
                    save_progress()
                with log.open("wb", buffering=0) as stream:
                    try:
                        with admit_dispatch(directory):
                            process = owner.spawn(argv, cwd=gate.cwd, env=environment,
                                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
                            else:
                                owner.reap_exited_orphans()
                    row["returncode"] = owner.wait(process)
                    stream.flush()
                    os.fsync(stream.fileno())
        row["status"] = "passed" if row["returncode"] == 0 else "failed"
        if report is not None:
            try:
                if gate.report in ("offline-e2e", "codex-live"):
                    suite = gate.id.removeprefix("codex-live-")
                    details = json.loads(report.read_text())
                    if gate.report == "offline-e2e":
                        partitions = row["partitions"] = details["partitions"]
                        counts = [release_counts(partition["counts"], OFFLINE_E2E_COUNTS)
                                  for partition in partitions if partition["counts"] is not None]
                        complete_counts = bool(partitions) and len(counts) == len(partitions)
                        if complete_counts:
                            row["counts"] = {key: sum(item[key] for item in counts) for key in OFFLINE_E2E_COUNTS}
                        confirmed = complete_counts and all(
                            part["status"] == "passed" and part["returncode"] == 0 and part["counts"]["passed"] > 0
                            for part in partitions
                        )
                    else:
                        summary = details["suites"].get(suite)
                        row["execution"] = details["execution"].get(suite)
                        row["selection"] = details.get("selection", {}).get(suite)
                        if suite == "pytest":
                            expected_selection = {
                                "scope": "targets" if gate.codex_targets else "full",
                                "targets": list(gate.codex_targets),
                            }
                        else:
                            expected_selection = {"scope": "smoke"}
                        if summary is not None:
                            row["counts"] = {**release_counts(summary, CODEX_LIVE_COUNTS), "exit_code": summary["exit_code"]}
                        confirmed = bool(summary and summary["exit_code"] == 0 and summary["skipped"] == 0 and row["execution"]
                                         and row["execution"]["exit_code"] == 0 and row["selection"] == expected_selection)
                    totals = row["counts"]
                    if row["returncode"] == 0 and not (
                        details["passed"] is True and confirmed and totals and totals["passed"] > 0
                        and not any(totals.get(key) for key in ("failed", "errors", "flaky"))
                    ):
                        row["evidence_error"] = "Release report did not confirm a passing execution"
                else:
                    totals = row["counts"] = native_counts(report, gate.report)
                    if row["returncode"] == 0 and (totals["failed"] or totals.get("errors")):
                        row["evidence_error"] = "Native report contains failed tests after a zero exit"
                    elif row["returncode"] == 0 and gate.id == "supervisor" and not totals["passed"]:
                        row["evidence_error"] = "Supervisor report contains no passing tests"
                    if context is not None and gate.pytest_context == "required":
                        context_data = json.loads(context.read_text())
                        row["execution"] = context_data
                        if row["returncode"] == 0 and (
                            context_data["exit_code"] != 0 or context_data["failures"]
                            or context_data["worker_crashes"] or not totals["total"]
                        ):
                            row["evidence_error"] = "Pytest execution report did not confirm a passing execution"
            except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as error:
                row["evidence_error"] = f"Native report unavailable: {error}"
        if gate.id == "pytest-focused" and additional:
            error = "Unset PYTEST_ADDOPTS to certify the requested pytest selection"
            row["evidence_error"] = "; ".join(filter(None, (row["evidence_error"], error)))
        if row["evidence_error"]:
            row["status"] = "failed"
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
    if any(gate.report not in (None, "pytest", "vitest", "offline-e2e", "codex-live") for gate in gates):
        raise ValueError("Unknown native report format")
    if output is None and any(gate.report in ("offline-e2e", "codex-live") for gate in gates):
        raise ValueError("Release checks require an evidence directory")
    source_files = tuple(file for gate in gates for file in gate.source_files)
    if output is not None:
        output = output.resolve()
        if output.is_relative_to(root.resolve()) and (root / ".git").exists():
            if subprocess.run(["git", "check-ignore", "-q", str(output)], cwd=root).returncode:
                raise ValueError("Evidence must be outside the checkout or in a Git-ignored directory")
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"format_version": 1, "state": "incomplete", "success": False, "started_at": now(),
              "finished_at": None, "inputs_before": None, "inputs_after": None, "inputs_changed": None,
              "gates": [{"id": gate.id, "name": gate.name, "scope": gate.scope, "status": "not-run"} for gate in gates],
              "evidence_errors": [], "exit_code": None}

    def save() -> None:
        if output is not None:
            atomic_json(output / "results.json", report)

    def interrupt(signum, frame):
        raise Interrupted(signum)

    handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    directory = targeted_directory()
    exit_code = 1
    try:
        save()
        stop_if_requested(directory)
        if output is not None:
            report["inputs_before"] = capture_inputs(root, source_files)
            save()
        for gate, row in zip(gates, report["gates"], strict=True):
            stop_if_requested(directory)
            print(f"\n── {gate.name}", flush=True)
            try:
                run_gate(gate, output, row, save)
            except OSError as error:
                row["evidence_error"] = str(error)
                raise
            save()
        stop_if_requested(directory)
        if output is not None:
            report["inputs_after"] = capture_inputs(root, source_files)
            stop_if_requested(directory)
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
        code = row.get("returncode")
        exit_detail = f" (exit {code})" if code not in (None, 0) else ""
        evidence_detail = f" — {row['evidence_error']}" if row.get("evidence_error") else ""
        print(f"  {row['status']}  {row['name']}{exit_detail}{evidence_detail}")
    for error in report["evidence_errors"]:
        print(f"  evidence error: {error}", file=sys.stderr)
    if output is not None:
        print(f"Evidence: {output / 'results.json'}")
    return exit_code


def commit_gates(root: Path, e2e: bool) -> list[Gate]:
    python = str(root / ".venv/bin/python")
    backend = f"tests/; {HERMETIC_PYTEST_SELECTION}"
    gates = [
        Gate("python-pins", "Python dependency pins", (python, "scripts/python_environment.py", "check", "--extra", "test", "--extra", "dev"), root, "runtime + test + dev", quiet=True),
        Gate("hook", "commit hook installed", (str(root / "scripts/setup-hooks.sh"), "--check"), root, "pre-commit installation"),
        Gate("ruff", "backend lint (ruff)", (str(root / ".venv/bin/ruff"), "check", "."), root, "repository Python"),
        Gate("test-imports", "test support imports", (python, "scripts/check_test_imports.py"), root, "test import baseline"),
        Gate("mypy", "backend types (mypy)", (str(root / ".venv/bin/mypy"),), root, "configured mypy selection"),
        Gate("pytest", "backend unit (hermetic release selection)",
             (str(root / ".venv/bin/pytest"), "-c", "pyproject.toml", "tests/", "-m", HERMETIC_PYTEST_SELECTION,
              "-n", "auto", "--maxprocesses", "8", "--dist=loadfile", "--max-worker-restart=0", "-q"),
             root, backend, "pytest"),
        Gate("eslint", "frontend lint (eslint)", ("bun", "run", "lint"), root / "web", "ESLint configuration"),
        Gate("vitest", "frontend unit (vitest)", ("bun", "run", "test"), root / "web", "Vitest configuration", "vitest"),
        Gate("tsc", "typecheck (tsc -b)", ("bun", "run", "typecheck"), root / "web", "app, node and E2E TypeScript projects"),
        Gate("contracts", "generated contracts in sync", (str(root / "scripts/check-contracts.sh"),), root, "OpenAPI TypeScript contracts"),
        Gate("docs-index", "docs indexes in sync", (python, "scripts/gen-docs-index.py", "--check"), root, "contract and plan indexes"),
    ]
    if e2e:
        gates.append(Gate("e2e", "e2e (playwright)", ("bun", "run", "test:e2e"), root / "web", "offline and live browser selection"))
    return gates


def release_gates(root: Path, *, supervisor: bool, codex_full: bool = False,
                  codex_targets: tuple[str, ...] = ()) -> list[Gate]:
    python = str(root / ".venv/bin/python")
    gates = [
        replace(gate, argv=(python, "-m", "pytest", *gate.argv[1:]), pytest_context="required")
        if gate.id == "pytest" else gate
        for gate in commit_gates(root, e2e=False)
    ]
    if supervisor:
        gates.append(Gate("supervisor", "isolated systemd supervisor", (
            python, "-m", "pytest", "-c", "pyproject.toml", "-n", "0",
            "tests/",
            "--run-real", "-m", "real and supervisor", "-q",
        ), root, "isolated systemd user units", "pytest", pytest_context="required"))
    gates.append(Gate("offline-e2e", "offline browser E2E", ("bun", "run", "test:e2e:fast"),
                      root / "web", "all offline browser partitions", "offline-e2e"))
    if codex_full or codex_targets:
        target_args = tuple(f"--pytest-target={target}" for target in codex_targets)
        scope = f"live Codex pytest targets: {', '.join(codex_targets)}" if codex_targets else "full live Codex pytest"
        gates.append(Gate("codex-live-pytest", "live Codex pytest",
                          (python, "scripts/codex_live.py", "--suite", "pytest", *target_args),
                          root, scope, "codex-live", source_files=pytest_source_files(codex_targets),
                          codex_targets=codex_targets))
    gates.append(Gate("codex-live-browser", "live Codex browser",
                      (python, "scripts/codex_live.py", "--suite", "browser"),
                      root, "live Codex browser smoke", "codex-live"))
    return gates


def pytest_target(root: Path, target: str) -> str:
    filename, separator, node = target.partition("::")
    path = (root / filename).resolve()
    if path.suffix != ".py" or not path.is_relative_to(root / "tests") or not path.is_file():
        raise argparse.ArgumentTypeError(f"expected a Python test file or node ID under tests/, got {target!r}")
    return f"{path.relative_to(root)}{separator}{node}"


def codex_pytest_target(root: Path, target: str) -> str:
    target = pytest_target(root, target)
    if target.partition("::")[0] == "tests/test_codex_login_real.py":
        raise argparse.ArgumentTypeError("Device authentication is outside the Codex live acceptance selection")
    return target


def pytest_input(root: Path, filename: str) -> str:
    path = root / filename
    if not path.is_relative_to(root) or not path.is_file() or not path.resolve().is_relative_to(root):
        raise argparse.ArgumentTypeError(f"expected an additional pytest input file in the checkout, got {filename!r}")
    return str(path.relative_to(root))


def pytest_source_files(targets: tuple[str, ...], inputs: Iterable[str] = ()) -> tuple[str, ...]:
    test_files = {Path(target.partition("::")[0]) for target in targets}
    conftests = {parent / "conftest.py" for path in test_files for parent in path.parents}
    return tuple(sorted(set(inputs).union(map(str, test_files | conftests))))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    available = commit_gates(root, e2e=True)
    release_available = release_gates(root, supervisor=True, codex_full=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--e2e", action="store_true", help="add Playwright to the full commit checks")
    parser.add_argument("--release", action="store_true", help="run commit gates, offline E2E and Codex browser smoke")
    parser.add_argument("--supervisor", action="store_true", help="include isolated supervisor checks with --release")
    parser.add_argument("--gate", action="append", choices=sorted({gate.id for gate in available + release_available}),
                        help="run only named gates (repeat to select additional gates)")
    parser.add_argument("--pytest-target", action="append", metavar="FILE_OR_NODE_ID",
                        type=lambda target: pytest_target(root, target),
                        help="run focused hermetic pytest (serial by default, repeat to select more tests)")
    codex = parser.add_mutually_exclusive_group()
    codex.add_argument("--codex-live-full", action="store_true", help="include the full Codex pytest capability matrix")
    codex.add_argument("--codex-live-target", action="append", metavar="FILE_OR_NODE_ID",
                       type=lambda target: codex_pytest_target(root, target),
                       help="include a live Codex capability target (repeat for multiple targets)")
    parser.add_argument("--pytest-workers", type=int, metavar="COUNT",
                        help="set the xdist worker count for a targeted release diagnostic run")
    parser.add_argument("--pytest-input", action="append", metavar="FILE", default=[],
                        type=lambda filename: pytest_input(root, filename),
                        help="fingerprint an additional focused pytest input file (repeat for helpers and data)")
    parser.add_argument("--output", type=Path, help="new evidence directory outside the checkout or Git-ignored")
    args = parser.parse_args()
    if args.supervisor and not args.release:
        parser.error("--supervisor requires --release")
    if args.release and (args.e2e or not args.output):
        parser.error("--release requires --output and supplies its own browser selection")
    if args.codex_live_full or args.codex_live_target:
        if not args.release or args.pytest_target or (args.gate and "codex-live-pytest" not in args.gate):
            parser.error("Codex live options require a release selection that includes codex-live-pytest")
    if args.release:
        codex_targets = tuple(dict.fromkeys(args.codex_live_target or ()))
        available = release_gates(
            root, supervisor=args.supervisor or "supervisor" in (args.gate or ()),
            codex_full=args.codex_live_full or ("codex-live-pytest" in (args.gate or ()) and not codex_targets),
            codex_targets=codex_targets,
        )
    if set(args.gate or ()).difference(gate.id for gate in available):
        parser.error("Choose gates from the selected commit or release check set")
    if args.pytest_input and not args.pytest_target:
        parser.error("--pytest-input requires --pytest-target")
    if args.pytest_workers is not None and (not args.release or not args.pytest_target or args.pytest_workers < 0):
        parser.error("--pytest-workers requires --release, --pytest-target and a nonnegative count")
    if args.gate or args.pytest_target:
        if args.e2e:
            parser.error("--e2e adds Playwright to the full suite. Use --gate e2e for a selected run")
        gates = [gate for gate in available if gate.id in (args.gate or ())]
        if args.pytest_target:
            if "pytest" in (args.gate or ()):
                parser.error("Choose full --gate pytest or focused --pytest-target")
            targets = tuple(dict.fromkeys(args.pytest_target))
            command = ((str(root / ".venv/bin/python"), "-m", "pytest") if args.release
                       else (str(root / ".venv/bin/pytest"),))
            workers = ("-n", str(args.pytest_workers or 0)) if args.release else ()
            distribution = ("--dist=loadfile", "--max-worker-restart=0") if args.pytest_workers else ()
            config = ("-c", "pyproject.toml") if args.release else ()
            gates.append(Gate(
                "pytest-focused", "backend unit (focused hermetic selection)",
                (*command, *config, *targets, "-m", HERMETIC_PYTEST_SELECTION, *workers, *distribution, "-q"),
                root, f"{', '.join(targets)} ({HERMETIC_PYTEST_SELECTION})", "pytest",
                source_files=pytest_source_files(targets, args.pytest_input),
                pytest_context="required" if args.release else "diagnostic",
            ))
    elif args.release:
        gates = available
    else:
        gates = commit_gates(root, e2e=args.e2e)
        if not args.e2e:
            print("skipped: e2e — run ./scripts/check.sh --e2e (needs signed-in CLIs)", flush=True)
    try:
        return run_checks(root, gates, args.output)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
