#!/usr/bin/env python3
"""Freeze a clean Python installation or verify the candidate's actual interpreter.

The verifier uses the dev extra's packaging parser. The inspected runtime needs
only the declared runtime dependencies; it does not import this verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

_INSPECT = """
import json, os, platform, sys
from importlib import metadata
version = platform.python_version()
environment = {
    'implementation_name': sys.implementation.name, 'implementation_version': version,
    'os_name': os.name, 'platform_machine': platform.machine(),
    'platform_python_implementation': platform.python_implementation(),
    'platform_release': platform.release(), 'platform_system': platform.system(),
    'platform_version': platform.version(), 'python_full_version': version,
    'python_version': '.'.join(platform.python_version_tuple()[:2]), 'sys_platform': sys.platform,
}
print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix, 'environment': environment,
    'packages': [{'name': d.metadata['Name'], 'version': d.version,
                  'requires': d.requires or [], 'requires_python': d.metadata.get('Requires-Python')}
                 for d in metadata.distributions()]}))
"""
_ENVIRONMENT_KEYS = ("implementation_name", "python_full_version", "python_version", "sys_platform", "platform_machine")


def inspect_environment(python: str) -> dict[str, Any]:
    """Ask that interpreter, without importing the service or changing packages."""
    observed = json.loads(subprocess.check_output([python, "-c", _INSPECT], text=True, timeout=30))
    packages = {}
    for package in observed["packages"]:
        name = canonicalize_name(package["name"])
        if name in packages and packages[name]["version"] != package["version"]:
            raise ValueError(f"Ambiguous installed dependency: {name}")
        packages[name] = package
    observed["packages"] = packages
    return observed


def resolve_packages(project: dict[str, Any], observed: dict[str, Any], extras: list[str], *, build: bool = False) -> set[str]:
    """Verify the selected base/extra closure, including dependency markers."""
    definition = project["project"]
    environment, installed = observed["environment"], observed["packages"]
    version = environment["python_full_version"]
    if not SpecifierSet(definition.get("requires-python", "")).contains(version, prereleases=True):
        raise ValueError(f"Project does not support Python {version}")
    requirements = list(definition.get("dependencies", []))
    for extra in extras:
        optional = definition.get("optional-dependencies", {})
        if extra not in optional:
            raise ValueError(f"Unknown project extra: {extra}")
        requirements.extend(optional[extra])
    if build:
        requirements.extend(project.get("build-system", {}).get("requires", ["setuptools>=40.8.0"]))
        requirements.append("pip")
    pending = [Requirement(value) for value in requirements]
    visited: dict[str, set[str]] = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({**environment, "extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        package = installed.get(name)
        if package is None:
            raise ValueError(f"Missing dependency: {requirement}")
        if requirement.url or not requirement.specifier.contains(package["version"], prereleases=True):
            raise ValueError(f"Installed dependency does not satisfy {requirement}: {package['version']}")
        if not SpecifierSet(package.get("requires_python") or "").contains(version, prereleases=True):
            raise ValueError(f"Installed dependency {name} does not support Python {version}")
        requested = {"", *requirement.extras}
        new = requested - visited.get(name, set())
        if not new:
            continue
        visited.setdefault(name, set()).update(requested)
        for raw in package["requires"]:
            child = Requirement(raw)
            if child.marker is None or any(child.marker.evaluate({**environment, "extra": extra}) for extra in new):
                # This marker was evaluated in its owning package's extras,
                # not the child's extra context on the next iteration.
                child.marker = None
                pending.append(child)
    return set(visited)


def write_lock(project: Path, observed: dict[str, Any], extras: list[str], *, build: bool = False) -> Path:
    definition = tomllib.loads((project / "pyproject.toml").read_text())
    selected = resolve_packages(definition, observed, extras, build=build)
    name = canonicalize_name(definition["project"]["name"])
    environment = {key: observed["environment"][key] for key in _ENVIRONMENT_KEYS}
    lines = ["# Generated by scripts/python_environment.py from a clean installation.",
             "# Resolved installation inputs; Wiki parsing inputs remain in constraints-wiki.txt.",
             "# environment: " + json.dumps(environment, sort_keys=True),
             "# resolution: " + json.dumps({"extras": sorted(extras), "build": build}, sort_keys=True)]
    lines.extend(f"{key}=={observed['packages'][key]['version']}" for key in sorted(selected) if key != name)
    path = project / "constraints-python.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def check_environment(project: Path, observed: dict[str, Any], extras: list[str], *, build: bool = False) -> dict[str, Any]:
    source = (project / "pyproject.toml").read_bytes()
    definition = tomllib.loads(source.decode())
    selected = resolve_packages(definition, observed, extras, build=build)
    raw = (project / "constraints-python.txt").read_bytes()
    pins, environment = {}, None
    for line in raw.decode().splitlines():
        if line.startswith("# environment: "):
            environment = json.loads(line.removeprefix("# environment: "))
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        if (requirement.url or requirement.marker or requirement.extras or len(specifiers) != 1
                or specifiers[0].operator != "==" or "*" in specifiers[0].version):
            raise ValueError(f"Expected an exact dependency pin: {line}")
        name = canonicalize_name(requirement.name)
        if name in pins:
            raise ValueError(f"Duplicate dependency pin: {name}")
        pins[name] = specifiers[0].version
    actual_environment = {key: observed["environment"][key] for key in _ENVIRONMENT_KEYS}
    if environment != actual_environment:
        raise ValueError(f"Unverified Python/platform combination: {actual_environment}")
    project_name = canonicalize_name(definition["project"]["name"])
    # Check the selected project's complete closure. A shared interpreter may
    # also contain unrelated tools; record them without claiming they belong
    # to this runtime or requiring production to install dev/build packages.
    for name in sorted(selected):
        actual = observed["packages"][name]["version"]
        if pins.get(name) != actual:
            raise ValueError(f"Dependency mismatch for {name}: installed {actual}, pinned {pins.get(name)}")
    return {"python": str(Path(observed["executable"]).resolve()), "prefix": str(Path(observed["prefix"]).resolve()), "environment": actual_environment,
            "extras": sorted(extras), "build": build, "packages": {
                name: observed["packages"][name]["version"] for name in sorted(selected)},
            "unselected_packages": {name: package["version"] for name, package in sorted(observed["packages"].items())
                                    if name != project_name and name not in selected},
            "constraints_sha256": hashlib.sha256(raw).hexdigest(), "project_sha256": hashlib.sha256(source).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("lock", "check"))
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    observed = inspect_environment(args.python)
    if args.operation == "lock":
        write_lock(args.project, observed, args.extra, build=args.build)
    print(json.dumps(check_environment(args.project, observed, args.extra, build=args.build), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from error
