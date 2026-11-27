#!/usr/bin/env -S octopus-git-python -I
"""Restore Git's native config environment when a view opens another repository."""

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NoReturn

_OPTIONS_WITH_VALUES = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--shallow-file", "--attr-source",
}


_DIRECTORY = os.O_PATH | os.O_DIRECTORY


def _git_metadata_file(path: str, fd: int, limit: int) -> bytes:
    entry = os.open(path, os.O_RDONLY | os.O_NONBLOCK, dir_fd=fd)
    try:
        return os.read(entry, limit) if stat.S_ISREG(os.fstat(entry).st_mode) else b""
    finally:
        os.close(entry)


def _open_common_directory(fd: int) -> int:
    try:
        stored = _git_metadata_file("commondir", fd, 4096)
    except FileNotFoundError:
        stored = b"."
    common = os.fsdecode(stored).rstrip("\r\n")
    if len(stored) == 4096 or not common or "\0" in common:
        raise FileNotFoundError("No Git common directory in the reserved repository")
    return os.open(common, _DIRECTORY, dir_fd=fd)


def _open_git_directory(root_fd: int) -> int:
    """Open the metadata directory named by this root's .git entry."""
    try:
        return os.open(".git", _DIRECTORY, dir_fd=root_fd)
    except NotADirectoryError:
        contents = _git_metadata_file(".git", root_fd, 4096)
        name = contents.removeprefix(b"gitdir: ").rstrip(b"\r\n")
        if len(contents) == 4096 or not contents.startswith(b"gitdir: ") or not name or b"\0" in name:
            raise FileNotFoundError("No Git directory in the reserved worktree") from None
        return os.open(name, _DIRECTORY, dir_fd=root_fd)


@dataclass(frozen=True)
class RepositoryBinding:
    worktree: str
    root: str
    git: str
    common: str
    original_git_dir: str | None

    def require_current(self) -> None:
        with ExitStack() as files:
            root_fd = os.open(self.worktree, _DIRECTORY)
            files.callback(os.close, root_fd)
            git_fd = _open_git_directory(root_fd)
            files.callback(os.close, git_fd)
            common_fd = _open_common_directory(git_fd)
            files.callback(os.close, common_fd)
            for observed, expected in ((root_fd, self.root), (git_fd, self.git), (common_fd, self.common)):
                if not os.path.samestat(os.fstat(observed), os.stat(expected)):
                    raise ValueError("The reserved Git repository changed before its callback")
            if self.original_git_dir is not None and not os.path.samefile(self.original_git_dir, self.git):
                raise ValueError("The reserved Git directory changed before its callback")


def _read_binding(view: Path) -> RepositoryBinding:
    return RepositoryBinding(**json.loads((view / "callback-repository.json").read_text()))


def _callback_binding_failed(view: Path) -> NoReturn:
    (view / "callback-binding-failed").touch()
    sys.exit("The reserved Git repository or hook directory changed before its callback")


def git_target(args: list[str]) -> tuple[Path | None, bool]:
    cwd = Path.cwd()
    git_dir = os.environ.get("GIT_DIR")
    options = iter(args)
    for argument in options:
        if not argument.startswith("-") or argument in {"--", "-h", "-v", "--help", "--version"}:
            break
        option, assignment, value = argument.partition("=")
        if option == "--exec-path" and not assignment:
            return None, True
        if option in _OPTIONS_WITH_VALUES:
            if not assignment:
                value = next(options, "")
            if option == "-C" and value:
                cwd = Path(os.path.realpath(cwd / value))
            elif option == "--git-dir":
                git_dir = value
    target = Path(os.path.realpath(cwd / git_dir)) if git_dir else None
    return target, False


def _advise_ignored_hook(view: Path, path: str, git: str) -> None:
    # Let Git apply its native advice configuration and translations. Use a
    # private non-executable file so reporting cannot launch the real hook.
    with TemporaryDirectory(dir=view) as directory:
        hook = Path(directory) / Path(path).name
        hook.touch(mode=0o600)
        result = subprocess.run(
            [git, "-c", f"core.hooksPath={directory}", "hook", "run", "--ignore-missing", hook.name],
            stderr=subprocess.PIPE,
        )
        sys.stderr.buffer.write(result.stderr.replace(os.fsencode(hook), os.fsencode(path)))
        if result.returncode:
            sys.exit(result.returncode if result.returncode >= 0 else 128 - result.returncode)


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    return info.st_dev, info.st_ino


