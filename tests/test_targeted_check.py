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
    (work / "ready.tmp").write_text(str(os.getpid()))
    (work / "ready.tmp").replace(work / "ready")
    time.sleep(60)
""")
    return probe


@pytest.mark.parametrize("signum", [None, signal.SIGINT, signal.SIGTERM], ids=["soft-stop", "SIGINT", "SIGTERM"])
def test_toolchain_initialization_keeps_retirement_pending_until_orphan_is_reaped(
    tmp_path, isolated_playwright, playwright_toolchain, signum,
):
    root, env, _, cache = isolated_playwright
    state, _ = playwright_toolchain
    ready, notified, release = (tmp_path / name for name in ("ready", "notified", "release"))
    process = subprocess.Popen(
        [*CLI, "run", str(state), "--", "./scripts/check.sh", "--gate", "ruff"], cwd=root, env=env,
    )
    try:
        launched = state / "launch.json"
        eventually(lambda: (ready.exists() and launched.exists()) or process.poll() is not None)
        assert ready.exists() and launched.exists(), "the real check.sh did not reach toolchain initialization"
        descendant = int(ready.read_text())
        runner = json.loads(launched.read_text())["runner_pid"]
        if signum is None:
            subprocess.run([*CLI, "stop", str(state)], cwd=root, env=env, check=True)
        else:
            process.send_signal(signum)
        eventually(lambda: notified.exists() and not proc_exists(runner))
        assert proc_exists(descendant)
        pending = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True)
        assert pending.returncode == 1
        assert not (state / "retirement.json").exists()
        assert json.loads((cache / "targeted-playwright-owner.json").read_text()) == str(state)
        if signum is not None:
            process.send_signal(signal.SIGTERM if signum == signal.SIGINT else signal.SIGINT)
        release.touch()
        assert process.wait(timeout=10) == 128 + (signum or signal.SIGTERM)
        proven = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True, check=True)
        assert json.loads(proven.stdout)["runner_pid"] == runner
        assert not proc_exists(descendant)
        assert not (cache / "targeted-playwright-owner.json").exists()
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


@pytest.fixture
def isolated_playwright(tmp_path, check_fixture):
    root, _, env = check_fixture
    home = tmp_path / "ports"
    preload = tmp_path / "preload"
    preload.mkdir()
    (preload / "sitecustomize.py").write_text(f"""
import pwd
from types import SimpleNamespace
pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir={str(home)!r})
""")
    env["PYTHONPATH"] = os.pathsep.join((str(preload), env["PYTHONPATH"]))
    return root, env, preload, home / ".cache/octopus-tests"


def inject_targeted_run(preload, code):
    (preload / "targeted_fault.py").write_text("from scripts import check_runner\n" + code)
    with (preload / "sitecustomize.py").open("a") as site:
        site.write(f"""
import sys
if sys.orig_argv[1:5] == ["-m", "scripts.targeted_check", "run", {str(preload.parent / "run")!r}]:
    import targeted_fault
""")


def pause_targeted_run(tmp_path, preload, hook):
    paused, resume = (tmp_path / name for name in ("paused", "resume"))
    inject_targeted_run(preload, f"""
from pathlib import Path
import time

def pause():
    Path({str(paused)!r}).touch()
    while not Path({str(resume)!r}).exists():
        time.sleep(0.02)

{hook}
""")
    return paused, resume


def pause_playwright_cleanup(tmp_path, preload):
    return pause_targeted_run(tmp_path, preload, """
import sys
def trace(frame, event, value):
    if event == "call" and frame.f_code.co_name == "clean_playwright":
        pause()
sys.settrace(trace)
""")


@pytest.fixture
def playwright_toolchain(tmp_path, isolated_playwright):
    root, _, _, _ = isolated_playwright
    state = tmp_path / "run"
    probe = write_probe(root, tmp_path)
    (root / "scripts/toolchain-env.sh").write_text(
        f'.venv/bin/python -m scripts.targeted_check reserve-playwright "{state}" || exit $?\n'
        f'.venv/bin/python "{probe}"\n'
    )
    return state, probe


@pytest.fixture
def playwright_probe(tmp_path, isolated_playwright):
    root, _, _, _ = isolated_playwright
    state = tmp_path / "run"
    ready = tmp_path / "ready"
    child = root / "child.py"
    child.write_text(f"""
