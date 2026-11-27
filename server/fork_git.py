"""Owned Git subprocess calls for session anchors and file revert."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import weakref
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, TYPE_CHECKING

from .aio import complete_cleanup
from .db.base import DatabaseBusy
from .git_view_child import (
    _DIRECTORY,
    RepositoryBinding,
    _git_metadata_file,
    _open_common_directory,
    _open_git_directory,
)
from .process_groups import group_exists, group_has_live_members_after_kill

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)

_RETIRE_TIMEOUT = 2.0
_POLL = 0.02
_ROOT_LOOKUP = ("rev-parse", "--show-toplevel")
_REPOSITORY_ENV = (
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
_VIEW_CONFIG_ENV = (
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL",
)
_FILTER_SHELL_CHARS = frozenset("|&;<>()$`\\\"' \t\n*?[#~=%")

type DirectoryIdentity = tuple[int, int]
type GitRepositoryIdentity = tuple[DirectoryIdentity, DirectoryIdentity]


@dataclass(frozen=True)
class GitResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_failed: bool = False
    binding_failed: bool = False
    configuration: GitConfigurationResult | None = None

    @property
    def started(self) -> bool:
        return self.returncode is not None

    @property
    def verified_success(self) -> bool:
        return self.returncode == 0 and not self.output_failed and not self.binding_failed

    @property
    def failed(self) -> bool:
        return self.timed_out or not self.verified_success

    def diagnostic(self) -> str:
        if self.timed_out:
            return "git timed out"
        if self.output_failed:
            return "git output could not be collected"
        if self.binding_failed:
            return "the reserved Git repository changed"
        return self.stderr


@dataclass(frozen=True)
class GitConfigurationResult:
    phase: str
    exit_code: int | None
    output_failed: bool


@dataclass(frozen=True)
class GitConfiguration:
    resolved: str
    primary: str
    executable: str
    exec_path: str
    child_exec_dir: Path
    shell: str | None
    hooks: Path | None


@dataclass(frozen=True)
class Worktree:
    path: Path
    identity: DirectoryIdentity | None
    ancestors: tuple[DirectoryIdentity, ...] = ()
    repository: GitRepositoryIdentity | None = None

    @property
    def key(self) -> tuple[int, int] | Path:
        return self.identity if self.identity is not None else self.path

    def require_current(self, working_dir: str) -> None:
        if not self.matches(working_dir):
            raise WorktreeChanged("The reserved Git worktree moved or was replaced")

    def matches(self, working_dir: str) -> bool:
        try:
            return worktree_for(working_dir).key == self.key
        except WorktreeChanged:
            return False

    def require_open_directory(self, fd: int) -> None:
        ancestry, git_root, repository = _worktree_ancestry(fd)
        identity = ancestry[git_root or 0]
        if identity != self.identity or repository != self.repository:
            raise WorktreeChanged("The opened directory is outside the reserved Git worktree")

    def contains_retirement_root(self, original: Worktree) -> bool:
        return original.identity in self.ancestors if original.identity is not None else original.key == self.key


class WorktreeChanged(RuntimeError):
    pass


def matches_worktree(working_dir: str, identity: object, *, worktree: Worktree | None = None) -> bool:
    if not isinstance(identity, list):
        return True
    try:
        current = (worktree if worktree is not None else worktree_for(working_dir)).identity
    except WorktreeChanged:
        return False
    return current is not None and list(current) == identity


def require_rewind_worktree(working_dir: str, metadata: str | None, *, worktree: Worktree | None = None) -> None:
    """Check the prepared rewind's binding at each session admission boundary."""
    try:
        value = json.loads(metadata or "{}")
    except json.JSONDecodeError:
        return
    identity = value.get("rewind_worktree_id") if isinstance(value, dict) else None
    if not matches_worktree(working_dir, identity, worktree=worktree):
        raise WorktreeChanged("The reserved Git worktree moved or cannot be verified")


def _directory_identity(fd: int) -> DirectoryIdentity:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _has_git_head(fd: int) -> bool:
    path = "HEAD"
    mode = os.stat(path, dir_fd=fd, follow_symlinks=False).st_mode
    if stat.S_ISLNK(mode):
        return os.readlink(path, dir_fd=fd).startswith("refs/")
    if not stat.S_ISREG(mode):
        return False
    return re.match(rb"(?:ref:\s*refs/|[0-9a-fA-F]{40})", _git_metadata_file(path, fd, 256)) is not None