def _inspect_hook(path: Path) -> tuple[bool, bool]:
    try:
        return stat.S_ISREG(path.stat().st_mode), False
    except OSError as error:
        return False, error.errno == errno.EACCES


def execute_hook(directory: str, display_path: str, git: str, args: list[str], *, shell: str = "/bin/sh") -> None:
    view = Path(os.environ["OCTOPUS_GIT_VIEW"])
    native = Path(display_path)
    try:
        expected_directory = _directory_identity(Path(directory))
        if _directory_identity(native.parent) != expected_directory:
            _callback_binding_failed(view)
    except OSError:
        _callback_binding_failed(view)

    found, denied = _inspect_hook(native)

    try:
        if found:
            binding = _read_binding(view)
            binding.require_current()
            if not os.path.samefile(".", binding.root):
                _callback_binding_failed(view)
        if _directory_identity(native.parent) != expected_directory:
            _callback_binding_failed(view)
    except (OSError, ValueError):
        _callback_binding_failed(view)

    if not found:
        if denied:
            _advise_ignored_hook(view, display_path, git)
        return

    # The kernel checks the executable it opens. Keep the native path for
    # shebang interpreters and let the shell handle ENOEXEC as Git does.
    try:
        os.execv(display_path, [display_path, *args])
    except OSError as error:
        if error.errno == errno.ENOEXEC:
            os.execv(shell, [shell, display_path, *args])
        elif error.errno == errno.EACCES and not os.access(display_path, os.X_OK):
            _advise_ignored_hook(view, display_path, git)
        elif error.errno not in {errno.ENOENT, errno.ENOTDIR} or _inspect_hook(native)[0]:
            sys.exit(f"Cannot exec '{display_path}': {error.strerror}")


def main() -> None:
    if sys.argv[1:2] == ["--execute-hook"]:
        execute_hook(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[6:], shell=sys.argv[5])
        return
    view = Path(os.environ["OCTOPUS_GIT_VIEW"])
    launcher = Path(sys.argv[0])
    if launcher.name == "callback-check":
        try:
            _read_binding(view).require_current()
        except (ValueError, OSError):
            _callback_binding_failed(view)
        return
    inside_hook = launcher.parent == view / "hook-bin"
    private_path = str(view / ("hook-bin" if inside_hook else "bin"))
    native_path = os.pathsep.join(entry for entry in os.get_exec_path() if entry != private_path)
    binary = shutil.which("git", path=native_path)
    if binary is None:
        raise FileNotFoundError("Git child executable is unavailable")
    args = sys.argv[1:]
    git_dir, exec_path_query = git_target(args)
    if inside_hook:
        binding = _read_binding(view)
        if git_dir == Path(os.path.realpath(binding.git)):
            os.environ.setdefault("GIT_COMMON_DIR", binding.common)
        elif os.environ.get("GIT_COMMON_DIR") == binding.common:
            del os.environ["GIT_COMMON_DIR"]
        os.execv(binary, ["git", *args])
    native = json.loads((view / "git-child.json").read_text())
    original = native["environment"]
    outside_view = git_dir != view / "admin"
    if os.environ.get("GIT_EXEC_PATH") == private_path and (exec_path_query or outside_view):
        os.environ["GIT_EXEC_PATH"] = original["GIT_EXEC_PATH"]
    if not exec_path_query and outside_view:
        for key, value in original.items():
            if key.startswith("GIT_CONFIG_"):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if os.environ.get("GIT_INDEX_FILE") == native["view_index"]:
            del os.environ["GIT_INDEX_FILE"]
        if os.environ.get("GIT_WORK_TREE") == ".":
            # The view command adds --work-tree=. for its pinned directory.
            # Git will parse any explicit override from this child's argv.
            del os.environ["GIT_WORK_TREE"]
        del os.environ["OCTOPUS_GIT_VIEW"]
        os.environ["PATH"] = native_path
    os.execv(binary, ["git", *args])


if __name__ == "__main__":
    main()
