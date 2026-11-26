"""Run the real targeted entry points under the candidate's retirement owner."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.check_runner import GateChildren

SOURCE = Path(__file__).resolve().parents[1]
CLI = (sys.executable, "-m", "scripts.targeted_check")


def eventually(condition):
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def proc_exists(pid):
    return Path(f"/proc/{pid}").exists()


@pytest.fixture
def check_fixture(tmp_path):
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SOURCE / "scripts/check.sh", scripts)
    for module in ("check_runner.py", "python_environment.py"):
        shutil.copy2(SOURCE / "scripts" / module, scripts)
    venv_bin = root / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    (root / "web").mkdir()
    env = dict(os.environ, PYTHONPATH=str(SOURCE))
    return root, scripts, env


def write_probe(root, tmp_path):
    probe = root / "probe.py"
    probe.write_text(f"""
import os, signal, subprocess, sys, time
from pathlib import Path
work = Path({str(tmp_path)!r})
if len(sys.argv) == 1:
    subprocess.Popen([sys.executable, __file__, "descendant"], start_new_session=True)
    time.sleep(60)
else:
    def on_term(*_):
        (work / "notified").touch()
        while not (work / "release").exists():
            time.sleep(0.02)
        sys.exit()
    signal.signal(signal.SIGTERM, on_term)
    (work / "ready").write_text(str(os.getpid()))
    time.sleep(60)
""")
    return probe


def test_toolchain_initialization_keeps_retirement_pending_until_orphan_is_reaped(tmp_path, check_fixture):
    root, scripts, env = check_fixture
    probe = write_probe(root, tmp_path)
    (scripts / "toolchain-env.sh").write_text(f'.venv/bin/python "{probe}"\n')
    state = tmp_path / "run"
    ready, notified, release = (tmp_path / name for name in ("ready", "notified", "release"))
    process = subprocess.Popen(
        [*CLI, "run", str(state), "--", "./scripts/check.sh", "--gate", "ruff"], cwd=root, env=env,
    )
    try:
        eventually(lambda: ready.exists() or process.poll() is not None)
        assert ready.exists(), "the real check.sh did not reach toolchain initialization"
        descendant = int(ready.read_text())
        runner = json.loads((state / "launch.json").read_text())["runner_pid"]
        subprocess.run([*CLI, "stop", str(state)], cwd=root, env=env, check=True)
        eventually(lambda: notified.exists() and not proc_exists(runner))
        assert proc_exists(descendant)
        pending = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True)
        assert pending.returncode == 1
        assert not (state / "retirement.json").exists()
        release.touch()
        assert process.wait(timeout=10) == 128 + signal.SIGTERM
        proven = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True, check=True)
        assert json.loads(proven.stdout)["runner_pid"] == runner
        assert not proc_exists(descendant)
    finally:
        release.touch()
        if process.poll() is None:
            subprocess.run([*CLI, "stop", str(state)], cwd=root, env=env)
            process.wait(timeout=15)


def install_command(path, body):
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o700)


@pytest.mark.parametrize("phase", ["inputs-before", "gate", "dispatch", "inputs-after", "complete"])
def test_real_check_preserves_later_gate_and_input_boundaries(tmp_path, check_fixture, phase):
    root, scripts, env = check_fixture
    probe = write_probe(root, tmp_path)
    binary = root / ".venv/bin"
    (scripts / "toolchain-env.sh").write_text(f'export PATH="{binary}:$PATH"\n')
    later = tmp_path / "later-gate-started"
    (scripts / "gen-docs-index.py").write_text(f"from pathlib import Path\nPath({str(later)!r}).touch()\n")
    git = shutil.which("git")
    subprocess.run([git, "init", "-q"], cwd=root, check=True)
    install_command(binary / "git", f"""
import os, sys
from pathlib import Path
if sys.argv[1:2] == ["ls-files"] and (
    {phase!r} == "inputs-before" or ({phase!r} == "inputs-after" and Path({str(later)!r}).exists())
):
    os.execv(sys.executable, [sys.executable, {str(probe)!r}])