def _require_common_directory(git_fd: int, common_fd: int) -> None:
    observed = _open_common_directory(git_fd)
    try:
        if _directory_identity(observed) != _directory_identity(common_fd):
            raise WorktreeChanged("The reserved Git common directory changed")
    finally:
        os.close(observed)


def _fd_path(fd: int) -> Path:
    return Path(f"/proc/{os.getpid()}/fd/{fd}")


def _cleanup_git_view(directory: TemporaryDirectory[str]) -> None:
    try:
        directory.cleanup()
    except OSError:
        logger.warning("Git directory view could not be removed: %s", directory.name, exc_info=True)


def _git_directory_view(
    git_fd: int, common_fd: int, files: ExitStack, config: GitConfiguration, binding: RepositoryBinding,
) -> tuple[Path, dict[str, str]]:
    """Give tree walkers a native commondir scoped to their own repository."""
    view = TemporaryDirectory(prefix="octopus-git-")
    files.callback(_cleanup_git_view, view)
    root = Path(view.name).resolve()
    common, admin = root / "common", root / "admin"
    common.mkdir()
    admin.mkdir()
    # Git canonicalizes the common directory. Keep that directory private
    # and resolve its entries relative to the verified common descriptor.
    for name in os.listdir(_fd_path(common_fd)):
        if name != "config":
            (common / name).symlink_to(_fd_path(common_fd) / name)
    for name in os.listdir(_fd_path(git_fd)):
        if name not in {"HEAD", "commondir", "config.worktree", "index"}:
            (admin / name).symlink_to(_fd_path(git_fd) / name)
    head_path = _fd_path(git_fd) / "HEAD"
    if head_path.is_symlink():
        head = b"ref: " + os.fsencode(os.readlink(head_path)) + b"\n"
        (admin / "HEAD").write_bytes(head)
    else:
        source = os.open("HEAD", os.O_RDONLY | os.O_NONBLOCK, dir_fd=git_fd)
        with os.fdopen(source, "rb") as source_head, (admin / "HEAD").open("wb") as dest:
            if not stat.S_ISREG(os.fstat(source).st_mode):
                raise WorktreeChanged("The reserved Git HEAD changed")
            shutil.copyfileobj(source_head, dest)
    (admin / "commondir").write_text("../common\n")
    # The index lock and target belong to the same original directory.
    (root / "callback-repository.json").write_text(json.dumps(asdict(binding)))
    hooks = config.hooks
    wrappers = None
    if hooks is not None:
        pinned_hooks = hooks
        # Git uses a native absolute hook path when a commondir locator or
        # gitfile participates in repository discovery.
        use_dot_git = binding.original_git_dir is None and not (_fd_path(git_fd) / "commondir").is_file()
        for reserved in (Path(binding.git), Path(binding.common)):
            native = Path(os.readlink(reserved))
            if hooks.is_relative_to(reserved):
                hooks = (Path(".git") if use_dot_git else native) / hooks.relative_to(reserved)
                break
            if hooks.is_relative_to(native):
                pinned_hooks = reserved / hooks.relative_to(native)
                break
        else:
            if not hooks.is_absolute():
                pinned_hooks = (Path(binding.git) / hooks.relative_to(".git") if hooks.is_relative_to(".git")
                                else Path(binding.root) / hooks)
        try:
            hooks_fd = os.open(pinned_hooks, _DIRECTORY)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            # A hook may be installed after the command has started.
            pass
        else:
            files.callback(os.close, hooks_fd)
            pinned_hooks = _fd_path(hooks_fd)
        wrappers = _write_callback_hooks(root, hooks, pinned_hooks, binding, config)
    return admin, _git_config_view(admin, config, binding, wrappers)


def _config_quote(value: str) -> str:
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\t", "\\t").replace("\b", "\\b")) + '"'


def _config_records(observed: str) -> Iterator[tuple[str, str, str | None]]:
    records = observed.removesuffix("\0").split("\0") if observed else []
    for scope, entry in zip(records[::2], records[1::2], strict=True):
        key, explicit, value = entry.partition("\n")
        yield scope, key, value if explicit else None


