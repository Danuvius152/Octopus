"""Reject new static imports of test cases; retain a fixed historical baseline."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("test-case-imports-baseline.json")


def case_module(module: str) -> bool:
    return module.startswith("tests.") and any(
        part.startswith("test_") or part.endswith("_test") for part in module.split(".")[1:]
    )


def imports(root: Path) -> dict[str, int]:
    found = {}
    for directory in ("server", "tests", "scripts"):
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root)
            package = relative.with_suffix("").parts[:-1]
            for node in ast.walk(ast.parse(path.read_text(), filename=str(relative))):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [(alias.name, "*") for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = list(package[:len(package) - node.level + 1]) if node.level else []
                    module = ".".join([*prefix, *node.module.split(".")]) if node.module else ".".join(prefix)
                    for alias in node.names:
                        # Both `from tests.test_x import helper` and
                        # `from tests import test_x` create a case dependency.
                        if case_module(module):
                            targets.append((module, alias.name))
                        elif case_module(f"{module}.{alias.name}"):
                            targets.append((f"{module}.{alias.name}", "*"))
                for module, name in targets:
                    if case_module(module):
                        found[f"{relative.as_posix()}:{module}:{name}"] = node.lineno
    return found


def check(root: Path = ROOT, baseline: Path = BASELINE, *, prune: bool = False) -> list[str]:
    current = imports(root)
    allowed = set(json.loads(baseline.read_text()))
    if prune:
        # This command can only shrink exceptions, never bless a new import.
        baseline.write_text(json.dumps(sorted(allowed & current.keys()), indent=2) + "\n")
    return [f"{key.split(':', 1)[0]}:{current[key]}: case-module import {key.split(':', 1)[1]}"
            for key in sorted(current.keys() - allowed)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prune-baseline", action="store_true")
    args = parser.parse_args()
    violations = check(prune=args.prune_baseline)
    if violations:
        print("\n".join(violations))
        print("Move reusable helpers/fixtures into support modules; do not grow the baseline.")
        return 1
    print("Test support imports: no new case-module dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