os.execv({git!r}, [{git!r}, *sys.argv[1:]])
""")
    install_command(binary / "ruff", f"""
import os, sys
import time
from pathlib import Path
if {phase!r} == "gate":
    os.execv(sys.executable, [sys.executable, {str(probe)!r}])
if {phase!r} == "dispatch":
    Path({str(tmp_path / "ready")!r}).write_text(str(os.getpid()))
    while not Path({str(tmp_path / "release")!r}).exists():
        time.sleep(0.02)
""")
    # Source evidence requires a HEAD even in this small fixture repository.
    subprocess.run(
        [git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "fixture"], cwd=root, check=True,
    )
    state, output, ready, release = (tmp_path / name for name in ("run", "evidence", "ready", "release"))
    process = subprocess.Popen(
        [*CLI, "run", str(state), "--", "./scripts/check.sh", "--gate", "ruff", "--gate", "docs-index",
         "--output", str(output)], cwd=root, env=env,
    )
    try:
        if phase == "complete":
            assert process.wait(timeout=15) == 0
        else:
            eventually(lambda: ready.exists() or process.poll() is not None)
            assert ready.exists(), f"did not reach {phase}"
            descendant = int(ready.read_text())
            assert proc_exists(descendant)
            if phase == "dispatch":
                # Keep the owner from delivering TERM until the current
                # gate has returned. The next gate must see the stop request.
                process.send_signal(signal.SIGSTOP)
            subprocess.run([*CLI, "stop", str(state)], cwd=root, env=env, check=True)
            release.touch()
            if phase == "dispatch":
                eventually(lambda: json.loads((output / "results.json").read_text())["state"] == "interrupted")
                assert not later.exists() and not (state / "retirement.json").exists()
                process.send_signal(signal.SIGCONT)
            assert process.wait(timeout=15) == 128 + signal.SIGTERM
            assert not proc_exists(descendant)
        proof = json.loads((state / "retirement.json").read_text())
        assert not proc_exists(proof["runner_pid"])
        report = json.loads((output / "results.json").read_text())
        assert report["state"] == ("complete" if phase == "complete" else "interrupted")
        assert later.exists() == (phase in {"inputs-after", "complete"})
    finally:
        release.touch()
        if process.poll() is None:
            process.send_signal(signal.SIGCONT)
            subprocess.run([*CLI, "stop", str(state)], cwd=root, env=env)
            process.wait(timeout=15)


def test_killing_outer_owner_leaves_original_resources_and_missing_proof(tmp_path, check_fixture):
    root, scripts, env = check_fixture
    probe = write_probe(root, tmp_path)
    (scripts / "toolchain-env.sh").write_text(f'.venv/bin/python "{probe}"\n')
    state = tmp_path / "run"
    ready, release = (tmp_path / name for name in ("ready", "release"))
    # The test remains a subreaper so its deliberately broken execution
    # cannot leave children behind after the assertion.
    with GateChildren(term_grace=0.1) as cleanup:
        process = cleanup.spawn(
            [*CLI, "run", str(state), "--", "./scripts/check.sh", "--gate", "ruff"], cwd=root, env=env,
        )
        try:
            eventually(lambda: ready.exists() or process.poll() is not None)
            assert ready.exists()
            runner = json.loads((state / "launch.json").read_text())["runner_pid"]
            descendant = int(ready.read_text())
            process.kill()
            assert process.wait(timeout=10) == -signal.SIGKILL
            assert proc_exists(runner) and proc_exists(descendant)
            pending = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True)
            assert pending.returncode == 1
            assert probe.exists() and (state / "launch.json").exists()
        finally:
            release.touch()
    assert not (state / "retirement.json").exists()
    assert not proc_exists(runner) and not proc_exists(descendant)


def test_status_keeps_exit_proof_readable_when_cleanup_cannot_validate_reference(tmp_path, check_fixture):
    root, _, env = check_fixture
    state = tmp_path / "run"
    command = [*CLI, "run", str(state), "--", sys.executable, "-c", "pass"]
    subprocess.run(command, cwd=root, env=env, check=True)
    reference = state / "playwright-runtime.json"
    reference.write_text(json.dumps(str(root)))
    cleanup = subprocess.run([*CLI, "cleanup", str(state)], cwd=root, env=env, capture_output=True, text=True)
    assert cleanup.returncode == 1 and "Invalid targeted Playwright runtime reference" in cleanup.stderr
    status = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True, check=True)
    assert json.loads(status.stdout)["returncode"] == 0
    assert root.is_dir() and reference.is_file()


def test_stop_before_control_directory_is_ready_can_be_retried(tmp_path, check_fixture):
    root, _, env = check_fixture
    state = tmp_path / "run"
    stop = [*CLI, "stop", str(state)]
    request = subprocess.run(stop, cwd=root, env=env, capture_output=True, text=True)
    assert request.returncode == 1 and "control directory is not ready" in request.stderr
    assert not state.exists()
    state.mkdir()
    subprocess.run(stop, cwd=root, env=env, check=True)
    assert (state / "stop").is_file()


def test_retired_playwright_cleanup_does_not_wait_for_an_unrelated_service(tmp_path, check_fixture):
    root, _, env = check_fixture
    state = tmp_path / "run"
    isolated = tmp_path / "ports"
    paused, resume, serving, release = (tmp_path / name for name in ("paused", "resume", "serving", "release"))
    preload = tmp_path / "preload"
    preload.mkdir()
    (preload / "sitecustomize.py").write_text(f"""