def _config_entry(key: str, value: str | None) -> str:
    section, _, variable = key.partition(".")
    if "." in variable:
        subsection, variable = variable.rsplit(".", 1)
        section += " " + _config_quote(subsection)
    assignment = f" = {_config_quote(value)}" if value is not None else ""
    return f"[{section}]\n\t{variable}{assignment}\n"


def _git_parameter_quote(value: str) -> str:
    # Match Git's shell-quoted GIT_CONFIG_PARAMETERS representation.
    return "'" + value.replace("'", "'\\''").replace("!", "'\\!'") + "'"


def _filter_command_kind(key: str, value: str) -> str | None:
    section, _, mode = key.rpartition(".")
    if not section.startswith("filter.") or mode not in {"clean", "smudge", "process"}:
        return None
    return "executable" if _FILTER_SHELL_CHARS.isdisjoint(value) else "shell"


def _write_callback_environment(
    path: Path, index: Path, native: dict[str, str | None], original_git_dir: str | None,
) -> None:
    lines = [
        "unset GIT_WORK_TREE OCTOPUS_GIT_VIEW",
        f'if [ "${{GIT_INDEX_FILE-}}" = {shlex.quote(str(index))} ]; then unset GIT_INDEX_FILE; fi',
    ]
    for key, value in {**native, "GIT_DIR": original_git_dir}.items():
        lines.append(f"export {key}={shlex.quote(value)}" if value is not None else f"unset {key}")
    path.write_bytes(os.fsencode("\n".join(lines) + "\n"))


def _write_callback_hooks(
    view: Path, directory: Path, pinned: Path, binding: RepositoryBinding, config: GitConfiguration,
) -> Path:
    hooks = view / "callback-hooks"
    hooks.mkdir()
    launcher = Path(__file__).with_name("git_view_child.py")
    hook_bin = view / "hook-bin"
    hook_bin.mkdir()
    (hook_bin / "git").symlink_to(launcher)
    (hook_bin / "octopus-git-python").symlink_to(sys.executable)
    # A Git invoked by absolute path skips our launcher. The default
    # repository must keep its common directory when Git uses a locator.
    common_env = (
        f"export GIT_COMMON_DIR={shlex.quote(binding.common)}"
        if binding.original_git_dir is not None or not os.path.samefile(binding.git, binding.common)
        else "unset GIT_COMMON_DIR"
    )
    # These are the hook sites used by the Git index, ref and checkout writes.
    # Each dispatcher checks for its hook at invocation, after earlier hooks
    # have had the opportunity to install, replace or remove it.
    for name in ("post-checkout", "post-index-change", "reference-transaction"):
        hook = directory / name
        wrapper = hooks / name
        wrapper.write_bytes(os.fsencode(
            '#!/bin/sh\n'
            '. "$OCTOPUS_GIT_VIEW/native-callback-env"\n'
            f'export GIT_DIR={shlex.quote(binding.git)}\n{common_env}\n'
            f'export OCTOPUS_GIT_VIEW={shlex.quote(str(view))} PATH={shlex.quote(str(hook_bin))}:"$PATH"\n'
            f'exec {shlex.quote(sys.executable)} -I {shlex.quote(str(launcher))}'
            f' --execute-hook {shlex.quote(str(pinned))} {shlex.quote(str(hook))} {shlex.quote(config.executable)}'
            f' {shlex.quote(config.shell or "/bin/sh")} "$@"\n',
        ))
        wrapper.chmod(0o700)
    return hooks


