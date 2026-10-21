"""Standard-library-only systemd cgroup checks, also executable with -I -S."""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ScopeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceScope:
    unit: str
    root: Path
    current: Path
    unit_path: PurePosixPath


def _mount_path(value: str) -> PurePosixPath:
    return PurePosixPath(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value))


def current_scope(unit: str, *, proc_root: Path = Path("/proc")) -> ServiceScope:
    if not unit.endswith(".service") or "/" in unit or any(c.isspace() for c in unit):
        raise ScopeError("Expected a single systemd service unit name")
    try:
        groups = [line.split(":", 2) for line in (proc_root / "self/cgroup").read_text().splitlines()]
        unified = [entry[2] for entry in groups if len(entry) == 3 and entry[:2] == ["0", ""]]
        if len(unified) != 1:
            raise ScopeError("A unique cgroup v2 membership is required")
        group = PurePosixPath(unified[0])
        if not group.is_absolute() or group.parts.count(unit) != 1 or ".." in group.parts:
            raise ScopeError(f"Current process is not unambiguously inside {unit}")
        unit_path = PurePosixPath(*group.parts[:group.parts.index(unit) + 1])
        mounts = []
        for line in (proc_root / "self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            if after.split()[0] != "cgroup2":
                continue
            fields = before.split()
            mount_root, mount = _mount_path(fields[3]), _mount_path(fields[4])
            if not mount_root.is_absolute() or not mount.is_absolute():
                raise ScopeError("Cgroup mount paths must be absolute")
            if unit_path.is_relative_to(mount_root):
                root = Path(mount) / unit_path.relative_to(mount_root)
                current = Path(mount) / group.relative_to(mount_root)
                mounts.append(ServiceScope(unit, root, current, unit_path))
        if len(mounts) != 1:
            raise ScopeError("A unique readable mount covering the service unit root is required")
        return mounts[0]
    except (OSError, ValueError, IndexError) as error:
        raise ScopeError(f"Cannot resolve cgroup v2 service scope: {error}") from error


def check_prestart(unit: str, *, proc_root: Path = Path("/proc")) -> ServiceScope:
    scope = current_scope(unit, proc_root=proc_root)
    try:
        count = int((scope.root / "pids.current").read_text())
        members = [int(pid) for pid in (scope.current / "cgroup.procs").read_text().split()]
    except (OSError, ValueError) as error:
        raise ScopeError(f"Cannot verify service task membership: {error}") from error
    if count != 1 or members != [os.getpid()]:
        raise ScopeError(f"Service scope still contains other tasks: pids.current={count}; refusing startup")
    return scope


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an empty service scope before starting Octopus")
    parser.add_argument("--unit", required=True)
    args = parser.parse_args()
    try:
        scope = check_prestart(args.unit)
    except ScopeError as error:
        print(str(error), file=sys.stderr)
        return 78
    print(f"Verified service scope: {scope.unit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