import pwd, sys, time
from pathlib import Path
from types import SimpleNamespace
pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir={str(isolated)!r})
def pause_cleanup(frame, event, value):
    if event == "call" and frame.f_code.co_name == "clean_playwright":
        Path({str(paused)!r}).touch()
        while not Path({str(resume)!r}).exists():
            time.sleep(0.02)
    return pause_cleanup
if sys.orig_argv[1:4] == ["-m", "scripts.targeted_check", "run"]:
    sys.settrace(pause_cleanup)
""")
    env["PYTHONPATH"] = os.pathsep.join((str(preload), env["PYTHONPATH"]))
    main = subprocess.Popen(
        [*CLI, "run", str(state), "--", *CLI, "reserve-playwright", str(state)], cwd=root, env=env,
        stdout=subprocess.DEVNULL,
    )
    ordinary = None
    cleanup = [*CLI, "cleanup", str(state)]
    try:
        eventually(lambda: paused.exists() or main.poll() is not None)
        assert paused.exists() and (state / "retirement.json").is_file()
        runtime = Path(json.loads((state / "playwright-runtime.json").read_text()))
        assert runtime.is_dir()
        cache = isolated / ".cache/octopus-tests"
        with (cache / "targeted-playwright.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_SH)
            pending = subprocess.run(cleanup, cwd=root, env=env, timeout=3, capture_output=True, text=True)
            assert pending.returncode == 1 and "retry cleanup" in pending.stderr
            assert runtime.is_dir()
        subprocess.run(cleanup, cwd=root, env=env, check=True, timeout=3)
        assert not runtime.exists() and not (cache / "targeted-playwright-owner.json").exists()
        program = (
            "from pathlib import Path; import time\n"
            f"Path({str(serving)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n"
        )
        ordinary = subprocess.Popen(
            [*CLI, "serve-playwright", str(root), "--", sys.executable, "-c", program],
            cwd=root, env=env,
        )
        eventually(lambda: serving.exists() or ordinary.poll() is not None)
        assert serving.exists()
        resume.touch()
        assert main.wait(timeout=3) == 0
        subprocess.run(cleanup, cwd=root, env=env, check=True, timeout=3)
        assert ordinary.poll() is None
    finally:
        release.touch()
        resume.touch()
        if ordinary is not None:
            ordinary.wait(timeout=5)
        main.wait(timeout=5)