def _git_config_view(
    admin: Path, config: GitConfiguration, binding: RepositoryBinding, wrappers: Path | None,
) -> dict[str, str]:
    """Replay resolved scopes for this view and let child repositories resolve their own."""
    groups: dict[str, list[str]] = {scope: [] for scope in ("system", "global", "local", "worktree")}
    command: list[str] = []
    format_settings = []
    view = admin.parent
    index = Path(binding.git) / "index"
    native: dict[str, str | None] = {key: os.environ.get(key) for key in _VIEW_CONFIG_ENV}
    native["GIT_EXEC_PATH"] = config.exec_path
    native["PATH"] = os.pathsep.join((str(config.child_exec_dir), os.environ.get("PATH", os.defpath)))
    _write_callback_environment(view / "native-callback-env", index, native, binding.original_git_dir)
    filter_environment = (
        '"$OCTOPUS_GIT_VIEW/bin/callback-check" || exit 1\n'
        '. "$OCTOPUS_GIT_VIEW/native-callback-env"\n'
    )
    for scope, key, value in _config_records(config.primary):
        if scope == "local" and (key.startswith("extensions.") or key in {
            "core.repositoryformatversion", "core.bare", "core.worktree",
        }):
            format_settings.append(_config_entry(key, value))
    for scope, key, value in _config_records(config.resolved):
        if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            continue
        if value:
            kind = _filter_command_kind(key, value)
            if kind == "executable":
                value = filter_environment + f"exec {shlex.quote(value)}"
            elif kind == "shell":
                value = f'. "$OCTOPUS_GIT_VIEW/native-shell-filter"\n{value}'
        if scope == "command":
            entry = _git_parameter_quote(key)
            if value is not None:
                entry += "=" + _git_parameter_quote(value)
            command.append(entry)
        elif scope in groups:
            groups[scope].append(_config_entry(key, value))

    local = admin.parent / "resolved-local.config"
    local.write_bytes(os.fsencode("".join(groups["local"])))
    # Early format detection reads only this file. Runtime settings keep
    # their original order in the resolved include.
    format_settings.append(_config_entry("include.path", str(local)))
    (admin.parent / "common" / "config").write_bytes(os.fsencode("".join(format_settings)))
    (admin / "config.worktree").write_bytes(os.fsencode("".join(groups["worktree"])))

    if config.shell is not None:
        (view / "native-shell-filter").write_bytes(os.fsencode(
            # Git puts the expanded filter command in $0. The first line
            # above is ours; pass the remaining text back as script and $0.
            'octopus_filter="${0#*\'\n\'}"\n'
            + filter_environment
            + f'exec {shlex.quote(config.shell)} -c "$octopus_filter" "$octopus_filter"\n',
        ))
    if wrappers is not None:
        command.append(_git_parameter_quote("core.hooksPath") + "=" + _git_parameter_quote(str(wrappers)))
    env = _git_view_child_environment(view, config, index, native)
    env.update(GIT_CONFIG_NOSYSTEM="0", GIT_CONFIG_PARAMETERS=" ".join(command), GIT_CONFIG_COUNT="0")
    for scope in ("system", "global"):
        resolved_path = admin.parent / f"resolved-{scope}.config"
        resolved_path.write_bytes(os.fsencode("".join(groups[scope])))
        env[f"GIT_CONFIG_{scope.upper()}"] = str(resolved_path)
    return env


def _git_view_child_environment(
    view: Path, config: GitConfiguration, index: Path, native_environment: dict[str, str | None],
) -> dict[str, str]:
    helpers = view / "bin"
    helpers.mkdir()
    try:
        entries = list(config.child_exec_dir.iterdir())
    except OSError as error:
        if error.errno not in {errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM, errno.ELOOP}:
            raise
        entries = []
    for entry in entries:
        if entry.name not in {"git", "octopus-git-python", "callback-check"}:
            (helpers / entry.name).symlink_to(entry)
    launcher = Path(__file__).with_name("git_view_child.py")
    if not os.access(launcher, os.X_OK):
        raise PermissionError("Git view child launcher is unavailable")
    (helpers / "git").symlink_to(launcher)
    (helpers / "callback-check").symlink_to(launcher)
    (helpers / "octopus-git-python").symlink_to(sys.executable)
    native = {
        "environment": native_environment,
        "view_index": str(index),
    }
    (view / "git-child.json").write_text(json.dumps(native))
    return {
        "OCTOPUS_GIT_VIEW": str(view),
        "GIT_EXEC_PATH": str(helpers),
        "GIT_INDEX_FILE": str(index),
        # Native lookup needs search permission, even when the view cannot
        # enumerate the directory to supply direct $GIT_EXEC_PATH references.
        "PATH": native_environment["PATH"] or os.defpath,
    }


def _prepare_stash_logs(common_fd: int) -> DirectoryIdentity | None:
    try:
        os.mkdir("logs", dir_fd=common_fd)
    except FileExistsError:
        return None
    directory = os.stat("logs", dir_fd=common_fd, follow_symlinks=False)
    return directory.st_dev, directory.st_ino