import os, signal, subprocess, sys, time
from pathlib import Path
subprocess.run({[*CLI, "reserve-playwright", str(state)]!r}, check=True, stdout=subprocess.DEVNULL)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(37))
ready = Path({str(ready)!r})
temporary = ready.with_suffix(".tmp")
temporary.write_text(str(os.getpid()))
temporary.replace(ready)
while not Path({str(tmp_path / "finish")!r}).exists():
    time.sleep(0.02)
""")
    return state, ready, [*CLI, "run", str(state), "--", sys.executable, str(child)]


def reserve_in_another_run(directory, root, env):
    return subprocess.run(
        [*CLI, "run", str(directory), "--", *CLI, "reserve-playwright", str(directory)],
        cwd=root, env=env, capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("phase", ["launch-record", "late-handle", "launch-record-error", "unknown-launch", "unknown-reap"])
def test_signal_at_launch_and_retirement_boundaries(tmp_path, isolated_playwright, playwright_probe, signum, phase):
    root, env, preload, cache = isolated_playwright
    state, ready, command = playwright_probe
    if phase in {"launch-record", "launch-record-error"}:
        hook = f"""
atomic_json = check_runner.atomic_json
def write(path, value):
    if path.name == "launch.json":
        pause()
        if {phase == "launch-record-error"!r}:
            raise OSError("injected launch record failure")
    atomic_json(path, value)
check_runner.atomic_json = write
"""
    elif phase in {"late-handle", "unknown-launch"}:
        hook = f"""
popen = check_runner.subprocess.Popen
def spawn(*args, **kwargs):
    process = popen(*args, **kwargs)
    pause()
    if {phase == "unknown-launch"!r}:
        raise OSError("injected unknown launch result")
    return process
check_runner.subprocess.Popen = spawn
"""
    else:
        hook = """
def fully_reaped(self):
    raise OSError("injected unconfirmed retirement")
check_runner.GateChildren.fully_reaped = fully_reaped
"""
    if phase == "unknown-reap":
        inject_targeted_run(preload, hook)
        paused = state / "launch.json"
        resume = None
    else:
        paused, resume = pause_targeted_run(tmp_path, preload, hook)

    # Adopt the deliberately abandoned child in the unconfirmed-reap case.
    with (tmp_path / "stderr").open("w+") as output, GateChildren(term_grace=0.1) as cleanup:
        process = cleanup.spawn(command, cwd=root, env=env, stderr=output)
        try:
            eventually(lambda: (paused.exists() and ready.exists()) or process.poll() is not None)
            assert paused.exists() and ready.exists()
            assert (state / "launch.json").exists() == (phase == "unknown-reap")
            runner = int(ready.read_text())
            process.send_signal(signum)
            if resume is not None:
                resume.touch()
            process.wait(timeout=15)
            output.seek(0)
            stderr = output.read()
            status = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True, text=True)
            runtime = Path(json.loads((state / "playwright-runtime.json").read_text()))
            following = reserve_in_another_run(tmp_path / "following", root, env)
            if phase.startswith("unknown"):
                assert process.returncode == 1 and "injected" in stderr
                assert status.returncode == 1 and "pending" in status.stderr
                assert not (state / "retirement.json").exists()
                assert proc_exists(runner) == (phase == "unknown-reap")
                assert json.loads((cache / "targeted-playwright-owner.json").read_text()) == str(state)
                assert runtime.is_dir() and following.returncode == 1
                assert "ports are retained" in following.stderr
            else:
                assert process.returncode == 128 + signum, stderr
                assert not proc_exists(runner)
                assert status.returncode == 0, status.stderr
                proof = json.loads(status.stdout)
                assert (proof["runner_pid"], proof["returncode"], proof["stop_requested"]) == (runner, 37, True)
                assert not runtime.exists() and following.returncode == 0
                if phase == "launch-record-error":
                    assert "injected launch record failure" in stderr
        finally:
            if resume is not None:
                resume.touch()
            (tmp_path / "finish").touch()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_stop_file_check_records_no_child(tmp_path, isolated_playwright, playwright_probe, signum):
    root, env, preload, _ = isolated_playwright
    state, ready, command = playwright_probe
    paused, resume = pause_targeted_run(tmp_path, preload, f"""
exists = Path.exists
def pause_stop_check(path, *args, **kwargs):
    if path == Path({str(state / "stop")!r}):
        pause()
    return exists(path, *args, **kwargs)
