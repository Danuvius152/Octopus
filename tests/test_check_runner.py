"""Verification evidence must describe the execution that actually happened."""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import check_runner as runner


@pytest.fixture
def fixed_inputs(monkeypatch):
    monkeypatch.setattr(runner, "capture_inputs", lambda *_: {"source": "fixed", "environment": "fixed"})


def gate(root, identifier, program, *, report=None):
    return runner.Gate(identifier, identifier, (sys.executable, "-c", program), root, "isolated regression", report)


@pytest.mark.parametrize(("arguments", "include_e2e"), [((), False), (("--e2e",), True)])
def test_cli_default_runs_the_commit_gate_set(monkeypatch, arguments, include_e2e):
    execute = Mock(return_value=0)
    monkeypatch.setattr(runner, "run_checks", execute)
    monkeypatch.setattr(sys, "argv", ["check.sh", *arguments])
    assert runner.main() == 0
    root, gates, output = execute.call_args.args
    assert gates == runner.commit_gates(root, e2e=include_e2e)
    assert any(gate.id == "e2e" for gate in gates) == include_e2e
    assert output is None


def test_cli_runs_only_the_named_gates(monkeypatch):
    execute = Mock(return_value=1)
    monkeypatch.setattr(runner, "run_checks", execute)
    monkeypatch.setattr(sys, "argv", ["check.sh", "--gate", "ruff", "--gate", "docs-index"])
    assert runner.main() == 1
    assert [gate.id for gate in execute.call_args.args[1]] == ["ruff", "docs-index"]


def test_cli_release_runs_independent_offline_and_live_gates(monkeypatch, tmp_path):
    execute = Mock(return_value=1)
    monkeypatch.setattr(runner, "run_checks", execute)
    monkeypatch.setattr(sys, "argv", ["check.sh", "--release", "--supervisor", "--output", str(tmp_path)])
    assert runner.main() == 1
    _, gates, output = execute.call_args.args
    assert [gate.id for gate in gates[-4:]] == ["supervisor", "offline-e2e", "codex-live-pytest", "codex-live-browser"]
    assert output == tmp_path
    assert next(gate for gate in gates if gate.id == "pytest").pytest_context


def test_cli_release_targeted_load_probe_records_parallel_arguments(monkeypatch, tmp_path):
    execute = Mock(return_value=0)
    monkeypatch.setattr(runner, "run_checks", execute)
    monkeypatch.setattr(sys, "argv", ["check.sh", "--release", "--output", str(tmp_path),
        "--pytest-target", "tests/test_bg_tasks.py", "--pytest-target", "tests/test_check_hooks.py", "--pytest-workers", "8"])
    assert runner.main() == 0
    [focused] = execute.call_args.args[1]
    assert focused.id == "pytest-focused" and focused.pytest_context
    assert focused.argv[-5:] == ("-n", "8", "--dist=loadfile", "--max-worker-restart=0", "-q")


@pytest.mark.parametrize("include_ruff", [False, True])
def test_cli_records_focused_pytest_as_a_separate_selection(monkeypatch, tmp_path, include_ruff):
    execute = Mock(return_value=0)
    monkeypatch.setattr(runner, "run_checks", execute)
    node = "tests/test_check_hooks.py::test_unrelated_document_does_not_run_product_checks"
    module = "tests/test_check_runner.py"
    destination = tmp_path / "evidence"
    selected_gates = ["--gate", "ruff"] if include_ruff else []
    monkeypatch.setattr(sys, "argv", ["check.sh", *selected_gates, "--pytest-target", node,
                                      "--pytest-target", module, "--output", str(destination)])
    assert runner.main() == 0
    _, gates, output = execute.call_args.args
    assert [gate.id for gate in gates[:-1]] == (["ruff"] if include_ruff else [])
    focused = gates[-1]
    assert focused.id == "pytest-focused" and focused.report == "pytest"
    assert focused.argv[1:] == (node, module, "-m", "not real and not knowledge_eval and not wiki_perf", "-q")
    assert node in focused.scope and module in focused.scope
    assert output == destination