def _remove_unused_stash_logs(common_fd: int, created: DirectoryIdentity) -> None:
    directory = os.stat("logs", dir_fd=common_fd, follow_symlinks=False)
    if (directory.st_dev, directory.st_ino) == created:
        os.rmdir("logs", dir_fd=common_fd)

def _open_repository(root_fd: int, files: ExitStack) -> tuple[int, int]:
    git_fd = _open_git_directory(root_fd)
    files.callback(os.close, git_fd)
    common_fd = _open_common_directory(git_fd)
    files.callback(os.close, common_fd)
    if not _has_git_head(git_fd) or not all(
        stat.S_ISDIR(os.stat(name, dir_fd=common_fd).st_mode) for name in ("objects", "refs")
    ):
        raise FileNotFoundError("The reserved Git repository is unavailable")
    return git_fd, common_fd


def _repository_identity(root_fd: int) -> GitRepositoryIdentity | None:
    try:
        with ExitStack() as files:
            git_fd, common_fd = _open_repository(root_fd, files)
            return _directory_identity(git_fd), _directory_identity(common_fd)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _worktree_ancestry(fd: int) -> tuple[tuple[DirectoryIdentity, ...], int | None, GitRepositoryIdentity | None]:
    """Observe physical ancestry independently of the nearest Git locator."""
    directory = os.dup(fd)
    try:
        ancestry = []
        git_root = None
        repository = None
        while True:
            identity = _directory_identity(directory)
            ancestry.append(identity)
            if git_root is None and (repository := _repository_identity(directory)) is not None:
                git_root = len(ancestry) - 1
            parent = os.open("..", _DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = parent
            if _directory_identity(parent) == identity:
                return tuple(ancestry), git_root, repository
    finally:
        os.close(directory)


def worktree_for(working_dir: str) -> Worktree:
    """Find the local boundary without launching Git in a fenced directory."""
    try:
        directory = Path(working_dir).resolve()
        ancestor = directory
        while True:
            try:
                fd = os.open(ancestor, _DIRECTORY)
                break
            except FileNotFoundError:
                if ancestor.parent == ancestor:
                    return Worktree(directory, None)
                ancestor = ancestor.parent
        try:
            ancestry, git_root, repository = _worktree_ancestry(fd)
            if git_root is None:
                return Worktree(directory, ancestry[0] if ancestor == directory else None, ancestry)
            depth = git_root
            root = ancestor.parents[depth - 1] if 0 < depth <= len(ancestor.parents) else ancestor
            return Worktree(root, ancestry[git_root], ancestry, repository)
        finally:
            os.close(fd)
    except (OSError, RuntimeError) as error:
        raise WorktreeChanged("The reserved Git worktree cannot be verified") from error


@dataclass
class GitScope:
    worktree: Worktree
    db: Database | None
    session_id: str | None
    app_id: str | None = None


_scope: ContextVar[GitScope | None] = ContextVar("fork_git_scope", default=None)
_executions: dict[asyncio.Task[GitResult], tuple[GitScope, asyncio.Event]] = {}
_worktree_locks: weakref.WeakValueDictionary[tuple[int, int] | Path, asyncio.Lock] = weakref.WeakValueDictionary()


@contextmanager
def git_scope(
    worktree: Worktree, db: Database | None, session_id: str | None, *, inherit: bool = False,
) -> Iterator[None]:
    if inherit and _scope.get() is not None:
        yield
        return
    token = _scope.set(GitScope(worktree, db, session_id))
    try:
        yield
    finally:
        _scope.reset(token)


def reserved_worktree(working_dir: str) -> Worktree:
    scope = _scope.get()
    return scope.worktree if scope is not None else worktree_for(working_dir)


def worktree_lock(worktree: Worktree) -> asyncio.Lock:
    lock = _worktree_locks.get(worktree.key)
    if lock is None:
        lock = asyncio.Lock()
        _worktree_locks[worktree.key] = lock
    return lock


@dataclass(frozen=True)
class StashObservation:
    oid: str
    verify: bool = False


class GitRetirementPending(RuntimeError):
    def __init__(self, execution: asyncio.Task[GitResult], *, timed_out: bool):
        super().__init__("Git execution retirement is unconfirmed")
        self.execution = execution
        self.timed_out = timed_out
        self.file_operation = False
        self.stash: StashObservation | None = None


class GitRetirementCancelled(asyncio.CancelledError):
    def __init__(self, execution: asyncio.Task[GitResult], *, timed_out: bool):
        super().__init__("Git caller cancelled")
        self.execution = execution
        self.timed_out = timed_out
        self.file_operation = False
        self.stash: StashObservation | None = None


def worktree_retiring(worktree: Worktree) -> bool:
    return any(worktree.contains_retirement_root(original.worktree) and stopping.is_set() and not _retired(task)
               for task, (original, stopping) in _executions.items())


async def application_git_pending(db: Database, app_id: str, session_ids: list[str]) -> bool:
    if await db.has_reverting_fork(session_ids):
        return True
    ids = set(session_ids)
    return any(scope.db is db and (scope.app_id == app_id or scope.session_id in ids) and not _retired(task)
               for task, (scope, _) in _executions.items())


async def worktree_blocked(db: Database, worktree: Worktree) -> bool:
    for cwd, path, identity in await db.load_revert_worktrees():
        if identity is not None:
            if identity in worktree.ancestors:
                return True
            continue
        if path is not None and Path(path) == worktree.path:
            return True
        # A legacy row did not record which ancestor Git used. A newer
        # repository at the old cwd cannot narrow the original fence.
        try:
            observed = worktree_for(cwd)
            if observed.key == worktree.key or (path is None and worktree.identity in observed.ancestors):
                return True
        except WorktreeChanged:
            if Path(cwd) == worktree.path or (path is None and Path(cwd).is_relative_to(worktree.path)):
                return True
    return worktree_retiring(worktree)


def git_executions_pending() -> bool:
    return any(not _retired(task) for task in _executions)


async def drain_git_executions() -> None:
    active = {task for task in _executions if not _retired(task)}
    if not active:
        return
    for task in active:
        _executions[task][1].set()
    await asyncio.wait(active, timeout=_RETIRE_TIMEOUT)
    if any(not _retired(task) for task in active):
        raise RuntimeError("Git execution retirement is unconfirmed at shutdown")


async def _read_pipe(stream: IO[bytes], stopping: asyncio.Event) -> tuple[bytes, bool]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    transport: asyncio.ReadTransport | None = None
    try:
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), stream)
        return await reader.read(), False
    except OSError:
        # The Popen handle already belongs to the original owner. A reader
        # failure must stop that group before a successor can start.
        stopping.set()
        return b"", True
    finally:
        if transport is None:
            stream.close()
        else:
            transport.close()