Path.exists = pause_stop_check
""")
    process = subprocess.Popen(command, cwd=root, env=env)
    try:
        eventually(lambda: paused.exists() or process.poll() is not None)
        assert paused.exists()
        process.send_signal(signum)
        resume.touch()
        assert process.wait(timeout=10) == 128 + signum
        assert not ready.exists() and not (state / "launch.json").exists()
        status = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True, text=True, check=True)
        proof = json.loads(status.stdout)
        assert (proof["runner_pid"], proof["returncode"], proof["stop_requested"]) == (None, None, True)
    finally:
        resume.touch()
        (tmp_path / "finish").touch()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=20)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("phase", ["waiting", "cleanup"])
def test_signal_keeps_exit_proof_when_playwright_cleanup_is_busy(
    tmp_path, isolated_playwright, playwright_probe, signum, phase,
):
    root, env, preload, cache = isolated_playwright
    state, ready, command = playwright_probe
    paused, resume = pause_playwright_cleanup(tmp_path, preload)
    process = subprocess.Popen(command, cwd=root, env=env, stderr=subprocess.PIPE, text=True)
    try:
        eventually(lambda: ready.exists() or process.poll() is not None)
        assert ready.exists()
        runner = int(ready.read_text())
        runtime = Path(json.loads((state / "playwright-runtime.json").read_text()))
        with (cache / "targeted-playwright.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_SH)
            if phase == "waiting":
                process.send_signal(signum)
            else:
                (tmp_path / "finish").touch()
            eventually(lambda: paused.exists() or process.poll() is not None)
            assert paused.exists() and not proc_exists(runner)
            if phase != "waiting":
                process.send_signal(signum)
            resume.touch()
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 128 + signum and "cleanup pending" in stderr
        status = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True, text=True, check=True)
        proof = json.loads(status.stdout)
        assert proof["runner_pid"] == runner
        assert proof["returncode"] == (37 if phase == "waiting" else 0)
        assert proof["stop_requested"] == (phase == "waiting")
        assert runtime.is_dir()
        assert json.loads((cache / "targeted-playwright-owner.json").read_text()) == str(state)

        other = tmp_path / "other"
        blocked = reserve_in_another_run(other, root, env)
        assert blocked.returncode == 1 and "ports are retained" in blocked.stderr
        (other / "playwright-runtime.json").write_text(json.dumps(str(runtime)))
        wrong_owner = subprocess.run(
            [*CLI, "cleanup", str(other)], cwd=root, env=env, capture_output=True, text=True,
        )
        assert wrong_owner.returncode == 1 and "Invalid targeted Playwright runtime reference" in wrong_owner.stderr
        assert runtime.is_dir()
        assert json.loads((cache / "targeted-playwright-owner.json").read_text()) == str(state)

        subprocess.run([*CLI, "cleanup", str(state)], cwd=root, env=env, check=True)
        assert not runtime.exists() and not (cache / "targeted-playwright-owner.json").exists()
        assert reserve_in_another_run(tmp_path / "following", root, env).returncode == 0
    finally:
        resume.touch()
        (tmp_path / "finish").touch()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=20)


@pytest.mark.parametrize("retirement_confirmed", [True, False], ids=["confirmed", "unknown"])
def test_second_signal_at_cli_exit_preserves_result(tmp_path, isolated_playwright, playwright_probe, retirement_confirmed):
    root, env, preload, cache = isolated_playwright
    state, ready, command = playwright_probe
    hook = """
import sys
exit_cli = sys.exit
def pause_cli_exit(status):
    pause()
    exit_cli(status)
sys.exit = pause_cli_exit
"""
    if not retirement_confirmed:
        hook += """
def fully_reaped(self):
    raise OSError("injected unconfirmed retirement")