@pytest.mark.parametrize(("edited", "symlink"), [
    ("test_local.py", False), ("conftest.py", False), ("helper.py", False),
    pytest.param("conftest.py", True, id="linked-conftest-content"),
    pytest.param("helper.py", True, id="helper-retarget"),
])
def test_focused_evidence_rejects_changes_to_an_ignored_pytest_input(tmp_path, monkeypatch, edited, symlink):
    root = tmp_path / "repo"
    folder = root / "tests/.agents"
    folder.mkdir(parents=True)
    target = folder / "test_local.py"
    (folder / "helper.py").write_text("VALUE = 1\n")
    (folder / "conftest.py").write_text("import pytest\nfrom helper import VALUE\n\n@pytest.fixture\ndef value():\n    return VALUE\n")
    dependency = folder / edited
    change = 'with dependency.open("a") as source:\n        source.write("\\n# changed while pytest was running\\n")'
    if symlink:
        original = dependency.rename(folder / f"original-{edited}")
        dependency.symlink_to(original.name)
        if edited == "helper.py":
            (folder / "alternate-helper.py").write_bytes(original.read_bytes())
            change = 'dependency.unlink()\n    dependency.symlink_to("alternate-helper.py")'
    target.write_text(f'''from pathlib import Path

def test_edit(value):
    assert value == 1
    dependency = Path(__file__).with_name({edited!r})
    {change}
''')
    (root / ".gitignore").write_text(".venv/\n.agents/\n.pytest_cache/\n__pycache__/\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "baseline"], cwd=root, check=True)
    subprocess.run(["git", "check-ignore", "-q", str(folder / edited)], cwd=root, check=True)
    pytest_bin = root / ".venv/bin/pytest"
    pytest_bin.parent.mkdir(parents=True)
    pytest_bin.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m pytest "$@"\n')
    pytest_bin.chmod(0o700)
    monkeypatch.setattr(runner, "__file__", str(root / "scripts/check_runner.py"))
    monkeypatch.setattr(runner, "environment_identity", lambda root: {})
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    output = tmp_path / "evidence"
    extra = ["--pytest-input", "tests/.agents/helper.py"] if edited == "helper.py" else []
    monkeypatch.setattr(sys, "argv", ["check.sh", "--pytest-target", "tests/.agents/test_local.py::test_edit", *extra,
                                     "--output", str(output)])

    assert runner.main() == 1
    report = json.loads((output / "results.json").read_text())
    [focused] = report["gates"]
    assert focused["returncode"] == 0 and focused["counts"]["passed"] == 1
    assert f"tests/.agents/{edited}" in focused["source_files"]
    assert report["inputs_changed"] and not report["success"]


@pytest.mark.parametrize("arguments", [
    ("--pytest-target", "tests/"),
    ("--gate", "pytest", "--pytest-target", "tests/test_check_runner.py"),
    ("--pytest-input", "tests/test_check_runner.py"),
    ("--pytest-target", "tests/test_check_runner.py", "--pytest-input", "tests/"),
    ("--e2e", "--gate", "ruff"),
    ("--release",),
    ("--release", "--output", "unused", "--gate", "e2e"),
    ("--gate", "offline-e2e", "--output", "unused"),
    ("--pytest-workers", "8", "--pytest-target", "tests/test_bg_tasks.py"),
])
def test_cli_rejects_invalid_selections_before_running_checks(monkeypatch, arguments):
    execute = Mock()
    monkeypatch.setattr(runner, "run_checks", execute)
    monkeypatch.setattr(sys, "argv", ["check.sh", *arguments])
    with pytest.raises(SystemExit, match="2"):
        runner.main()
    execute.assert_not_called()


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


@pytest.mark.parametrize("missing_partition", [False, True])
def test_release_preserves_child_failure_even_when_wrapper_exits_zero(tmp_path, fixed_inputs, missing_partition):
    failed_offline = '''import json, os
from pathlib import Path
out = Path(os.environ["OCTOPUS_E2E_EVIDENCE_DIR"])
out.mkdir()
partitions = [
    {"status": "failed", "returncode": 1, "counts": {"passed": 3, "failed": 1, "flaky": 0, "skipped": 0, "errors": 0}},
    {"status": "passed", "returncode": 0, "counts": {"passed": 2, "failed": 0, "flaky": 0, "skipped": 0, "errors": 0}}
]
'''
    if missing_partition:
        failed_offline += 'partitions.append({"status": "failed", "returncode": 1, "counts": None})\n'
    failed_offline += '(out / "result.json").write_text(json.dumps({"passed": False, "partitions": partitions}))\n'
    passing_live = '''import json, sys
from pathlib import Path
out = Path(sys.argv[-1])
out.mkdir()
(out / "result.json").write_text(json.dumps({"passed": True,
    "suites": {"browser": {"passed": 1, "failed": 0, "skipped": 0, "exit_code": 0}},
    "execution": {"browser": {"exit_code": 0}}}))
'''
    output = tmp_path / "release"
    assert runner.run_checks(tmp_path, [
        gate(tmp_path, "offline-e2e", failed_offline, report="offline-e2e"),
        gate(tmp_path, "codex-live-browser", passing_live, report="codex-live"),
    ], output) == 1
    offline, live = json.loads((output / "results.json").read_text())["gates"]
    assert offline["returncode"] == 0 and offline["status"] == "failed" and offline["evidence_error"]
    assert offline["counts"] == (None if missing_partition else {
        "passed": 5, "failed": 1, "flaky": 0, "skipped": 0, "errors": 0,
    })
    assert offline["partitions"][1]["counts"]["passed"] == 2
    assert live["status"] == "passed" and live["counts"]["passed"] == 1


@pytest.mark.parametrize(("first_counts", "second_counts", "expected_totals"), [
    ({"passed": 2}, {"passed": 1, "failed": 1, "flaky": 0, "skipped": 0, "errors": 0}, None),
    ({"passed": 2, "failed": 0, "flaky": 0, "skipped": 0, "errors": 0},
     {"passed": 1, "failed": -1, "flaky": 0, "skipped": 0, "errors": 0}, None),
    ({"passed": 2, "failed": 0, "flaky": 0, "skipped": 0, "errors": 0},
     {"passed": 0, "failed": 0, "flaky": 0, "skipped": 1, "errors": 0},
     {"passed": 2, "failed": 0, "flaky": 0, "skipped": 1, "errors": 0}),
])
def test_release_requires_valid_evidence_from_each_offline_partition(
    tmp_path, fixed_inputs, first_counts, second_counts, expected_totals,
):
    child = f'''import json, os
from pathlib import Path
out = Path(os.environ["OCTOPUS_E2E_EVIDENCE_DIR"])
out.mkdir()
counts = {[first_counts, second_counts]!r}
partitions = [{{"status": "passed", "returncode": 0, "counts": item}} for item in counts]
(out / "result.json").write_text(json.dumps({{"passed": True, "partitions": partitions}}))
'''
    output = tmp_path / "evidence"
    assert runner.run_checks(tmp_path, [gate(tmp_path, "offline-e2e", child, report="offline-e2e")], output) == 1
    [offline] = json.loads((output / "results.json").read_text())["gates"]
    assert offline["status"] == "failed" and offline["returncode"] == 0 and offline["evidence_error"]
    assert [part["counts"] for part in offline["partitions"]] == [first_counts, second_counts]
    assert offline["counts"] == expected_totals


@pytest.mark.parametrize("skip_all", [False, True])
def test_supervisor_uses_repo_config_and_requires_a_passing_case(tmp_path, fixed_inputs, monkeypatch, skip_all):
    for selector in runner.PYTEST_SELECTION_ENVIRONMENT:
        monkeypatch.delenv(selector, raising=False)
    root = tmp_path / "checkout"
    (root / "tests").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    python = root / ".venv/bin/python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\nmarkers = ["real", "supervisor"]\n')
    (root / "pytest.ini").write_text("[pytest]\naddopts = -k preserved\n")
    (root / "tests/conftest.py").write_text(
        "def pytest_addoption(parser):\n    parser.addoption('--run-real', action='store_true')\n"
    )
    (root / "tests/test_service_logging.py").touch()
    skip = "pytest.skip('fixture unavailable')"
    (root / "tests/test_other_supervisor.py").write_text(
        "import pytest\npytestmark = [pytest.mark.real, pytest.mark.supervisor]\n"
        f"def test_additional_supervisor():\n    {skip if skip_all else 'pass'}\n"
    )
    (root / "tests/test_service_supervisor.py").write_text(
        "import pytest\npytestmark = [pytest.mark.real, pytest.mark.supervisor]\n"
        f"def test_preserved():\n    {skip if skip_all else 'pass'}\n"
        f"def test_filtered():\n    {skip if skip_all else 'pytest.fail()'}\n"
    )
    plugin_root = Path(runner.__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", str(plugin_root), prepend=os.pathsep)
    supervisor = next(gate for gate in runner.release_gates(root, supervisor=True) if gate.id == "supervisor")
    output = tmp_path / "evidence"
    assert runner.run_checks(root, [supervisor], output) == 1
    [observed] = json.loads((output / "results.json").read_text())["gates"]
    assert observed["status"] == "failed"
    counts = observed["counts"]
    if skip_all:
        assert observed["returncode"] == 0
        assert counts["passed"] == 0 and counts["skipped"] == 3
        assert observed["evidence_error"] == "Supervisor report contains no passing tests"
    else:
        assert observed["returncode"] == 1
        assert counts["passed"] == 2 and counts["failed"] == 1
        assert observed["execution"]["failures"][0]["nodeid"].endswith("::test_filtered")


def test_pytest_evidence_distinguishes_xdist_failure_from_serial_pass(tmp_path, fixed_inputs):
    root = Path(runner.__file__).resolve().parents[1]
    failing = tmp_path / "test_sensitive.py"
    failing.write_text("def test_sensitive(pytestconfig):\n    assert not hasattr(pytestconfig, 'workerinput')\n")
    other = tmp_path / "test_neighbor.py"
    other.write_text("def test_neighbor(): pass\n")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    base = (sys.executable, "-m", "pytest", "-c", str(config), "-q")
    output = tmp_path / "execution"
    assert runner.run_checks(root, [
        runner.Gate("parallel", "parallel", (*base, "-n", "2", "--dist=loadfile", str(failing), str(other)),
                    root, "two files", "pytest", pytest_context=True),
        runner.Gate("serial", "serial", (*base, "-n", "0", str(failing)),
                    root, "one file", "pytest", pytest_context=True),
    ], output) == 1
    parallel, serial = json.loads((output / "results.json").read_text())["gates"]
    observed = parallel["execution"]
    assert observed["workers_started"] == 2 and observed["distribution"] == "loadfile"
    [failure] = observed["failures"]
    assert failure["nodeid"].endswith("test_sensitive.py::test_sensitive")
    assert failure["worker"] in ("gw0", "gw1")
    assert serial["execution"]["workers_started"] == 0 and serial["execution"]["failures"] == []
    assert serial["status"] == "passed" and parallel["status"] == "failed"


def test_pytest_evidence_identifies_the_crashed_worker(tmp_path, fixed_inputs):
    root = Path(runner.__file__).resolve().parents[1]
    crashing = tmp_path / "test_worker_crash.py"
    crashing.write_text("import os\ndef test_crash(): os._exit(17)\n")
    neighbor = tmp_path / "test_neighbor.py"
    neighbor.write_text("def test_neighbor(): pass\n")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    argv = (sys.executable, "-m", "pytest", "-c", str(config), "-q", "-n", "2", "--dist=loadfile",
            "--max-worker-restart=0", str(crashing), str(neighbor))
    probe = runner.Gate("crash", "crash", argv, root, "two files", "pytest", pytest_context=True)
    output = tmp_path / "evidence"
    assert runner.run_checks(root, [probe], output) == 1
    [observed] = json.loads((output / "results.json").read_text())["gates"]
    assert observed["returncode"] == 1 and observed["execution"]["workers_started"] == 2
    [failure] = observed["execution"]["failures"]
    assert failure["nodeid"].endswith("test_worker_crash.py::test_crash")
    assert failure["worker"] in ("gw0", "gw1")
    assert f"worker {failure['worker']!r} crashed while running" in (output / "crash.log").read_text()


def test_pytest_evidence_identifies_a_worker_lost_during_collection(tmp_path, fixed_inputs):
    root = Path(runner.__file__).resolve().parents[1]
    crashing = tmp_path / "test_collection_crash.py"
    crashing.write_text("def test_neighbor(): pass\n")
    (tmp_path / "conftest.py").write_text('''
import os
import time
from pathlib import Path

collected = Path(__file__).with_name("collected")


def pytest_xdist_node_collection_finished(node, ids):
    if node.gateway.id == "gw1" and ids:
        collected.touch()


def pytest_collection_modifyitems(config, items):
    if getattr(config, "workerinput", {}).get("workerid") == "gw0":
        deadline = time.monotonic() + 10
        while not collected.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Healthy worker did not finish collection")
            time.sleep(0.01)
        os._exit(17)
''')
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    argv = (sys.executable, "-m", "pytest", "-c", str(config), "-q", "-n", "2", "--dist=loadfile",
            "--max-worker-restart=0", str(crashing))
    probe = runner.Gate("crash", "crash", argv, root, "one file", "pytest", pytest_context=True)
    output = tmp_path / "evidence"
    assert runner.run_checks(root, [probe], output) == 1
    [observed] = json.loads((output / "results.json").read_text())["gates"]
    assert observed["returncode"] == 0 and observed["status"] == "failed"
    assert observed["counts"]["total"] == 0
    [crash] = observed["execution"]["worker_crashes"]
    assert crash["worker"] == "gw0" and crash["error"]


def test_pytest_evidence_attributes_collection_errors_to_the_reporting_worker(tmp_path, fixed_inputs):
    root = Path(runner.__file__).resolve().parents[1]
    source = tmp_path / "test_collection_error.py"
    source.write_text(
        "import os\nif os.environ.get('PYTEST_XDIST_WORKER') == 'gw0': raise RuntimeError('collection error')\n"
        "def test_neighbor(): pass\n"
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    argv = (sys.executable, "-m", "pytest", "-c", str(config), "-q", "-n", "2", "--dist=loadfile", str(source))
    probe = runner.Gate("collect", "collect", argv, root, "one file", "pytest", pytest_context=True)
    output = tmp_path / "evidence"
    assert runner.run_checks(root, [probe], output) == 1
    [observed] = json.loads((output / "results.json").read_text())["gates"]
    [failure] = [item for item in observed["execution"]["failures"] if item["nodeid"].endswith(source.name)]
    assert failure["phase"] == "collection" and failure["worker"] == "gw0"


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


def test_environment_filtered_focused_pytest_retains_the_actual_execution(tmp_path, fixed_inputs, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k selected")
    source = tmp_path / "test_scope.py"
    source.write_text("def test_selected(): pass\ndef test_other(): pass\n")
    output = tmp_path / "evidence"
    selection = runner.Gate("pytest-focused", "pytest", (sys.executable, "-m", "pytest", str(source), "-q"), tmp_path, "test scope", "pytest")
    assert runner.run_checks(tmp_path, [selection], output) == 1
    report = json.loads((output / "results.json").read_text())
    row = report["gates"][0]
    assert row["returncode"] == 0 and row["counts"]["total"] == 1
    assert row["selection_environment"]["PYTEST_ADDOPTS"] == "-k selected"
    assert row["evidence_error"] and not report["success"]


@pytest.mark.parametrize("selector", ["PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"])
def test_release_focused_pytest_refuses_plugin_overrides(tmp_path, fixed_inputs, monkeypatch, selector):
    monkeypatch.setenv(selector, "unapproved")
    marker = tmp_path / "started"
    selection = runner.Gate("pytest-focused", "pytest", (
        sys.executable, "-c", f"open({str(marker)!r}, 'w').close()",
    ), tmp_path, "test scope", "pytest", pytest_context=True)
    output = tmp_path / "evidence"
    assert runner.run_checks(tmp_path, [selection], output) == 1
    [row] = json.loads((output / "results.json").read_text())["gates"]
    assert row["status"] == "failed" and row["returncode"] is None and not marker.exists()
    assert selector in row["evidence_error"]


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
    monkeypatch.setattr(runner, "capture_inputs", lambda *_: next(identities))
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
        "r.capture_inputs=lambda *_: {}; "
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
        f"r.subreaper({original}); r.capture_inputs=lambda *_: {{}}; "
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


@pytest.mark.parametrize("save_evidence", [False, True])
def test_reaps_exited_orphan_while_gate_is_running(tmp_path, fixed_inputs, save_evidence):
    output = tmp_path / "evidence" if save_evidence else None
    program = """
import os
import time
from pathlib import Path

reader, writer = os.pipe()
launcher = os.fork()
if launcher == 0:
    os.close(reader)
    if os.fork() == 0:
        os.write(writer, str(os.getpid()).encode())
    os._exit(0)
os.close(writer)
os.waitpid(launcher, 0)
with os.fdopen(reader) as pipe:
    orphan = Path("/proc") / pipe.read()
deadline = time.monotonic() + 3
while orphan.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
assert not orphan.exists(), "Exited orphan outlived its running gate"
"""
    assert runner.run_checks(tmp_path, [gate(tmp_path, "orphan", program)], output) == 0


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
        "r.capture_inputs=lambda *_: {}; "
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
