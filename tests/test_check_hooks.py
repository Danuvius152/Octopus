"""Exercise the installed hook scheduler against real verification inputs."""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parents[1]


def git(root, env, *args):
    subprocess.run(["git", *args], cwd=root, env=env, check=True, timeout=15)


def commit_fixture(root, env):
    git(root, env, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "Baseline")


def run_pre_commit(root, env, *args):
    executable = shutil.which("lefthook")
    assert executable, "Install the required hook tool using scripts/setup-hooks.sh"
    try:
        return subprocess.run(
            [executable, "run", "pre-commit", "--no-auto-install", "--no-stage-fixed", "--no-tty", *args],
            cwd=root, env=env, text=True, capture_output=True, timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        error.add_note(f"Hook command {args!r}; pytest worker {os.environ.get('PYTEST_XDIST_WORKER', 'serial')}")
        for label, data in (("stdout", error.stdout), ("stderr", error.stderr)):
            if data:
                error.add_note(f"Last 4096 bytes of {label}:\n{data[-4096:].decode(errors='replace')}")
        raise


@pytest.fixture
def hook_env():
    return {key: value for key, value in os.environ.items() if not key.startswith(("LEFTHOOK", "GIT_"))}


@pytest.fixture
def hooks(tmp_path, hook_env):
    git(tmp_path, hook_env, "init", "-q")
    config = yaml.safe_load((PROJECT / "lefthook.yml").read_text())
    config.pop("rc", None)
    for name, command in config["pre-commit"]["commands"].items():
        command["run"] = f"{shlex.quote(sys.executable)} -c {shlex.quote(f'print(\"EXECUTED:{name}\")')}"
    (tmp_path / "lefthook.yml").write_text(yaml.safe_dump(config))
    return tmp_path, hook_env


def staged_checks(hooks, path):
    root, env = hooks
    changed = root / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.touch(exist_ok=True)
    git(root, env, "add", "--", path)
    result = run_pre_commit(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    return set(re.findall(r"EXECUTED:([a-z-]+)", result.stdout))


@pytest.mark.parametrize("path", [
    "web/src/components/ChatView.tsx", "web/e2e/run.ts", "web/tests/e2eSelection.test.ts",
    "web/vite.config.ts", "web/playwright.config.ts", "web/eslint.config.js", "web/tsconfig.e2e.json",
    "web/package.json", "web/bun.lock", "web/patches/vitest@4.1.12.patch", "web/src/lib/recordingReplay.json",
    "tests/fixtures/codex-file-change-result.json", "tests/fixtures/wiki-command-cases.json", "server/app_agent_client.js",
    "lefthook.yml", "scripts/toolchain-env.sh",
])
def test_frontend_inputs_trigger_all_existing_frontend_checks(hooks, path):
    assert {"eslint", "tsc", "vitest"} <= staged_checks(hooks, path)


def test_unrelated_document_does_not_run_product_checks(hooks):
    assert staged_checks(hooks, "docs/user-guide.md") == set()


@pytest.mark.parametrize(("path", "expected"), [
    ("server/models.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/routers/sessions.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/routers/nested/example.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/submission_models.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/wiki/contracts.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/db/users.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("tests/test_check_runner.py", {"ruff", "pytest", "test-support-imports"}),
    ("scripts/check_runner.py", {"ruff", "pytest", "test-support-imports"}),
    ("scripts/check.sh", {"ruff", "pytest", "test-support-imports"}),
    ("scripts/dump-openapi.py", {"ruff", "pytest", "test-support-imports", "contracts"}),
    ("scripts/check-contracts.sh", {"contracts"}),
    ("scripts/generate-contracts.sh", {"contracts"}),
    ("scripts/snapshot-contract-source.py", {"ruff", "pytest", "test-support-imports", "contracts"}),
    ("scripts/toolchain-env.sh", {"ruff", "pytest", "test-support-imports", "eslint", "tsc", "vitest", "contracts"}),
    ("lefthook.yml", {"ruff", "pytest", "test-support-imports", "eslint", "tsc", "vitest", "contracts"}),
    ("pyproject.toml", {"ruff", "mypy", "pytest", "test-support-imports", "contracts"}),
    ("constraints-python.txt", {"ruff", "mypy", "pytest", "test-support-imports", "contracts"}),
    ("constraints-wiki.txt", {"contracts"}),
    ("web/release-entry-assets.mjs", {"eslint", "tsc", "vitest", "pytest"}),
    ("web/package.json", {"eslint", "tsc", "vitest", "pytest", "contracts"}),
    ("web/bun.lock", {"eslint", "tsc", "vitest", "pytest", "contracts"}),
    ("web/bunfig.toml", {"contracts"}),
    ("web/src/api/contracts.ts", {"eslint", "tsc", "vitest", "contracts"}),
    ("docs/implemented/memory.md", {"docs-index"}),
    ("docs/plans/example/plan.md", {"docs-index"}),
])
def test_other_existing_gates_match_direct_and_nested_inputs(hooks, path, expected):
    assert staged_checks(hooks, path) == expected


def test_renaming_contract_out_of_its_original_path_still_runs_check(hooks):
    root, env = hooks
    path = "web/src/api/contracts.ts"
    (root / path).parent.mkdir(parents=True)
    (root / path).write_text("export type Payload = string;\n")
    git(root, env, "add", "--", path)
    commit_fixture(root, env)
    git(root, env, "mv", "--", path, "shifted-contract.md")

    result = run_pre_commit(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXECUTED:contracts" in result.stdout


@pytest.fixture
def pytest_checkout(tmp_path, hook_env):
    root = tmp_path / "moved pytest checkout"
    root.mkdir()
    env = {key: value for key, value in hook_env.items() if not key.startswith("PYTEST_")}
    env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = "1"
    git(root, env, "init", "-q")
    for path in (".gitignore", "lefthook.yml", "pyproject.toml", "scripts/check.sh",
                 "scripts/check_runner.py", "scripts/pytest_diagnostics.py", "scripts/pytest_evidence.py",
                 "scripts/python_environment.py",
                 "scripts/toolchain-env.sh"):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT / path, target)
    (root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (root / "web").mkdir()
    (root / "web/package.json").write_text('{"name":"pytest-hook-fixture","private":true}\n')
    git(root, env, "add", "--", ".gitignore", "lefthook.yml", "pyproject.toml", "scripts", "web/package.json")
    commit_fixture(root, env)
    source = root / "tests/test_gate.py"
    source.parent.mkdir()
    source.write_text("def test_pass_only(): pass\ndef test_failure(): assert False, 'expected fixture failure'\n")
    git(root, env, "add", "--", "tests/test_gate.py")
    return root, env


@pytest.mark.parametrize(("variable", "value"), [
    ("PYTEST_ADDOPTS", "-k pass_only"),
    ("PYTEST_PLUGINS", "custom_pytest_plugin"),
    ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
])
def test_full_pytest_hook_refuses_inherited_selection(pytest_checkout, tmp_path, variable, value):
    root, env = pytest_checkout
    output = tmp_path / "refused hook evidence"
    env[variable] = value
    env["OCTOPUS_PYTEST_OUTPUT"] = str(output)

    result = run_pre_commit(root, env, "--command", "pytest")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "bringing up nodes" not in result.stdout
    message = f"Unset {variable} to certify the requested pytest selection"
    assert message in result.stdout
    evidence = json.loads((output / "results.json").read_text())
    [full] = evidence["gates"]
    assert evidence["state"] == "complete" and not evidence["success"]
    assert full["id"] == "pytest" and full["evidence_error"] == message
    assert full["selection_environment"][variable] == value
    assert full["returncode"] is None and full["log"] is None and full["report"] is None


@pytest.mark.parametrize(("entry", "code", "local_config"), [
    ("hook", 1, False),
    ("hook", 5, False),
    ("hook", 1, True),
    ("combined", 1, True),
])
def test_full_pytest_preserves_failed_execution(pytest_checkout, tmp_path, entry, code, local_config):
    root, env = pytest_checkout
    if code == 5:
        (root / "tests/test_gate.py").write_text("import pytest\n@pytest.mark.real\ndef test_live(): assert False\n")
        git(root, env, "add", "--", "tests/test_gate.py")
    if local_config:
        (root / "pytest.ini").write_text("[pytest]\naddopts = -k pass_only\n")
    output = tmp_path / "failed pytest evidence"
    if entry == "hook":
        env["OCTOPUS_PYTEST_OUTPUT"] = str(output)
        result = run_pre_commit(root, env, "--command", "pytest")
    else:
        result = subprocess.run(["./scripts/check.sh", "--gate", "pytest", "--output", str(output)],
                                cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"(exit {code})" in result.stdout
    evidence = json.loads((output / "results.json").read_text())
    [full] = evidence["gates"]
    assert evidence["state"] == "complete" and not evidence["success"]
    assert full["id"] == "pytest" and full["status"] == "failed"
    assert full["returncode"] == code and full["evidence_error"] is None
    assert (output / "pytest.xml").is_file()
    if code == 1:
        assert full["counts"]["passed"] == 1 and full["counts"]["failed"] == 1
        assert "expected fixture failure" in (output / "pytest.log").read_text()
    else:
        assert full["counts"]["total"] == 0


def test_combined_cli_certifies_only_the_explicit_pytest_target(pytest_checkout, tmp_path):
    root, env = pytest_checkout
    output = tmp_path / "focused evidence"
    target = "tests/test_gate.py::test_pass_only"

    result = subprocess.run(["./scripts/check.sh", "--pytest-target", target, "--output", str(output)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads((output / "results.json").read_text())
    [focused] = evidence["gates"]
    assert evidence["success"] and focused["id"] == "pytest-focused"
    assert target in focused["scope"] and focused["counts"]["passed"] == 1


def test_focused_pytest_output_preserves_console_import_resolution(pytest_checkout, tmp_path):
    root, env = pytest_checkout
    output = tmp_path / "focused evidence"
    target = "tests/test_gate.py"
    (root / "httpx.py").write_text("raise RuntimeError('loaded the shadow module')\n")
    (root / target).write_text("import httpx\n\ndef test_import(): assert httpx.Client\n")
    command = ["./scripts/check.sh", "--pytest-target", target]

    for options in ([], ["--output", str(output)]):
        result = subprocess.run([*command, *options], cwd=root, env=env, text=True,
                                capture_output=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["counts"]["passed"] == 1 and Path(gate["execution_context"]).is_file()


def test_focused_pytest_output_preserves_inherited_xdist_context(pytest_checkout, tmp_path):
    root, env = pytest_checkout
    output = tmp_path / "focused evidence"
    target = "tests/test_gate.py"
    env.update(PYTEST_XDIST_WORKER="gw-parent", PYTEST_XDIST_TESTRUNUID="parent-run", PYTEST_XDIST_WORKER_COUNT="3")
    (root / target).write_text("""
import os

def test_inherited_context():
    assert os.environ['PYTEST_XDIST_WORKER'] == 'gw-parent'
    assert os.environ['PYTEST_XDIST_TESTRUNUID'] == 'parent-run'
    assert os.environ['PYTEST_XDIST_WORKER_COUNT'] == '3'
""")

    result = subprocess.run(["./scripts/check.sh", "--pytest-target", target, "--output", str(output)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 0, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["counts"]["passed"] == 1
    assert json.loads(Path(gate["execution_context"]).read_text())["workers_started"] == 0


def test_focused_pytest_accepts_an_unrelated_plugin_named_dsession(pytest_checkout, tmp_path):
    root, env = pytest_checkout
    output = tmp_path / "focused evidence"
    target = "tests/test_gate.py"
    env.update(PYTEST_PLUGINS="dsession", PYTHONPATH=str(root))
    (root / "dsession.py").write_text("def pytest_configure(config): config.fixture_plugin_loaded = True\n")
    (root / target).write_text("def test_plugin(pytestconfig): assert pytestconfig.fixture_plugin_loaded\n")

    result = subprocess.run(["./scripts/check.sh", "--pytest-target", target, "--output", str(output)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 0, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["returncode"] == 0 and gate["counts"]["passed"] == 1
    assert json.loads(Path(gate["execution_context"]).read_text())["workers_started"] == 0


@pytest.mark.parametrize("release", [False, True], ids=["diagnostic", "release"])
@pytest.mark.parametrize(("representation", "line"), [
    ("BrokenRenderer()", 1), ("TerminalOnly()", 1), ("WithLocation(item.path, 2)", 2),
    ("WithLocation(7, True)", 1),
], ids=["broken-renderer", "terminal-only", "structured-location", "invalid-location"])
def test_focused_pytest_keeps_plugin_failure_text_and_location(pytest_checkout, tmp_path, release, representation, line):
    root, env = pytest_checkout
    output = tmp_path / "focused evidence"
    target = "tests/test_gate.py"
    config = root / "pyproject.toml"
    config.write_text(config.read_text().replace('addopts = "--strict-markers"', 'addopts = "--strict-markers --tb=no -rN"'))
    (root / target).write_text("def test_a():\n    assert False\n\ndef test_b(): pass\n")
    (root / "tests/conftest.py").write_text(f"""from types import SimpleNamespace

import pytest

class BrokenRenderer:
    def __str__(self):
        return 'original failure text'

    def toterminal(self, writer):
        raise RuntimeError('renderer failure')

class TerminalOnly:
    def toterminal(self, writer):
        writer.line('original failure text')

class WithLocation(TerminalOnly):
    def __init__(self, path, line):
        self.reprcrash = SimpleNamespace(path=path, lineno=line, message='original failure text')

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item):
    report = yield
    if report.failed:
        report.longrepr = {representation}
    return report
""")
    command = ["./scripts/check.sh", "--pytest-target", target, "--output", str(output)]
    if release:
        command.append("--release")

    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["returncode"] == 1
    assert gate["counts"] == {"passed": 1, "failed": 1, "errors": 0, "skipped": 0, "total": 2}
    [failure] = json.loads(Path(gate["execution_context"]).read_text())["failures"]
    assert failure["nodeid"] == f"{target}::test_a"
    assert failure["error"] == "original failure text"
    assert root / failure["location"]["file"] == root / target and failure["location"]["line"] == line


def test_focused_pytest_retains_surrogate_failure_and_executes_next_test(pytest_checkout, tmp_path):
    root, env = pytest_checkout
    output = tmp_path / "focused evidence"
    target = "tests/test_gate.py"
    (root / target).write_text("def test_a(): raise ValueError(chr(0xdcff))\ndef test_b(): pass\n")

    result = subprocess.run(["./scripts/check.sh", "--pytest-target", target, "--output", str(output)],
                            cwd=root, env=env, errors="surrogateescape", capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["returncode"] == 1
    assert gate["counts"] == {"passed": 1, "failed": 1, "errors": 0, "skipped": 0, "total": 2}
    [failure] = json.loads(Path(gate["execution_context"]).read_text())["failures"]
    assert failure["nodeid"] == f"{target}::test_a"
    assert f"ValueError: {chr(0xdcff)}" in failure["error"]


@pytest.mark.parametrize("release", [False, True], ids=["diagnostic", "release"])
@pytest.mark.parametrize("wrapper", [False, True], ids=["hook", "wrapper"])
def test_focused_pytest_saves_failure_before_a_report_consumer_raises(pytest_checkout, tmp_path, release, wrapper):
    root, env = pytest_checkout
    output = tmp_path / "report consumer evidence"
    target = "tests/test_gate.py"
    decorator = "    @pytest.hookimpl(wrapper=True, tryfirst=True)\n" if wrapper else ""
    tail = "        return (yield)\n" if wrapper else ""
    (root / "tests/conftest.py").write_text(f"""
import pytest

class BrokenReporter:
{decorator}    def pytest_runtest_logreport(self, report):
        if report.failed:
            raise OSError('report consumer failed')
{tail}
def pytest_configure(config):
    config.pluginmanager.register(BrokenReporter())
""")
    command = ["./scripts/check.sh", "--pytest-target", target, "--output", str(output)]
    if release:
        command.append("--release")

    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["returncode"] == 3
    original, consumer = json.loads(Path(gate["execution_context"]).read_text())["failures"]
    assert original["nodeid"] == f"{target}::test_failure" and original["phase"] == "call"
    assert "AssertionError: expected fixture failure" in original["error"]
    assert consumer["phase"] == "internal" and "OSError: report consumer failed" in consumer["error"]


@pytest.mark.parametrize("release", [False, True], ids=["diagnostic", "release"])
def test_focused_pytest_reports_an_unwritable_execution_artifact(pytest_checkout, tmp_path, release):
    root, env = pytest_checkout
    output = tmp_path / "focused evidence"
    execution = output / "pytest-focused-execution.json"
    (root / "tests/conftest.py").write_text(f"""
from pathlib import Path

def pytest_sessionstart():
    Path({str(execution)!r}).mkdir()
""")
    command = ["./scripts/check.sh", "--pytest-target", "tests/test_gate.py::test_pass_only", "--output", str(output)]
    if release:
        command.append("--release")
    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == int(release), result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["returncode"] == 0 and gate["counts"]["passed"] == 1
    assert bool(gate["evidence_error"]) == release
    assert "Cannot save pytest diagnostic evidence" in (output / "pytest-focused.log").read_text()


def test_focused_pytest_keeps_observed_failure_after_interruption(pytest_checkout, tmp_path):
    root, env = pytest_checkout
    output = tmp_path / "interrupted pytest evidence"
    target = "tests/test_gate.py"
    running = tmp_path / "second-test-pid"
    (root / target).write_text(f"""
import os
import time
from pathlib import Path


def test_a():
    raise ValueError('failure before interruption')


def test_b():
    Path({str(running)!r}).write_text(str(os.getpid()))
    time.sleep(60)
""")
    command = ["./scripts/check.sh", "--pytest-target", target, "--output", str(output)]
    manifest = output / "results.json"
    execution = output / "pytest-focused-execution.json"
    with (tmp_path / "runner.log").open("w+") as log, subprocess.Popen(
        command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
    ) as process:
        try:
            deadline = time.monotonic() + 15
            while not (running.is_file() and running.stat().st_size and execution.is_file()):
                if process.poll() is not None or time.monotonic() >= deadline:
                    log.seek(0)
                    pytest.fail(f"Failure evidence and the running test did not overlap:\n{log.read()}")
                time.sleep(0.02)

            test_pid = int(running.read_text())
            os.kill(test_pid, 0)
            observed = json.loads(execution.read_text())
            [failure] = observed["failures"]
            assert failure["nodeid"] == f"{target}::test_a"
            assert failure["phase"] == "call" and failure["worker"] is None
            assert "ValueError: failure before interruption" in failure["error"]
            assert "raise ValueError" in failure["error"]
            assert root / failure["location"]["file"] == root / target
            assert failure["location"]["line"] == 8
            assert observed["targets"] == [target] and observed["exit_code"] is None

            report = json.loads(manifest.read_text())
            assert report["inputs_before"]["source"]["head"]
            [gate] = report["gates"]
            assert gate["status"] == "incomplete" and target in gate["argv"]
            assert gate["execution_context"] == str(execution)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)

    assert process.returncode == 128 + signal.SIGTERM
    assert not Path(f"/proc/{test_pid}").exists()
    assert json.loads(execution.read_text()) == observed
    report = json.loads(manifest.read_text())
    assert report["state"] == "interrupted" and not report["success"]
    [gate] = report["gates"]
    assert gate["status"] == "interrupted" and gate["counts"] is None
    assert not Path(gate["report"]).exists()


@pytest.mark.parametrize("source", [
    "test-module", "conftest", "conftest-plugin", "plugin-runtime", "plugin-syntax", "environment-plugin",
])
def test_focused_pytest_records_the_original_import_error(pytest_checkout, tmp_path, source):
    root, env = pytest_checkout
    output = tmp_path / "import evidence"
    target = "tests/test_gate.py"
    broken = target if source == "test-module" else "tests/conftest.py"
    if source == "environment-plugin":
        env["PYTEST_PLUGINS"] = "missing_octopus_environment_plugin"
    elif source in ("plugin-runtime", "plugin-syntax"):
        (root / broken).write_text("pytest_plugins = ['octopus_fixture_plugin']\n")
        (root / "tests/octopus_fixture_plugin.py").write_text(
            "raise RuntimeError('intentional plugin failure')\n" if source == "plugin-runtime" else "def :\n"
        )
    elif source == "conftest-plugin":
        (root / broken).write_text("pytest_plugins = ['missing_octopus_fixture_plugin']\n")
    else:
        (root / broken).write_text("raise ImportError('intentional import failure')\n")
    if source == "conftest":
        env["PYTEST_XDIST_WORKER"] = "gw-parent"

    result = subprocess.run(["./scripts/check.sh", "--pytest-target", target, "--output", str(output)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["status"] == "failed"
    assert gate["returncode"] == (2 if source == "test-module" else 4 if source == "conftest" else 1)
    execution = json.loads(Path(gate["execution_context"]).read_text())
    [failure] = execution["failures"]
    assert failure["phase"] == "collection" and failure["worker"] is None
    if source == "conftest-plugin":
        assert "No module named 'missing_octopus_fixture_plugin'" in failure["error"]
    elif source == "environment-plugin":
        assert "No module named 'missing_octopus_environment_plugin'" in failure["error"]
    elif source in ("plugin-runtime", "plugin-syntax"):
        expected = "RuntimeError: intentional plugin failure" if source == "plugin-runtime" else "SyntaxError: invalid syntax"
        assert expected in failure["error"]
        assert Path(failure["location"]["file"]).name == "octopus_fixture_plugin.py"
        assert failure["location"]["line"] == 1
    else:
        assert root / failure["nodeid"] == root / broken
        assert "ImportError: intentional import failure" in failure["error"]
        assert root / failure["location"]["file"] == root / broken
    assert execution["targets"] == ([target] if source == "test-module" else [])


@pytest.mark.parametrize("release", [False, True], ids=["diagnostic", "release"])
@pytest.mark.parametrize(("source", "exit_code"), [
    ("configuration", 3), ("configuration-usage", 4), ("conftest-syntax", 4), ("addopts-plugin", 1),
    ("conftest-system-exit", 1),
])
def test_focused_pytest_records_startup_errors(pytest_checkout, tmp_path, release, source, exit_code):
    root, env = pytest_checkout
    output = tmp_path / "startup evidence"
    conftest = root / "tests/conftest.py"
    if source == "configuration":
        conftest.write_text("def pytest_configure():\n    import missing_octopus_configuration_plugin\n")
    elif source == "configuration-usage":
        conftest.write_text("""import pytest

def pytest_configure():
    try:
        import missing_octopus_configuration_plugin
    except ImportError as error:
        raise pytest.UsageError('cannot configure fixture plugin') from error
""")
    elif source == "addopts-plugin":
        config = root / "pyproject.toml"
        config.write_text(config.read_text().replace(
            'addopts = "--strict-markers"', 'addopts = "--strict-markers -p missing_octopus_addopts_plugin"',
        ))
    elif source == "conftest-system-exit":
        conftest.write_text("raise SystemExit('primary conftest import abort')\n")
    else:
        conftest.write_text("def :\n")
    command = ["./scripts/check.sh", "--pytest-target", "tests/test_gate.py", "--output", str(output)]
    if release:
        command.append("--release")

    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["returncode"] == exit_code
    execution = json.loads(Path(gate["execution_context"]).read_text())
    [failure] = execution["failures"]
    assert failure["worker"] is None
    if source.startswith("configuration"):
        assert failure["phase"] == "internal"
        assert "No module named 'missing_octopus_configuration_plugin'" in failure["error"]
        if source == "configuration-usage":
            assert "UsageError: cannot configure fixture plugin" in failure["error"]
        assert root / failure["location"]["file"] == conftest
        assert failure["location"]["line"] == (2 if source == "configuration" else 5)
    elif source == "addopts-plugin":
        assert failure["phase"] == "collection"
        assert "No module named 'missing_octopus_addopts_plugin'" in failure["error"]
    elif source == "conftest-system-exit":
        assert failure["phase"] == "collection"
        assert "SystemExit: primary conftest import abort" in failure["error"]
        assert root / failure["location"]["file"] == conftest
        assert failure["location"]["line"] == 1
    else:
        assert failure["phase"] == "collection" and failure["nodeid"] == "tests/conftest.py"
        assert "SyntaxError: invalid syntax" in failure["error"]
        assert root / failure["location"]["file"] == conftest
        assert failure["location"]["line"] == 1
    assert execution["targets"] == (["tests/test_gate.py"] if source.startswith("configuration") else [])
    assert "tests/test_gate.py" in gate["source_files"] and "tests/test_gate.py" in gate["argv"]


@pytest.mark.parametrize("release", [False, True], ids=["diagnostic", "release"])
@pytest.mark.parametrize(("hook", "phase"), [
    ("pytest_collection_modifyitems", "collection"),
    ("pytest_configure", "startup"),
    ("pytest_sessionstart", "startup"),
], ids=["collection", "configure", "sessionstart"])
@pytest.mark.parametrize(("statement", "exit_code"), [
    ("pytest.exit('execution rejected by validation plugin', returncode=17)", 17),
    ("pytest.exit('execution stopped successfully', returncode=0)", 0),
    ("raise KeyboardInterrupt", 2),
], ids=["failed", "successful", "interrupt"])
def test_focused_pytest_records_only_failed_explicit_exits(pytest_checkout, tmp_path, release, statement, exit_code,
                                                         hook, phase):
    root, env = pytest_checkout
    output = tmp_path / "explicit exit evidence"
    conftest = root / "tests/conftest.py"
    conftest.write_text(f"import pytest\n\ndef {hook}():\n    {statement}\n")
    command = ["./scripts/check.sh", "--pytest-target", "tests/test_gate.py", "--output", str(output)]
    if release:
        command.append("--release")

    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == int(bool(exit_code) or release or phase == "startup"), result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["returncode"] == exit_code
    path = Path(gate["execution_context"])
    if exit_code == 17:
        [failure] = json.loads(path.read_text())["failures"]
        assert failure["phase"] == phase
        assert "execution rejected by validation plugin" in failure["error"]
        assert hook in failure["error"]
        assert root / failure["location"]["file"] == conftest
        assert failure["location"]["line"] == 4
    elif phase == "collection":
        assert json.loads(path.read_text())["failures"] == []
    else:
        assert not path.exists()


@pytest.mark.parametrize("workers", ["1", "auto"])
def test_focused_pytest_attributes_collection_errors_from_configured_xdist(pytest_checkout, tmp_path, workers):
    root, env = pytest_checkout
    output = tmp_path / "parallel evidence"
    target = "tests/test_gate.py"
    config = root / "pyproject.toml"
    config.write_text(config.read_text().replace('addopts = "--strict-markers"', f'addopts = "--strict-markers -n {workers}"'))
    (root / target).write_text("raise ImportError('intentional worker module import failure')\n")

    result = subprocess.run(["./scripts/check.sh", "--pytest-target", target, "--output", str(output)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["counts"]["errors"] == 1
    execution = json.loads(Path(gate["execution_context"]).read_text())
    assert execution["distribution"] == "load"
    assert execution["workers_requested"] == execution["workers_started"] == 1
    [failure] = execution["failures"]
    assert failure["nodeid"] == target and failure["phase"] == "collection" and failure["worker"] == "gw0"
    assert "ImportError: intentional worker module import failure" in failure["error"]


@pytest.mark.parametrize("during_collection", [False, True], ids=["crash", "internal-error"])
def test_focused_parallel_pytest_records_an_observable_worker_error(pytest_checkout, tmp_path, during_collection):
    root, env = pytest_checkout
    output = tmp_path / "worker crash evidence"
    target = "tests/test_gate.py"
    if during_collection:
        (root / "tests/conftest.py").write_text("""
def pytest_collection_modifyitems(config):
    if getattr(config, 'workerinput', {}).get('workerid') == 'gw1':
        raise RuntimeError('intentional worker collection hook failure')
""")
    else:
        (root / target).write_text("import os\n\ndef test_crash(): os._exit(17)\n")

    result = subprocess.run(["./scripts/check.sh", "--release", "--pytest-target", target,
                             "--pytest-workers", "2" if during_collection else "1", "--output", str(output)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)

    assert result.returncode == 1, result.stdout + result.stderr
    [gate] = json.loads((output / "results.json").read_text())["gates"]
    assert gate["status"] == "failed"
    execution = json.loads(Path(gate["execution_context"]).read_text())
    if during_collection:
        [failure] = [item for item in execution["failures"]
                     if "RuntimeError: intentional worker collection hook failure" in item["error"]]
        assert failure["phase"] == "internal"
        assert execution["workers_requested"] == 2 and failure["worker"] == "gw1"
        assert "tests/conftest.py" in failure["error"]
        assert failure["location"] is None
    else:
        [failure] = execution["failures"]
        assert failure["nodeid"] == f"{target}::test_crash" and failure["worker"] == "gw0"
        assert "worker 'gw0' crashed while running 'tests/test_gate.py::test_crash'" in failure["error"]
        [crash] = execution["worker_crashes"]
        assert crash["worker"] == "gw0" and crash["error"]


@pytest.fixture
def contracts_checkout(tmp_path, hook_env):
    root = tmp_path / "moved checkout"
    root.mkdir()
    git(root, hook_env, "init", "-q")
    for path in (".gitignore", "lefthook.yml", "scripts/check-contracts.sh",
                 "scripts/dump-openapi.py", "scripts/generate-contracts.sh", "scripts/snapshot-contract-source.py",
                 "scripts/toolchain-env.sh", "web/package.json"):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT / path, target)
    (root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (root / "web/node_modules").symlink_to(PROJECT / "web/node_modules", target_is_directory=True)
    hook_env["OCTOPUS_ENV_FILE"] = os.devnull

    model = root / "server/submission_models.py"
    model.parent.mkdir()
    (model.parent / "__init__.py").touch()
    (model.parent / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from server.submission_models import Reply\n"
        "app = FastAPI()\n"
        "@app.post('/items')\n"
        "def create_item(item: Reply) -> Reply:\n"
        "    return item\n"
    )
    model.write_text("from pydantic import BaseModel\nclass Reply(BaseModel):\n    original: str\n")
    contract = root / "web/src/api/contracts.ts"
    contract.parent.mkdir(parents=True)

    def generate():
        result = subprocess.run(["bun", "run", "generate:contracts"], cwd=root / "web",
                                env=hook_env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr

    generate()
    git(root, hook_env, "add", "--", ".gitignore", "lefthook.yml", "scripts", "server",
        "web/package.json", str(contract.relative_to(root)))
    commit_fixture(root, hook_env)
    return root, hook_env, generate


def test_changes_to_model_require_staging_the_regenerated_contract(contracts_checkout):
    root, hook_env, generate = contracts_checkout
    model = root / "server/submission_models.py"
    contract = root / "web/src/api/contracts.ts"
    with model.open("a") as stream:
        stream.write("    staged_field: str\n")
    git(root, hook_env, "add", str(model.relative_to(root)))
    generate()
    regenerated = contract.read_bytes()
    assert b"staged_field" in regenerated

    blocked = run_pre_commit(root, hook_env, "--command", "contracts")
    assert blocked.returncode == 1
    assert "contracts.ts is STALE" in blocked.stdout and "staged_field" in blocked.stdout
    assert contract.read_bytes() == regenerated

    git(root, hook_env, "add", str(contract.relative_to(root)))
    accepted = run_pre_commit(root, hook_env, "--command", "contracts")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "contracts.ts is in sync" in accepted.stdout


def test_staged_check_excludes_intent_to_add_files(contracts_checkout):
    root, env, _ = contracts_checkout
    dependency = "server/future_dependency.py"
    (root / dependency).write_text("# Intended for a later commit\n")
    git(root, env, "add", "--intent-to-add", dependency)
    with (root / "server/main.py").open("a") as stream:
        stream.write("\nimport server.future_dependency\n")
    git(root, env, "add", "server/main.py")

    git(root, env, "config", "core.hooksPath", ".git/hooks")
    subprocess.run(["lefthook", "install", "--force"], cwd=root, env=env, check=True, timeout=15)
    commands = yaml.safe_load((root / "lefthook.yml").read_text())["pre-commit"]["commands"]
    env["LEFTHOOK_EXCLUDE"] = ",".join(name for name in commands if name != "contracts")
    commit = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
              "commit", "--no-gpg-sign", "-m", "Import the new module"]
    result = subprocess.run(commit, cwd=root, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "No module named 'server.future_dependency'" in result.stdout + result.stderr

    git(root, env, "add", dependency)
    result = subprocess.run(commit, cwd=root, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "contracts.ts is in sync" in result.stdout + result.stderr


@pytest.mark.parametrize("setting", ["autocrlf", "relative_tmpdir"])
def test_staged_check_accepts_unchanged_contract_with_local_settings(contracts_checkout, setting):
    root, env, _ = contracts_checkout
    generator = "scripts/generate-contracts.sh"
    with (root / generator).open("a") as stream:
        stream.write("\n# Schema stays the same.\n")
    git(root, env, "add", generator)
    if setting == "autocrlf":
        (root / ".gitattributes").write_text(f"{generator} eol=crlf export-ignore\n")
        git(root, env, "add", ".gitattributes")
        git(root, env, "config", "core.autocrlf", "true")
    else:
        env["TMPDIR"] = "."

    result = run_pre_commit(root, env, "--command", "contracts")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "contracts.ts is in sync" in result.stdout


@pytest.mark.parametrize("staged", [True, False], ids=["hook", "working-tree"])
def test_generator_cannot_replace_the_checked_contract(contracts_checkout, staged):
    root, env, _ = contracts_checkout
    contract = root / "web/src/api/contracts.ts"
    original = contract.read_bytes()
    (root / "server/new_model.py").write_text(
        "from pydantic import BaseModel\nclass NewModel(BaseModel):\n    added_field: str\n"
    )
    (root / "server/submission_models.py").write_text(
        "from server.new_model import NewModel\nclass Reply(NewModel):\n    original: str\n"
    )
    with (root / "scripts/generate-contracts.sh").open("a") as stream:
        stream.write('\ncp -- "$output" "$root/web/src/api/contracts.ts"\n')
    if staged:
        git(root, env, "add", "server", "scripts/generate-contracts.sh")
        result = run_pre_commit(root, env, "--command", "contracts")
    else:
        env["TMPDIR"] = "."
        result = subprocess.run(["./scripts/check-contracts.sh"], cwd=root, env=env,
                                text=True, capture_output=True, timeout=15)

    assert contract.read_bytes() == original
    assert result.returncode == 1, result.stdout + result.stderr
    assert "contracts.ts is STALE" in result.stdout + result.stderr
    assert "added_field" in result.stdout + result.stderr