check_runner.GateChildren.fully_reaped = fully_reaped
"""
    paused, resume = pause_targeted_run(tmp_path, preload, hook)
    with GateChildren(term_grace=0.1) as cleanup:
        process = cleanup.spawn(command, cwd=root, env=env, stderr=subprocess.DEVNULL)
        try:
            eventually(lambda: ready.exists() or process.poll() is not None)
            assert ready.exists()
            runner = int(ready.read_text())
            process.send_signal(signal.SIGINT)
            eventually(lambda: paused.exists() or process.poll() is not None)
            assert paused.exists()
            process.send_signal(signal.SIGTERM)
            resume.touch()
            assert process.wait(timeout=10) == (130 if retirement_confirmed else 1)
            status = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True, text=True)
            owner_file = cache / "targeted-playwright-owner.json"
            if retirement_confirmed:
                assert not proc_exists(runner) and not owner_file.exists()
                assert status.returncode == 0, status.stderr
                proof = json.loads(status.stdout)
                assert (proof["runner_pid"], proof["returncode"], proof["stop_requested"]) == (runner, 37, True)
            else:
                assert proc_exists(runner)
                assert status.returncode == 1 and "pending" in status.stderr
                assert not (state / "retirement.json").exists()
                assert json.loads(owner_file.read_text()) == str(state)
                blocked = reserve_in_another_run(tmp_path / "following", root, env)
                assert blocked.returncode == 1 and "ports are retained" in blocked.stderr
        finally:
            resume.touch()
            (tmp_path / "finish").touch()


def test_cleanup_requires_durable_exit_proof_after_rename(tmp_path, isolated_playwright, playwright_probe):
    root, env, preload, cache = isolated_playwright
    state, ready, command = playwright_probe
    storage_restored = tmp_path / "storage-restored"
    # Both independent readers and the writer see the control-directory sync fault.
    # The writer pauses after rename so cleanup can also attempt to sync the proof.
    with (preload / "sitecustomize.py").open("a") as site:
        site.write(f"""
import sys, time
from pathlib import Path
from scripts import check_runner
sync_directory = check_runner.sync_directory
def sync(path):
    if path == Path({str(state)!r}) and (path / "retirement.json").is_file():
        if sys.orig_argv[3] == "run":
            Path({str(tmp_path / "paused")!r}).touch()
            while not Path({str(tmp_path / "resume")!r}).exists():
                time.sleep(0.02)
        if not Path({str(storage_restored)!r}).exists():
            raise OSError("injected retirement directory sync failure")
    sync_directory(path)
check_runner.sync_directory = sync
""")
    process = subprocess.Popen(command, cwd=root, env=env, stderr=subprocess.PIPE, text=True)
    try:
        eventually(lambda: ready.exists() or process.poll() is not None)
        assert ready.exists()
        process.send_signal(signal.SIGINT)
        eventually(lambda: (tmp_path / "paused").exists() or process.poll() is not None)
        assert (tmp_path / "paused").exists() and (state / "retirement.json").exists()
        cleanup = [*CLI, "cleanup", str(state)]
        failed = subprocess.run(cleanup, cwd=root, env=env, capture_output=True, text=True, timeout=10)
        assert failed.returncode == 1 and "directory sync failure" in failed.stderr
        (tmp_path / "resume").touch()
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 1 and "directory sync failure" in stderr
        assert json.loads((cache / "targeted-playwright-owner.json").read_text()) == str(state)
        storage_restored.touch()
        subprocess.run(cleanup, cwd=root, env=env, check=True)
        assert reserve_in_another_run(tmp_path / "following", root, env).returncode == 0
    finally:
        (tmp_path / "resume").touch()
        (tmp_path / "finish").touch()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=20)


def test_killing_outer_owner_leaves_original_resources_and_missing_proof(
    tmp_path, isolated_playwright, playwright_toolchain,
):
    root, env, _, cache = isolated_playwright
    state, probe = playwright_toolchain
    ready, release = (tmp_path / name for name in ("ready", "release"))
    # The test remains a subreaper so its deliberately broken execution
    # cannot leave children behind after the assertion.
    with GateChildren(term_grace=0.1) as cleanup:
        process = cleanup.spawn(
            [*CLI, "run", str(state), "--", "./scripts/check.sh", "--gate", "ruff"], cwd=root, env=env,
        )
        try:
            launched = state / "launch.json"
            eventually(lambda: (ready.exists() and launched.exists()) or process.poll() is not None)
            assert ready.exists() and launched.exists()
            runner = json.loads(launched.read_text())["runner_pid"]
            descendant = int(ready.read_text())
            process.kill()
            assert process.wait(timeout=10) == -signal.SIGKILL
            assert proc_exists(runner) and proc_exists(descendant)
            pending = subprocess.run([*CLI, "status", str(state)], cwd=root, env=env, capture_output=True)
            assert pending.returncode == 1
            assert json.loads((cache / "targeted-playwright-owner.json").read_text()) == str(state)
            blocked = reserve_in_another_run(tmp_path / "following", root, env)
            assert blocked.returncode == 1 and "ports are retained" in blocked.stderr
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


def test_retired_playwright_cleanup_does_not_wait_for_an_unrelated_service(tmp_path, isolated_playwright):
    root, env, preload, cache = isolated_playwright
    state = tmp_path / "run"
    paused, resume = pause_playwright_cleanup(tmp_path, preload)
    serving, release = (tmp_path / name for name in ("serving", "release"))
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
