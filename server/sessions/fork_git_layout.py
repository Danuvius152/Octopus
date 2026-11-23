"""Verify that a literal fork copy keeps Git administration within its own tree."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from codecs import escape_decode
from collections import deque
from collections.abc import Iterator
from pathlib import Path


class GitLayoutError(Exception):
    """Git administration could not be confirmed to stay in the copy."""


def require_independent_git_layout(source: Path, copy: Path) -> None:
    """Inspect existing bytes as they would be laid out at the copy path.

    Pass the actual destination as both paths after copying. Regular working
    tree symlinks are preserved and are outside Git administration.
    """
    try:
        _GitLayout(source, copy).check()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise GitLayoutError("Git layout inspection failed") from error


def _raise_walk_error(error: OSError) -> None:
    raise error


def _mode(path: Path) -> int:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return 0


def _looks_like_git_dir(entries: set[str]) -> bool:
    return "HEAD" in entries and bool({"commondir", "objects"} & entries)


_QUOTED_ALTERNATE = re.compile(rb'"(?:[^"\\]|\\(?:[abtnvfr"\\]|[0-3][0-7]{2}))*"')
_GIT_HEAD_PREFIX = re.compile(r"ref:[ \t\n\r]*refs/|[a-fA-F0-9]{40}")


def _alternate_paths(contents: str) -> Iterator[str]:
    data = os.fsencode(contents).partition(b"\0")[0]
    start = 0
    while start < len(data):
        quoted = _QUOTED_ALTERNATE.match(data, start)
        if quoted is not None:
            path = escape_decode(quoted[0][1:-1])[0]
            end = quoted.end()
        else:
            end = data.find(b"\n", start)
            if end < 0:
                end = len(data)
            path = b"" if data[start] == ord("#") else data[start:end]
        # Git consumes one separator byte after a quoted path or a line.
        start = end + 1
        if path:
            yield os.fsdecode(path)


class _GitLayout:
    def __init__(self, source: Path, copy: Path) -> None:
        self.source = source
        self.copy = copy
        self.storage_dirs: set[Path] = set()
        self.object_dirs: set[Path] = set()

    def check(self) -> None:
        checkouts: dict[Path, Path] = {}
        possible_git_dirs: list[Path] = []
        for directory, subdirs, files in os.walk(self.source, onerror=_raise_walk_error):
            path = Path(directory)
            entries = set(subdirs) | set(files)
            if ".git" in entries:
                git_dir = self._checkout_git_dir(path)
                if git_dir is not None:
                    checkouts[path] = git_dir
            if _looks_like_git_dir(entries):
                possible_git_dirs.append(path)

        # Register explicit checkouts first. Their private storage can turn up
        # before the checkout in the filesystem walk and need not itself be
        # usable as a checkout directory.
        attached = set(checkouts.values())
        standalone = [
            path for path in possible_git_dirs
            if path not in attached and self._discover_git_directory(path)
        ]
        if self.source not in checkouts and self.source not in standalone and self.source not in attached:
            self._check_no_inherited_git(self.source)
            if self.copy != self.source:
                parent = next(parent for parent in self.copy.parents if parent.is_dir())
                self._check_no_inherited_git(parent)

        common_dirs = {git_dir: self._git_storage(git_dir) for git_dir in sorted(attached | set(standalone))}
        module_roots = {
            self._reference(common, "modules", "submodule storage", missing_ok=True)
            for common in set(common_dirs.values())
        }

        for worktree, git_dir in checkouts.items():
            self._check_repository(worktree, git_dir, common_dirs[git_dir])
        for git_dir in standalone:
            retained_module = any(
                git_dir != root and git_dir.is_relative_to(root) for root in module_roots
            )
            self._check_repository(
                git_dir, git_dir, common_dirs[git_dir],
                standalone=True, retained_module=retained_module,
            )

    @staticmethod
    def _check_no_inherited_git(cwd: Path) -> None:
        # Discovery needs a .git entry or a bare repository's HEAD. When
        # a marker exists, let Git interpret it and apply its search rules.
        if not any(_mode(path / ".git") or _mode(path / "HEAD") for path in (cwd, *cwd.parents)):
            return
        found = _run_git(cwd, "rev-parse", "--absolute-git-dir")
        if found.returncode == 128 and found.stderr.startswith((
            b"fatal: not a git repository (or any of the parent directories): .git\n",
            b"fatal: not a git repository (or any parent up to mount point ",
        )):
            return
        _git_output(cwd, found)
        raise GitLayoutError("the working directory inherits Git administration from an ancestor")

    def _resolve(self, path: Path, reference: str, *, missing_ok: bool = False) -> Path:
        """Follow references component by component in the projected tree."""
        if not reference or "\0" in reference:
            raise GitLayoutError("an empty or invalid path could not be resolved")

        current = self.source
        pending = deque(path.relative_to(self.source).parts)
        links = 0

        def add(target: Path) -> None:
            nonlocal current
            if target.is_absolute():
                if not target.is_relative_to(self.copy):
                    raise GitLayoutError("an absolute path points outside the copy")
                target = target.relative_to(self.copy)
                current = self.source
            pending.extendleft(reversed(target.parts))

        # Resolve the base first. Git interprets relative references from the
        # directory selected by the containing path, including its symlinks.
        if Path(reference).is_absolute():
            pending.clear()
            add(Path(reference))
        else:
            pending.extend(Path(reference).parts)
        while pending:
            component = pending.popleft()
            if component == "..":
                if current == self.source:
                    raise GitLayoutError("a relative path points outside the copy")
                current = current.parent
                continue
            current /= component
            mode = _mode(current)
            if stat.S_ISLNK(mode):
                links += 1
                if links > 40:
                    raise GitLayoutError("a symbolic link could not be resolved")
                target = current.readlink()
                current = current.parent
                add(target)
            elif not mode:
                if not missing_ok or ".." in pending:
                    raise GitLayoutError("a referenced path is missing")
                return current.joinpath(*pending)
            elif pending and not stat.S_ISDIR(mode):
                raise GitLayoutError("a referenced parent is not a directory")
        return current

    def _reference(self, base: Path, raw: str, name: str, *, missing_ok: bool = False) -> Path:
        try:
            return self._resolve(base, raw, missing_ok=missing_ok)
        except GitLayoutError as error:
            raise GitLayoutError(f"{name}: {error}") from error

    @staticmethod
    def _read(path: Path, *, limit: int = -1) -> str:
        # Special files are not Git metadata. The nonblocking open also
        # protects the probe if a regular file is concurrently replaced.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise GitLayoutError("Git metadata is not a regular file")
            return os.fsdecode(stream.read(limit))

    def _checkout_git_dir(self, worktree: Path) -> Path | None:
        marker = self._reference(worktree, ".git", ".git")
        if marker.is_dir():
            return marker if self._discover_git_directory(marker) else None
        gitfile = self._read(marker).rstrip("\r\n")
        if not gitfile.startswith("gitdir: "):
            raise GitLayoutError(".git is not a valid gitfile")
        return self._reference(worktree, gitfile.removeprefix("gitdir: "), "gitfile")

    def _discover_git_directory(self, git_dir: Path) -> bool:
        # A link broken at the source can select live storage after copying.
        # Check those paths once HEAD can identify the directory to Git.
        changed = False
        if self._has_git_head(git_dir / "HEAD"):
            common = self._common_directory(git_dir)
            paths = ((git_dir, "HEAD"), (common, "objects"), (common, "refs"))
            for base, name in paths:
                projected = self._reference(base, name, f"Git symlink {name}", missing_ok=True)
                if self.copy != self.source and projected.exists() and projected != (base / name).resolve():
                    changed = True
        if _is_git_directory(git_dir):
            return True
        if changed:
            raise GitLayoutError("Git discovery resolves different storage at the destination")
        return False

    def _has_git_head(self, head: Path) -> bool:
        mode = _mode(head)
        if stat.S_ISLNK(mode):
            return os.readlink(head).startswith("refs/")
        if not mode or stat.S_ISDIR(mode):
            return False
        # Git's discovery recognizes a symbolic ref or an object ID prefix in
        # the first 255 bytes. It still decides actual repository membership.
        return _GIT_HEAD_PREFIX.match(self._read(head, limit=255)) is not None

    def _check_repository(
        self, worktree: Path, git_dir: Path, common: Path, *,
        standalone: bool = False, retained_module: bool = False,
    ) -> None:
        configured = self._configured_worktree(git_dir, common, missing_ok=retained_module)
        if configured is not None and not configured.is_dir():
            # A removed submodule can retain a relative core.worktree. Its
            # storage was checked above, including any links and alternates.
            if retained_module and not configured.exists():
                return
            raise GitLayoutError("core.worktree is not a directory")
        self._probe_worktree(
            worktree, git_dir, standalone=standalone, allow_no_worktree=standalone and configured is None,
        )

    def _probe_worktree(
        self, worktree: Path, git_dir: Path, *, standalone: bool, allow_no_worktree: bool,
    ) -> None:
        # A standalone store can share a directory with a checkout. Select the
        # store directly while preserving Git's lack of an implicit worktree
        # when it discovers a store from its administration directory.
        selection = (f"--git-dir={git_dir}",) if standalone else ()

        def run_probe(*args: str) -> subprocess.CompletedProcess[bytes]:
            return _run_git(
                worktree, *selection, "rev-parse", "--path-format=absolute", *args,
                implicit_worktree=not standalone,
            )

        def probe(*args: str) -> str:
            return _git_output(worktree, run_probe(*args)).removesuffix("\n")

        if Path(probe("--absolute-git-dir")).resolve(strict=True) != git_dir:
            raise GitLayoutError("Git did not find the expected worktree administration")
        self._require_observed_path(probe("--git-common-dir"))
        self._require_observed_path(probe("--git-path", "objects"))
        self._require_observed_path(probe("--git-path", "index"))
        bare = probe("--is-bare-repository")
        if bare == "false":
            top = run_probe("--show-toplevel")
            if not (
                allow_no_worktree and top.returncode == 128
                and top.stderr == b"fatal: this operation must be run in a work tree\n"
            ):
                self._require_observed_path(_git_output(worktree, top).removesuffix("\n"))
        elif bare not in {"true", "false"}:
            raise GitLayoutError("Git did not report whether the repository has a worktree")

    def _require_observed_path(self, value: str) -> None:
        path = Path(value)
        if not path.is_absolute() or not path.resolve().is_relative_to(self.source):
            raise GitLayoutError("Git resolved worktree administration outside the inspected tree")

    def _common_directory(self, git_dir: Path) -> Path:
        if _mode(git_dir / "commondir"):
            location = self._reference(git_dir, "commondir", "commondir")
            return self._reference(git_dir, self._read(location).rstrip("\r\n"), "commondir")
        return git_dir

    def _git_storage(self, git_dir: Path) -> Path:
        if not git_dir.is_dir() or not _mode(git_dir / "HEAD"):
            raise GitLayoutError("Git administration is missing a directory or HEAD")
        common = self._common_directory(git_dir)
        if _mode(git_dir / "gitdir"):
            raise GitLayoutError("linked worktree administration has a gitdir back-reference")
        worktrees = self._reference(common, "worktrees", "worktrees", missing_ok=True)
        if worktrees.exists() and any(worktrees.iterdir()):
            raise GitLayoutError("the repository has registered linked worktrees")

        for directory in {git_dir, common}:
            self._check_storage_links(directory)
        self._check_objects(self._reference(common, "objects", "object store"))
        return common

    def _check_storage_links(self, root: Path) -> None:
        remaining = [root]
        while remaining:
            directory = remaining.pop()
            if directory in self.storage_dirs:
                continue
            self.storage_dirs.add(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        target = self._reference(
                            directory, entry.name, f"Git symlink {entry.name}",
                            missing_ok=entry.name == "HEAD" and _is_head_ref(Path(entry.path)),
                        )
                        if target.is_dir():
                            remaining.append(target)
                    elif entry.is_dir(follow_symlinks=False):
                        remaining.append(Path(entry.path))

    def _check_objects(self, root: Path) -> None:
        remaining = [root]
        while remaining:
            objects = remaining.pop()
            if objects in self.object_dirs:
                continue
            self.object_dirs.add(objects)
            self._check_storage_links(objects)
            if not _mode(objects / "info" / "alternates"):
                continue
            alternates = self._reference(objects, "info/alternates", "object alternates")
            for raw in _alternate_paths(self._read(alternates)):
                remaining.append(self._reference(objects, raw, "object alternates"))

    def _configured_worktree(self, git_dir: Path, common: Path, *, missing_ok: bool) -> Path | None:
        # Git sets core.worktree during repository setup, using the direct
        # config files without includes. Later config queries can see values
        # from includes that did not select the actual worktree.
        common_config = common / "config"
        config = self._read_config(common_config)
        if "core.repositoryformatversion" not in config or int(_git_config(
            common_config, "--type=int", "--get", "core.repositoryformatversion",
        )) < 0:
            return None
        if "extensions.worktreeconfig" in config and _git_config(
            common_config,
            "--type=bool", "--get", "extensions.worktreeconfig",
        ) == "true\n":
            config.update(self._read_config(git_dir / "config.worktree"))
        elif _mode(git_dir / "commondir"):
            return None  # Common config cannot set the individual worktree root.

        if "core.worktree" in config:
            return self._reference(git_dir, config["core.worktree"], "core.worktree", missing_ok=missing_ok)
        return None

    def _read_config(self, path: Path) -> dict[str, str]:
        if not _mode(path):
            return {}
        config = self._reference(path.parent, path.name, "Git config")
        if not stat.S_ISREG(config.stat().st_mode):
            raise GitLayoutError("Git config is not a regular file")
        values = {}
        for entry in _git_config(config, "--null", "--list").split("\0"):
            if entry:
                key, _, value = entry.partition("\n")
                values[key] = value
        return values


def _git_config(path: Path, *args: str) -> str:
    # An explicit file read must not try discovering a checkout for retained
    # submodule storage whose former worktree no longer exists.
    return _git(Path("/"), f"--git-dir={os.devnull}", "config", "--no-includes", "--file", str(path), *args)


def _is_git_directory(path: Path) -> bool:
    result = _run_git(Path("/"), f"--git-dir={os.devnull}", "rev-parse", "--resolve-git-dir", str(path))
    if result.returncode == 128 and result.stderr == b"fatal: not a gitdir '" + os.fsencode(path) + b"'\n":
        return False
    _git_output(path, result)
    return True


def _is_head_ref(path: Path) -> bool:
    target = os.readlink(path)
    if not target.startswith("refs/"):
        return False
    result = _run_git(Path("/"), "check-ref-format", target)
    return result.returncode == 0 and not result.stderr


def _run_git(cwd: Path, *args: str, implicit_worktree: bool = True) -> subprocess.CompletedProcess[bytes]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    if not implicit_worktree:
        env["GIT_IMPLICIT_WORK_TREE"] = "0"
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, check=False, timeout=10,
    )


def _git_output(cwd: Path, result: subprocess.CompletedProcess[bytes]) -> str:
    if result.returncode or result.stderr:
        raise GitLayoutError(f"Git could not inspect {cwd.name or 'repository configuration'!r}")
    return os.fsdecode(result.stdout)


def _git(cwd: Path, *args: str) -> str:
    return _git_output(cwd, _run_git(cwd, *args))
