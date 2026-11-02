"""Dependency closure and candidate checks, with no package installation."""

import json
import tomllib

import pytest
from packaging.markers import default_environment

from scripts.python_environment import check_environment, resolve_packages, write_lock


def package(name, version="2.0", requires=()):
    return {"name": name, "version": version, "requires": list(requires), "requires_python": ">=3.12"}


def installation(*packages):
    return {"environment": default_environment(), "executable": "/verified/bin/python", "prefix": "/verified",
            "packages": {item["name"]: item for item in packages}}


def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text('''[project]
name = "octopus"
requires-python = ">=3.12"
dependencies = ["alpha[standard]>=2"]
[project.optional-dependencies]
dev = ["checker>=2"]
[build-system]
requires = ["builder>=2"]
''')
    return tomllib.loads((tmp_path / "pyproject.toml").read_text())


def test_extra_dependencies_keep_their_own_marker_context(tmp_path):
    definition = project(tmp_path)
    observed = installation(package("alpha", requires=[
        "beta[feature]>=2; extra == 'standard'", "windows-only; sys_platform == 'win32'",
    ]), package("beta", requires=["gamma>=2; extra == 'feature'"]), package("gamma"))
    observed["environment"]["sys_platform"] = "linux"
    assert resolve_packages(definition, observed, []) == {"alpha", "beta", "gamma"}
    del observed["packages"]["gamma"]
    with pytest.raises(ValueError, match="Missing dependency: gamma"):
        resolve_packages(definition, observed, [])


def test_clean_lock_excludes_unselected_host_packages_and_runtime_does_not_need_dev_tools(tmp_path):
    project(tmp_path)
    observed = installation(package("alpha"), package("checker"), package("builder"), package("pip"), package("host-only"))
    lock = write_lock(tmp_path, observed, ["dev"], build=True)
    assert "host-only" not in lock.read_text()
    runtime = installation(package("alpha"), package("unrelated-tool", version="99.0"))
    receipt = check_environment(tmp_path, runtime, [])
    assert receipt["packages"] == {"alpha": "2.0"}
    assert receipt["unselected_packages"] == {"unrelated-tool": "99.0"}
    with pytest.raises(ValueError, match="Missing dependency: checker"):
        check_environment(tmp_path, runtime, ["dev"])


def test_candidate_check_rejects_version_drift_and_missing_optional_closure(tmp_path):
    project(tmp_path)
    observed = installation(package("alpha", requires=["beta; extra == 'standard'"]), package("beta"))
    lock = write_lock(tmp_path, observed, [])
    original = lock.read_bytes()
    observed["packages"]["beta"]["version"] = "3.0"
    with pytest.raises(ValueError, match="Dependency mismatch for beta"):
        check_environment(tmp_path, observed, [])
    assert lock.read_bytes() == original  # A candidate check cannot rewrite its policy.
    del observed["packages"]["beta"]
    with pytest.raises(ValueError, match="Missing dependency: beta"):
        check_environment(tmp_path, observed, [])


def test_candidate_check_binds_python_platform_and_pin_bytes(tmp_path):
    project(tmp_path)
    observed = installation(package("alpha"))
    lock = write_lock(tmp_path, observed, [])
    receipt = check_environment(tmp_path, observed, [])
    assert len(receipt["constraints_sha256"]) == 64
    environment = observed["environment"]
    environment["platform_machine"] = "another-architecture"
    with pytest.raises(ValueError, match="Unverified Python/platform"):
        check_environment(tmp_path, observed, [])
    assert json.loads(next(line.removeprefix("# environment: ") for line in lock.read_text().splitlines()
                           if line.startswith("# environment: ")))["platform_machine"] != "another-architecture"


@pytest.mark.parametrize("pin", ["alpha>=2", "alpha==2.*", "alpha==2.0\nalpha==2.0"])
def test_candidate_requires_exact_unambiguous_pins(tmp_path, pin):
    project(tmp_path)
    observed = installation(package("alpha"))
    lock = write_lock(tmp_path, observed, [])
    lock.write_text(lock.read_text().replace("alpha==2.0", pin))
    with pytest.raises(ValueError, match="exact dependency pin|Duplicate dependency pin"):
        check_environment(tmp_path, observed, [])
