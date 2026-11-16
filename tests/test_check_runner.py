"""Verification evidence must describe the execution that actually happened."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import check_runner as runner


@pytest.fixture
def fixed_inputs(monkeypatch):
    monkeypatch.setattr(runner, "capture_inputs", lambda root: {"source": "fixed", "environment": "fixed"})


def gate(root, identifier, program, *, report=None):
    return runner.Gate(identifier, identifier, (sys.executable, "-c", program), root, "isolated regression", report)


def test_failure_is_preserved_and_later_gate_runs_with_its_own_cwd(tmp_path, fixed_inputs):
    other = tmp_path / "other"
    other.mkdir()
    output = tmp_path / "evidence"
    gates = [gate(tmp_path, "bad", "import sys; print('before failure'); sys.exit(7)"),
             gate(other, "good", "import os; print(os.getcwd())")]
    assert runner.run_checks(tmp_path, gates, output) == 1
    report = json.loads((output / "results.json").read_text())
    assert report["state"] == "complete" and not report["success"]
    assert [row["returncode"] for row in report["gates"]] == [7, 0]
    assert [row["status"] for row in report["gates"]] == ["failed", "passed"]
    assert report["gates"][0]["argv"] == list(gates[0].argv)
    assert report["gates"][1]["cwd"] == str(other)
    assert report["gates"][0]["counts"] is None
    assert "before failure" in (output / "bad.log").read_text()
    assert str(other) in (output / "good.log").read_text()


def test_existing_evidence_is_never_overwritten_or_executed(tmp_path, fixed_inputs):
    output = tmp_path / "evidence"
    output.mkdir()
    original = output / "results.json"
    original.write_text("original proof")
    marker = tmp_path / "executed"
    with pytest.raises(FileExistsError):
        runner.run_checks(tmp_path, [gate(tmp_path, "bad", f"open({str(marker)!r}, 'w').close()")], output)
    assert original.read_text() == "original proof" and not marker.exists()


@pytest.mark.parametrize("save_evidence", [False, True])
def test_missing_tool_is_a_failed_gate_and_later_checks_still_run(tmp_path, fixed_inputs, save_evidence):
    output = tmp_path / "evidence" if save_evidence else None
    marker = tmp_path / "executed"
    gates = [runner.Gate("missing", "missing", (str(tmp_path / "missing-tool"),), tmp_path, "test"),
             gate(tmp_path, "later", f"open({str(marker)!r}, 'w').close()")]
    assert runner.run_checks(tmp_path, gates, output) == 1 and marker.exists()
    if output:
        row = json.loads((output / "results.json").read_text())["gates"][0]
        assert row["returncode"] is None and row["status"] == "failed" and row["execution_error"]


def test_result_write_failure_leaves_incomplete_evidence(tmp_path, fixed_inputs, monkeypatch):
    original = runner.atomic_json

    def fail_complete(path, report):
        if report["state"] == "complete":
            raise OSError("result storage unavailable")
        return original(path, report)

    monkeypatch.setattr(runner, "atomic_json", fail_complete)
    output = tmp_path / "evidence"
    assert runner.run_checks(tmp_path, [gate(tmp_path, "good", "print('pass')")], output) == 1
    report = json.loads((output / "results.json").read_text())
    assert report["state"] == "incomplete" and not report["success"]


@pytest.mark.parametrize("kind", ["pytest", "vitest"])
def test_native_report_counts_are_from_same_failed_execution(tmp_path, fixed_inputs, kind):
    if kind == "pytest":
        native = '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1"/></testsuites>'
        program = "import sys; from pathlib import Path; p=sys.argv[-1].split('=', 1)[1]; "
    else:
        native = json.dumps({"numTotalTests": 3, "numPassedTests": 1, "numFailedTests": 1, "numPendingTests": 1, "numTodoTests": 0})
        program = "import sys; from pathlib import Path; p=sys.argv[-1].split('=', 1)[1]; "
    program += f"Path(p).write_text({native!r}); print('real execution'); sys.exit(9)"
    output = tmp_path / "evidence"
    assert runner.run_checks(tmp_path, [gate(tmp_path, "tests", program, report=kind)], output) == 1
    row = json.loads((output / "results.json").read_text())["gates"][0]
    assert row["returncode"] == 9 and row["counts"]["total"] == 3
    assert row["counts"]["failed"] == 1 and row["counts"]["skipped"] == 1
    assert row["status"] == "failed" and row["evidence_error"] is None


@pytest.mark.parametrize("content", [None, "broken report"])
def test_missing_or_malformed_success_report_is_evidence_failure(tmp_path, fixed_inputs, content):
    program = "print('success without proof')" if content is None else (
        f"import sys; from pathlib import Path; Path(sys.argv[-1].split('=', 1)[1]).write_text({content!r})")
    output = tmp_path / "evidence"
    assert runner.run_checks(tmp_path, [gate(tmp_path, "tests", program, report="pytest")], output) == 1
    report = json.loads((output / "results.json").read_text())
    row = report["gates"][0]
    assert row["returncode"] == 0 and row["counts"] is None
    assert row["evidence_error"] and not report["success"]


def test_log_creation_failure_does_not_run_a_gate_or_publish_success(tmp_path, fixed_inputs, monkeypatch):
    original = Path.open

    def fail_log(path, *args, **kwargs):
        if path.suffix == ".log":
            raise OSError("log storage unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log)
    output = tmp_path / "evidence"
    marker = tmp_path / "executed"
    assert runner.run_checks(tmp_path, [gate(tmp_path, "bad", f"open({str(marker)!r}, 'w').close()")], output) == 1
    report = json.loads((output / "results.json").read_text())
    assert report["state"] == "incomplete" and not report["success"]
    assert not marker.exists()


def test_real_log_short_write_cannot_be_reported_as_complete(tmp_path):
    root = Path(__file__).resolve().parents[1]
    program = f'''
import json,resource,signal,sys
from pathlib import Path
from scripts.check_runner import Gate,run_gate
output=Path({str(tmp_path)!r})
row={{}}
old=resource.getrlimit(resource.RLIMIT_FSIZE)
signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE,(512,old[1]))
caught=False
try:
    run_gate(Gate('short','short',(sys.executable,'-c',"print('x'*4096)"),output,'test',quiet=True),output,row)
except OSError:
    caught=True
finally:
    resource.setrlimit(resource.RLIMIT_FSIZE,old)
print(json.dumps({{'caught':caught,'bytes':(output/'short.log').stat().st_size,'returncode':row.get('returncode')}}))
'''
    result = subprocess.run([sys.executable, "-c", program], cwd=root, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["bytes"] == 512 and observed["caught"], observed


def test_selection_environment_changes_verification_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "inspect_environment", lambda python: {"packages": {}})
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    before = runner.environment_identity(tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k selected")
    after = runner.environment_identity(tmp_path)
    assert before != after
    assert after["variables"]["PYTEST_ADDOPTS"] == "-k selected"


def test_environment_filtered_pytest_is_recorded_and_cannot_certify_full_gate(tmp_path, fixed_inputs, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k selected")
    source = tmp_path / "test_scope.py"
    source.write_text("def test_selected(): pass\ndef test_other(): pass\n")
    output = tmp_path / "evidence"
    selection = runner.Gate("pytest", "full pytest", (sys.executable, "-m", "pytest", str(source), "-q"), tmp_path, "complete suite", "pytest")
    assert runner.run_checks(tmp_path, [selection], output) == 1
    report = json.loads((output / "results.json").read_text())
    row = report["gates"][0]
    assert row["returncode"] == 0 and row["counts"]["total"] == 1
    assert row["selection_environment"]["PYTEST_ADDOPTS"] == "-k selected"
    assert row["evidence_error"] and not report["success"]


def test_source_identity_includes_untracked_content_modes_links_and_deletions(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "source.py"
    path.write_text("original")
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    original = runner.source_identity(tmp_path)
    path.write_text("changed")
    changed = runner.source_identity(tmp_path)
    assert original != changed and changed["dirty"]
    untracked = tmp_path / "new.py"
    untracked.write_text("one")
    one = runner.source_identity(tmp_path)
    untracked.write_text("two")
    two = runner.source_identity(tmp_path)
    assert one != two
    untracked.chmod(0o755)
    executable = runner.source_identity(tmp_path)
    assert executable != two
    link = tmp_path / "link"
    link.symlink_to("new.py")
    before_link = runner.source_identity(tmp_path)
    link.unlink()
    link.symlink_to("source.py")
    assert runner.source_identity(tmp_path) != before_link
    path.unlink()
    assert runner.source_identity(tmp_path) != executable


def test_changed_inputs_make_otherwise_passing_evidence_unusable(tmp_path, monkeypatch):
    identities = iter([{"source": "before"}, {"source": "after"}])
    monkeypatch.setattr(runner, "capture_inputs", lambda root: next(identities))
    output = tmp_path / "evidence"
    assert runner.run_checks(tmp_path, [gate(tmp_path, "good", "print('pass')")], output) == 1
    report = json.loads((output / "results.json").read_text())
    assert report["gates"][0]["status"] == "passed"
    assert report["inputs_changed"] and not report["success"]


@pytest.mark.parametrize("parent_exits", [False, True], ids=["live-parent", "exited-parent"])
def test_cancellation_stops_descendants_and_does_not_start_remaining_gate(tmp_path, parent_exits):
    root = Path(__file__).resolve().parents[1]
    output, ready, later = (tmp_path / name for name in ("evidence", "ready", "later"))
    descendant = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)")
    child = (f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
             + ("" if parent_exits else "time.sleep(60)"))
    program = (
        "from pathlib import Path; import sys; from scripts import check_runner as r; "
        "r.capture_inputs=lambda root: {}; "
        f"g=[r.Gate('waiting','waiting',(sys.executable,'-c',{child!r}),Path({str(tmp_path)!r}),'test'),"
        f"r.Gate('later','later',(sys.executable,'-c',\"open({str(later)!r}, 'w').close()\"),Path({str(tmp_path)!r}),'test')]; "
        f"sys.exit(r.run_checks(Path({str(tmp_path)!r}),g,Path({str(output)!r})))"
    )
    process = subprocess.Popen([sys.executable, "-c", program], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        child_pid = int(ready.read_text())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
        assert process.returncode == 128 + signal.SIGTERM
        assert not Path(f"/proc/{child_pid}").exists(), "gate descendant was not reaped"
        report = json.loads((output / "results.json").read_text())
        assert report["state"] == "interrupted" and not report["success"]
        assert report["gates"][0]["status"] == "interrupted" and not later.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        if ready.exists():
            try:
                os.kill(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("original", [0, 1])
@pytest.mark.parametrize("save_evidence", [False, True])
def test_parent_exit_retires_session_descendant_and_restores_subreaper(tmp_path, original, save_evidence):
    root = Path(__file__).resolve().parents[1]
    ready = tmp_path / "ready"
    descendant = ("import os,time; from pathlib import Path; "
                  f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)")
    child = ("import subprocess,sys,time; from pathlib import Path; "
             f"subprocess.Popen([sys.executable,'-c',{descendant!r}],start_new_session=True)\n"
             f"while not Path({str(ready)!r}).exists(): time.sleep(.01)\n"
             "sys.exit(7)")
    output = f"Path({str(tmp_path / 'evidence')!r})" if save_evidence else "None"
    program = (
        "import json,sys; from pathlib import Path; from scripts import check_runner as r; "
        f"r.subreaper({original}); r.capture_inputs=lambda root: {{}}; "
        f"g=r.Gate('child','child',(sys.executable,'-c',{child!r}),Path({str(tmp_path)!r}),'test'); "
        f"code=r.run_checks(Path({str(tmp_path)!r}),[g],{output}); "
        f"assert code==1; assert r.subreaper()=={original}; assert not r.children()"
    )
    result = subprocess.run([sys.executable, "-c", program], cwd=root, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not Path(f"/proc/{int(ready.read_text())}").exists()
    if save_evidence:
        row = json.loads((tmp_path / "evidence/results.json").read_text())["gates"][0]
        assert row["returncode"] == 7 and row["status"] == "failed"


def test_repeated_cancel_retires_generation_spawned_during_term(tmp_path):
    root = Path(__file__).resolve().parents[1]
    ready, spawned, later, output = (tmp_path / name for name in ("ready", "spawned", "later", "evidence"))
    descendant = ("import os,signal,time; from pathlib import Path; "
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  f"Path({str(spawned)!r}).write_text(str(os.getpid())); time.sleep(60)")
    child = ("import os,signal,subprocess,sys,time; from pathlib import Path\n"
             "def term(sig,frame):\n"
             f"    subprocess.Popen([sys.executable,'-c',{descendant!r}],start_new_session=True)\n"
             f"    while not Path({str(spawned)!r}).exists(): time.sleep(.01)\n"
             "    time.sleep(.1)\n"
             "    os._exit(7)\n"
             "signal.signal(signal.SIGTERM,term)\n"
             f"Path({str(ready)!r}).touch()\n"
             "time.sleep(60)")
    program = (
        "import sys; from pathlib import Path; from scripts import check_runner as r; "
        "r.capture_inputs=lambda root: {}; "
        f"g=[r.Gate('child','child',(sys.executable,'-c',{child!r}),Path({str(tmp_path)!r}),'test'),"
        f"r.Gate('later','later',(sys.executable,'-c',\"open({str(later)!r},'w').close()\"),Path({str(tmp_path)!r}),'test')]; "
        f"sys.exit(r.run_checks(Path({str(tmp_path)!r}),g,Path({str(output)!r})))"
    )
    process = subprocess.Popen([sys.executable, "-c", program], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        process.send_signal(signal.SIGTERM)
        while not spawned.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert spawned.exists()
        for _ in range(5):
            process.send_signal(signal.SIGINT)
            time.sleep(.02)
        process.communicate(timeout=10)
        assert process.returncode == 128 + signal.SIGTERM
        assert not Path(f"/proc/{int(spawned.read_text())}").exists()
        report = json.loads((output / "results.json").read_text())
        assert report["state"] == "interrupted" and report["gates"][0]["returncode"] == 7
        assert not later.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        if spawned.exists():
            try:
                os.kill(int(spawned.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_gate_refuses_to_adopt_unrelated_existing_children(tmp_path, fixed_inputs):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker = tmp_path / "gate-started"
    previous = runner.subreaper()
    try:
        assert runner.run_checks(tmp_path, [gate(tmp_path, "child", f"open({str(marker)!r},'w').close()")]) == 1
        assert unrelated.poll() is None and not marker.exists()
        assert runner.subreaper() == previous
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("save_evidence", [False, True])
def test_ignored_sigchld_refuses_gate_before_losing_exit_seven(tmp_path, save_evidence):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "evidence"
    output.mkdir()
    marker = tmp_path / "executed"
    child = f"from pathlib import Path; Path({str(marker)!r}).touch(); raise SystemExit(7)"
    program = (
        "import json,signal,sys; from pathlib import Path; from scripts import check_runner as r; "
        "signal.signal(signal.SIGCHLD,signal.SIG_IGN); row={}; refused=False\n"
        "try:\n"
        f"    r.run_gate(r.Gate('child','child',(sys.executable,'-c',{child!r}),Path.cwd(),'test'),"
        + (f"Path({str(output)!r})" if save_evidence else "None") + ",row)\n"
        "except OSError: refused=True\n"
        "print(json.dumps(dict(refused=refused,row=row)))"
    )
    result = subprocess.run([sys.executable, "-c", program], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["refused"] and not marker.exists(), observed
