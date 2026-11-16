"""Exercise the installed hook scheduler against real verification inputs."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def hooks(tmp_path_factory):
    root = tmp_path_factory.mktemp("hook-selection")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "lefthook.yml").read_text())
    config.pop("rc", None)
    for name, command in config["pre-commit"]["commands"].items():
        command["run"] = f"{shlex.quote(sys.executable)} -c {shlex.quote(f'print(\"EXECUTED:{name}\")')}"
    (root / "lefthook.yml").write_text(yaml.safe_dump(config))
    executable = shutil.which("lefthook")
    assert executable, "Install the required hook tool using scripts/setup-hooks.sh"
    return root, executable


@pytest.mark.parametrize("path", [
    "web/src/components/ChatView.tsx", "web/e2e/run.ts", "web/tests/e2eSelection.test.ts",
    "web/vite.config.ts", "web/playwright.config.ts", "web/eslint.config.js", "web/tsconfig.e2e.json",
    "web/package.json", "web/bun.lock", "web/src/lib/recordingReplay.json",
    "tests/fixtures/wiki-command-cases.json", "server/app_agent_client.js",
    "lefthook.yml", "scripts/toolchain-env.sh",
])
def test_frontend_inputs_trigger_all_existing_frontend_checks(hooks, path):
    root, executable = hooks
    changed = root / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.touch(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("LEFTHOOK")}
    result = subprocess.run([executable, "run", "pre-commit", "--file", path, "--no-auto-install", "--no-stage-fixed", "--no-tty"],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    selected = set(re.findall(r"EXECUTED:([a-z-]+)", result.stdout))
    assert {"eslint", "tsc", "vitest"} <= selected, result.stdout


def test_unrelated_document_does_not_run_product_checks(hooks):
    root, executable = hooks
    env = {key: value for key, value in os.environ.items() if not key.startswith("LEFTHOOK")}
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs/user-guide.md").touch()
    result = subprocess.run([executable, "run", "pre-commit", "--file", "docs/user-guide.md", "--no-auto-install", "--no-stage-fixed", "--no-tty"],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0 and "EXECUTED:" not in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize(("path", "expected"), [
    ("server/models.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/routers/sessions.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/routers/nested/example.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag", "contracts"}),
    ("server/db/users.py", {"ruff", "mypy", "pytest", "test-support-imports", "architecture-nag"}),
    ("tests/test_check_runner.py", {"ruff", "pytest", "test-support-imports"}),
    ("scripts/check_runner.py", {"ruff", "pytest", "test-support-imports"}),
    ("scripts/check.sh", {"ruff", "pytest", "test-support-imports"}),
    ("pyproject.toml", {"ruff", "mypy", "pytest", "test-support-imports"}),
    ("constraints-python.txt", {"ruff", "mypy", "pytest", "test-support-imports"}),
    ("docs/implemented/memory.md", {"docs-index"}),
    ("docs/plans/example/plan.md", {"docs-index"}),
])
def test_other_existing_gates_match_direct_and_nested_inputs(hooks, path, expected):
    root, executable = hooks
    changed = root / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.touch(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("LEFTHOOK")}
    result = subprocess.run([executable, "run", "pre-commit", "--file", path, "--no-auto-install", "--no-stage-fixed", "--no-tty"],
                            cwd=root, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(re.findall(r"EXECUTED:([a-z-]+)", result.stdout)) == expected, result.stdout