async def _collect_output(proc: subprocess.Popen[bytes], stopping: asyncio.Event) -> GitResult:
    assert proc.stdout is not None and proc.stderr is not None
    (out, out_failed), (err, err_failed) = await asyncio.gather(
        _read_pipe(proc.stdout, stopping), _read_pipe(proc.stderr, stopping),
    )
    while (exit_code := proc.poll()) is None:
        await asyncio.sleep(_POLL)
    return GitResult(
        returncode=exit_code,
        stdout=os.fsdecode(out),
        stderr=err.decode(errors="replace"),
        output_failed=out_failed or err_failed,
    )


async def _spawn_git(
    args: tuple[str, ...], *, directory_fd: int, repository_env: dict[str, str] | None = None,
    executable: str = "git",
) -> subprocess.Popen[bytes]:
    # Popen hands back the process and its pipes together. Async pipe
    # attachment starts only after this recoverable handle is captured.
    env = os.environ.copy()
    for key in _REPOSITORY_ENV:
        env.pop(key, None)
    if repository_env is not None:
        env.update(repository_env)
    return await asyncio.to_thread(
        subprocess.Popen, (executable, *args), cwd=_fd_path(directory_fd), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, env=env,
    )


async def _read_git_configuration(
    git_dir: Path, directory_fd: int, repository_env: dict[str, str], stopping: asyncio.Event,
    *, include_hooks: bool,
) -> GitConfiguration | GitResult:
    installed = shutil.which("git")
    if installed is None:
        raise FileNotFoundError("Git executable is unavailable")
    executable = str(Path(installed).absolute())
    config = (f"--git-dir={git_dir}", "--work-tree=.", "config", "--null", "--show-scope", "--list")
    observations: dict[str, str] = {}
    shell = None
    for phase, args in (
        ("resolved", (*config, "--includes")), ("primary", (*config, "--no-includes")),
        ("exec_path", (*config[:2], "--exec-path")),
        ("shell_path", (*config[:2], "var", "GIT_SHELL_PATH")),
        ("hooks_path", (*config[:2], "rev-parse", "--git-path", "hooks")),
    ):
        if phase == "hooks_path" and not include_hooks:
            continue
        if phase == "shell_path" and not include_hooks and not any(
            value and _filter_command_kind(key, value) == "shell"
            for _, key, value in _config_records(observations["resolved"])
        ):
            continue
        process = await _spawn_git(
            args, directory_fd=directory_fd, repository_env=repository_env, executable=executable,
        )
        observed = await _retire_git_process(process, stopping)
        # Older Git does not advertise its shell path. Use the platform sh.
        if phase == "shell_path" and observed.returncode == 129 and not observed.output_failed and not stopping.is_set():
            shell = "/bin/sh"
            continue
        if observed.failed or stopping.is_set():
            return GitResult(
                None, "", "Git configuration could not be read safely",
                configuration=GitConfigurationResult(phase, observed.returncode, observed.output_failed),
            )
        observations[phase] = observed.stdout
        if phase == "shell_path":
            shell = observed.stdout.removesuffix("\n")
    exec_path = observations["exec_path"].removesuffix("\n")
    worktree = Path(os.readlink(_fd_path(directory_fd)))
    child_exec_dir = Path(exec_path)
    if not child_exec_dir.is_absolute():
        child_exec_dir = worktree / child_exec_dir
    return GitConfiguration(
        resolved=observations["resolved"], primary=observations["primary"], executable=executable, exec_path=exec_path,
        child_exec_dir=child_exec_dir, shell=shell,
        hooks=Path(observations["hooks_path"].removesuffix("\n")) if "hooks_path" in observations else None,
    )


