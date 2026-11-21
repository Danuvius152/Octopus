"""Exercise the installed hook scheduler against real verification inputs."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
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
    "web/package.json", "web/bun.lock", "web/src/lib/recordingReplay.json",
    "tests/fixtures/wiki-command-cases.json", "server/app_agent_client.js",
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
                 "scripts/check_runner.py", "scripts/python_environment.py", "scripts/toolchain-env.sh"):
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
