"""Own a fixed candidate's check from entry-point startup through retirement."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType
from typing import Any

from .check_runner import (
    TARGETED_CHECK_DIR,
    GateChildren,
    Interrupted,
    admit_dispatch,
    atomic_json,
    exclusive_lock,
    stop_if_requested,
    sync_directory,
)

PLAYWRIGHT_OWNER = "targeted-playwright-owner.json"
PLAYWRIGHT_LOCK = "targeted-playwright.lock"


def retirement(directory: Path) -> dict[str, Any] | None:
    try:
        proof = json.loads((directory / "retirement.json").read_text())
    except (FileNotFoundError, ValueError):
        return None
    if (not isinstance(proof, dict) or proof.get("run_directory") != str(directory)
        or proof.get("format_version") != 1 or not {"runner_pid", "returncode"}.issubset(proof)):
        return None
    pid, status = proof.get("runner_pid"), proof.get("returncode")
    if (pid is None and status is None) or (type(pid) is int and pid > 0 and type(status) is int):
        # The proof becomes visible at rename, before the writer's directory sync.
        sync_directory(directory)
        return proof
    return None


def run_command(directory: Path, argv: list[str]) -> int:
    interrupted: int | None = None

    def remember_interrupt(signum: int, _frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = interrupted or signum

    # The run subcommand owns these signals through CLI exit, including when
    # main reports a launch or retirement error.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, remember_interrupt)

    directory.mkdir(mode=0o700)
    stop = directory / "stop"
    launch: dict[str, Any] = {
        "format_version": 1, "run_directory": str(directory), "cwd": os.getcwd(), "argv": argv, "runner_pid": None,
    }
    environment = {**os.environ, TARGETED_CHECK_DIR: str(directory)}
    launch_failed = False
    # Allow the nested gate owner to complete its own TERM and KILL phases.
    with GateChildren(term_grace=12, handle_signals=False) as owner:
        with exclusive_lock(directory / "dispatch.lock"):
            if not stop.exists() and interrupted is None:
                owner.spawn(argv, env=environment)
        process = owner.process
        if process is not None:
            launch["runner_pid"] = process.pid
            try:
                atomic_json(directory / "launch.json", launch)
            except OSError as error:
                print(f"Cannot write check launch record for {directory}: {error}", file=sys.stderr)
                launch_failed = True
            else:
                while not stop.exists() and interrupted is None:
                    try:
                        process.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        owner.reap_exited_orphans()

    # Leaving the owner successfully proves retirement. Startup errors and
    # unconfirmed retirement bypass publication and leave the reservation intact.
    returncode = process.returncode if process is not None else None
    stopped = stop.exists()
    atomic_json(directory / "retirement.json", {
        **launch, "returncode": returncode, "stop_requested": interrupted is not None or stopped,
    })
    cleanup_failed = False
    try:
        clean_playwright(directory)
    except (OSError, ValueError) as error:
        print(f"Playwright cleanup pending for {directory}: {error}", file=sys.stderr)
        cleanup_failed = True

    if interrupted is not None:
        return 128 + interrupted
    if stopped:
        return 128 + signal.SIGTERM
    if launch_failed or cleanup_failed:
        return 1
    assert returncode is not None
    return shell_status(returncode)


def shell_status(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


def playwright_cache() -> Path:
    # The fixed ports are shared even when a wrapped command replaces HOME.
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / ".cache/octopus-tests"


def playwright_runtime(cache: Path, directory: Path) -> Path:
    name = hashlib.sha256(os.fsencode(directory)).hexdigest()[:32]
    return cache / f"e2e-targeted-{name}"


def playwright_owner(cache: Path) -> Path | None:
    try:
        name = json.loads((cache / PLAYWRIGHT_OWNER).read_text())
    except FileNotFoundError:
        return None
    if not isinstance(name, str) or not Path(name).is_absolute():
        raise ValueError("Invalid targeted Playwright port reservation")
    return Path(name)


def reserve_playwright(directory: Path) -> Path:
    # Targeted Playwright uses the existing fixed service ports. The last
    # owner remains recorded across a launcher crash or external force kill.
    cache = playwright_cache()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with exclusive_lock(cache / PLAYWRIGHT_LOCK, blocking=False):
            previous = playwright_owner(cache)
            if previous is not None and previous != directory:
                raise ValueError(f"Playwright ports are retained until retirement and cleanup of {previous}")
            stop_if_requested(directory)
            runtime = playwright_runtime(cache, directory)
            atomic_json(directory / "playwright-runtime.json", str(runtime))
            runtime.mkdir(mode=0o700, exist_ok=True)
            atomic_json(cache / PLAYWRIGHT_OWNER, str(directory))
            return runtime
    except BlockingIOError as error:
        raise ValueError("Playwright ports are in use by another service launch") from error


def clean_playwright(directory: Path) -> None:
    try:
        recorded = json.loads((directory / "playwright-runtime.json").read_text())
    except FileNotFoundError:
        return
    cache = playwright_cache()
    expected = playwright_runtime(cache, directory)
    if recorded != str(expected):
        raise ValueError("Invalid targeted Playwright runtime reference")

    def pending() -> bool:
        # Both resources can only disappear once this run has proved exit.
        return os.path.lexists(expected) or playwright_owner(cache) == directory

    if not pending():
        return
    try:
        with exclusive_lock(cache / PLAYWRIGHT_LOCK, blocking=False):
            try:
                shutil.rmtree(expected)
            except FileNotFoundError:
                pass
            if playwright_owner(cache) == directory:
                (cache / PLAYWRIGHT_OWNER).unlink()
    except BlockingIOError as error:
        if pending():
            raise ValueError("Playwright port lease is busy; retry cleanup") from error


def dispatch_playwright(directory: Path, cwd: Path, started: Path, argv: list[str]) -> int:
    # The outer subreaper owns this helper and the native process. Hold the
    # stop lock only through Popen, so stopping does not wait for the partition.
    with admit_dispatch(directory):
        process = subprocess.Popen(argv, cwd=cwd)
        started.touch(mode=0o600)
    return shell_status(process.wait())


def serve_playwright(cwd: Path, argv: list[str]) -> None:
    cache = playwright_cache()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (cache / PLAYWRIGHT_LOCK).open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_SH)
        owner = playwright_owner(cache)
        if owner is not None and str(owner) != os.environ.get(TARGETED_CHECK_DIR):
            raise ValueError(f"Playwright ports are retained until retirement and cleanup of {owner}")
        os.chdir(cwd)
        # Keep the lease in the actual foreground service through exec.
        os.set_inheritable(lease.fileno(), True)
        os.execvp(argv[0], argv)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for name in ("run", "stop", "status", "cleanup", "reserve-playwright", "dispatch-playwright", "serve-playwright"):
        subparser = subparsers.add_parser(name)
        if name != "serve-playwright":
            subparser.add_argument("directory", type=Path)
        if name in ("dispatch-playwright", "serve-playwright"):
            subparser.add_argument("cwd", type=Path)
        if name == "dispatch-playwright":
            subparser.add_argument("started", type=Path)
        if name in ("run", "dispatch-playwright", "serve-playwright"):
            subparser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.operation == "serve-playwright":
            if not args.argv:
                parser.error("serve-playwright requires a command")
            serve_playwright(args.cwd, args.argv)
            return 1
        directory = args.directory.resolve()
        if args.operation == "run":
            if not args.argv:
                parser.error("run requires a command after its directory")
            return run_command(directory, args.argv)
        if args.operation == "stop":
            if not directory.is_dir():
                raise ValueError(f"Check control directory is not ready: {directory}")
            with exclusive_lock(directory / "dispatch.lock"):
                (directory / "stop").touch(mode=0o600)
            return 0
        if args.operation in ("reserve-playwright", "dispatch-playwright"):
            if os.environ.get(TARGETED_CHECK_DIR) != str(directory):
                parser.error("Playwright dispatch requires the owning targeted check")
            if args.operation == "reserve-playwright":
                print(reserve_playwright(directory))
                return 0
            if not args.argv:
                parser.error("dispatch-playwright requires a command")
            return dispatch_playwright(directory, args.cwd, args.started, args.argv)
        proof = retirement(directory)
        if proof is None:
            print(f"Check retirement is pending for {directory}", file=sys.stderr)
            return 1
        if args.operation == "cleanup":
            clean_playwright(directory)
            return 0
        print(json.dumps(proof))
        return 0
    except Interrupted as error:
        return 128 + error.signum
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