async def _execute_git(
    working_dir: str, args: tuple[str, ...], stopping: asyncio.Event, scope: GitScope,
) -> GitResult:
    if scope.db is not None and scope.session_id is not None:
        try:
            scope.app_id = await scope.db.authorize_session_git(scope.session_id)
        except (ValueError, DatabaseBusy) as error:
            return GitResult(None, "", str(error))
    with ExitStack() as files:
        repository_fds = None
        binding = None
        view = None
        created_logs = None
        try:
            fd = os.open(working_dir, _DIRECTORY)
            files.callback(os.close, fd)
            # Verify the directory actually opened, then give Popen that
            # directory even if its original pathname changes before fork.
            scope.worktree.require_open_directory(fd)
            if args != _ROOT_LOOKUP and scope.worktree.repository is not None:
                if _directory_identity(fd) != scope.worktree.identity:
                    raise WorktreeChanged("Git commands require the verified worktree root")
                git_fd, common_fd = _open_repository(fd, files)
                repository = _directory_identity(git_fd), _directory_identity(common_fd)
                if repository != scope.worktree.repository:
                    raise WorktreeChanged("The reserved Git repository changed")
                repository_fds = git_fd, common_fd
                gitfile = stat.S_ISREG(os.stat(".git", dir_fd=fd).st_mode)
                binding = RepositoryBinding(
                    working_dir, str(_fd_path(fd)), str(_fd_path(git_fd)), str(_fd_path(common_fd)),
                    os.readlink(_fd_path(git_fd)) if gitfile else None,
                )
                status = args[:1] == ("status",)
                stash = args[:3] == ("--literal-pathspecs", "stash", "push")
                store = args[:2] == ("stash", "store")
                uses_filters = status or stash or args[:2] in {
                    ("stash", "create"), ("--literal-pathspecs", "restore"),
                }
                reftable = (_fd_path(common_fd) / "reftable").is_dir()
                git_dir = _fd_path(git_fd)
                executable = "git"
                repository_env = {"GIT_COMMON_DIR": str(_fd_path(common_fd))}
                if uses_filters or store or reftable:
                    config = await _read_git_configuration(git_dir, fd, repository_env, stopping, include_hooks=not status)
                    if isinstance(config, GitResult):
                        return config
                    _require_common_directory(git_fd, common_fd)
                    if (stash or store) and not reftable:
                        created_logs = _prepare_stash_logs(common_fd)
                    git_dir, repository_env = _git_directory_view(git_fd, common_fd, files, config, binding)
                    view = git_dir.parent
                    executable = config.executable
                    # A split-index base is written under GIT_DIR. Write a full
                    # index when that directory is the per-execution view.
                    args = ("-c", "core.splitIndex=false", *args)
                args = (f"--git-dir={git_dir}", "--work-tree=.", *args)
                if status:
                    args = ("--no-optional-locks", *args)
                proc = await _spawn_git(
                    args, directory_fd=fd, repository_env=repository_env, executable=executable,
                )
            elif args == _ROOT_LOOKUP:
                proc = await _spawn_git(args, directory_fd=fd)
            else:
                raise WorktreeChanged("No Git repository was reserved for this command")
        except (WorktreeChanged, OSError, ValueError) as error:
            diagnostic = str(error) if isinstance(error, WorktreeChanged) else "git not spawnable"
            if created_logs is not None and repository_fds is not None:
                try:
                    _remove_unused_stash_logs(repository_fds[1], created_logs)
                except OSError:
                    diagnostic += "; cleanup of the prepared Git logs directory could not be confirmed"
            return GitResult(None, "", diagnostic)
        result = await _retire_git_process(proc, stopping)
        if view is not None and (view / "callback-binding-failed").exists():
            return replace(result, binding_failed=True)
        if binding is not None:
            try:
                binding.require_current()
            except (OSError, ValueError):
                return replace(result, binding_failed=True)
        return result


async def _retire_git_process(proc: subprocess.Popen[bytes], stopping: asyncio.Event) -> GitResult:
    # This is the only reader of both pipes. The request never cancels it,
    # including while spawn is pending or after it requests a stop twice.
    output = asyncio.create_task(_collect_output(proc, stopping), name="fork-git-output")
    stop = asyncio.create_task(stopping.wait(), name="fork-git-stop")
    killed = False
    observed_error = False
    try:
        await asyncio.wait({output, stop}, return_when=asyncio.FIRST_COMPLETED)
        while True:
            try:
                if stopping.is_set() and not killed:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    killed = True
                if output.done():
                    exists = group_has_live_members_after_kill if killed else group_exists
                    if not await asyncio.to_thread(exists, proc.pid):
                        break
            except OSError:
                if not observed_error:
                    logger.warning("Could not confirm Git process group %s retirement", proc.pid, exc_info=True)
                    observed_error = True
            await asyncio.sleep(_POLL)
        return output.result()
    finally:
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)


def _retired(task: asyncio.Task[GitResult]) -> bool:
    return task.done() and not task.cancelled() and task.exception() is None


def _observe_retired(task: asyncio.Task[GitResult]) -> None:
    # An unexpected owner failure provides no exit proof. The failed owner
    # stays registered so drain and admission can still see the obligation.
    if _retired(task):
        _executions.pop(task)


async def run_git(working_dir: str, *args: str, timeout: float = 10.0) -> GitResult:
    """Return the Git result only after the original execution has retired."""
    stopping = asyncio.Event()
    bound = _scope.get()
    try:
        scope = replace(bound) if bound is not None else GitScope(worktree_for(working_dir), None, None)
    except WorktreeChanged as error:
        return GitResult(None, "", str(error))
    execution = asyncio.create_task(_execute_git(working_dir, args, stopping, scope), name="fork-git")
    _executions[execution] = (scope, stopping)
    execution.add_done_callback(_observe_retired)
    cancelled: asyncio.CancelledError | None = None
    timed_out = False
    try:
        done, _ = await asyncio.wait({execution}, timeout=timeout)
        if done and _retired(execution):
            return execution.result()
        timed_out = not done
    except asyncio.CancelledError as error:
        cancelled = error

    stopping.set()
    try:
        await complete_cleanup(asyncio.wait({execution}, timeout=_RETIRE_TIMEOUT))
    except asyncio.CancelledError as error:
        if cancelled is None:
            cancelled = error
    if cancelled is not None:
        raise GitRetirementCancelled(execution, timed_out=timed_out) from cancelled
    if not _retired(execution):
        raise GitRetirementPending(execution, timed_out=timed_out)
    return replace(execution.result(), timed_out=True)
