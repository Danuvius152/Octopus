"""Directory ownership for an initializing full-copy fork on Linux."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..directory_cleanup import DirectoryBindingError, empty_directory
from ..directory_cleanup import mount_id as _mount_id
from ..directory_cleanup import move_unoccupied as _move
from ..workspace import (
    FORK_CREATION_ATTRIBUTE,
    FORK_RETIREMENT_TARGET_ATTRIBUTE,
    account_fork_root,
    user_root,
)

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ForkDirectoryError(RuntimeError):
    """The saved path no longer proves authority over a directory."""


class CopyDestinationExists(FileExistsError):
    """Exclusive installation found an existing public directory entry."""


def fork_copy_root(user_id: str | None) -> Path:
    if user_id is None:
        return Path.home() / ".octopus" / "fork"
    account = user_root(user_id)
    return account_fork_root(account.parent.resolve() / account.name)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def fd_path(fd: int) -> Path:
    return Path(f"/proc/{os.getpid()}/fd/{fd}")


@contextmanager
def _directory(name: str, *, parent: int | None = None) -> Iterator[int]:
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _parents(
    path: Path, *, allow_missing: bool = False, start: tuple[Path, int] | None = None,
) -> Iterator[tuple[Path, int]]:
    """Walk from a bound ancestor or the filesystem root without following links."""
    if start is None:
        opened = Path(path.anchor)
        parent = os.open(path.anchor, _DIRECTORY)
    else:
        opened, fd = start
        parent = os.dup(fd)
    try:
        for part in path.relative_to(opened).parts:
            try:
                child = os.open(part, _DIRECTORY, dir_fd=parent)
            except FileNotFoundError:
                if allow_missing:
                    break
                raise
            previous, parent = parent, child
            os.close(previous)
            opened /= part
        yield opened, parent
    finally:
        os.close(parent)


def _identity(fd: int) -> list[int]:
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def _same_directory(left: int, right: int) -> bool:
    return os.path.samestat(os.fstat(left), os.fstat(right)) and _mount_id(left) == _mount_id(right)


def _require_parent_binding(path: Path, parent: int) -> None:
    with _parents(path) as (_, current):
        if not _same_directory(current, parent):
            raise ForkDirectoryError("Fork copy parent no longer has its recorded binding")


def _service_controls_directory_entries(fd: int) -> bool:
    info = os.fstat(fd)
    write_bits = info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    return info.st_uid == os.geteuid() and write_bits == stat.S_IWUSR


def _discard_staged_parent(parent: int, name: str, bound: int) -> None:
    if not _service_controls_directory_entries(parent):
        raise ForkDirectoryError("Unpublished fork parent is no longer in a service-controlled directory")
    try:
        with _directory(name, parent=parent) as current:
            if not _same_directory(current, bound):
                raise ForkDirectoryError("Unpublished fork parent no longer has its creation binding")
            os.rmdir(name, dir_fd=parent)
            os.fsync(parent)
    except OSError as error:
        raise ForkDirectoryError("Unpublished fork parent could not be retired") from error


def _creation_token(value: object) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ForkDirectoryError("Fork directory has no valid planned creation identity")
    return value


@dataclass(frozen=True)
class CopyLocation:
    path: Path
    layout: str
    user_id: str | None
    planned_parent: tuple[Path, int] | None = None
    planned_staging: tuple[Path, int] | None = None

    @classmethod
    @contextmanager
    def planned(cls, path: Path, user_id: str | None) -> Iterator[CopyLocation]:
        root = _absolute(fork_copy_root(user_id))
        if path.parent != root or path.parent.resolve() != root:
            raise ForkDirectoryError("Fork copy parent is outside its original installation layout")
        with ExitStack() as handles, ExitStack() as users_handle:
            users_root = root.parent.parent
            users = None
            if user_id is not None:
                users = users_handle.enter_context(_parents(users_root, allow_missing=True))
            binding = handles.enter_context(_parents(root, allow_missing=True, start=users))
            staging = None
            if (users is not None and users[0] == users_root and binding[0] == root.parent
                    and _mount_id(users[1]) == _mount_id(binding[1])):
                staging = users
            if staging is None:
                users_handle.close()
            yield cls(path, "account" if user_id is not None else "global", user_id, binding, staging)

    @classmethod
    def saved(cls, working_dir: str, user_id: str | None, fork_id: str,
              metadata: dict[str, Any]) -> CopyLocation:
        layout = metadata.get("copy_layout", "legacy")
        if layout not in ("account", "global", "legacy") or (
            layout != "legacy" and (layout == "account") != (user_id is not None)
        ):
            raise ForkDirectoryError("Unknown initializing fork directory layout")
        root = _absolute(fork_copy_root(user_id if layout == "account" else None))
        path = _absolute(Path(working_dir))
        if path.parent != root or not path.name.endswith(f"-{fork_id}"):
            raise ForkDirectoryError("Initializing fork cwd is outside its original directory scope")
        return cls(path, layout, user_id)

    @contextmanager
    def _allocation_parent(self, token: str) -> Iterator[int]:
        assert self.planned_parent is not None
        anchor, parent = self.planned_parent
        root = self.path.parent
        try:
            _require_parent_binding(anchor, parent)
        except (OSError, ForkDirectoryError) as error:
            raise ForkDirectoryError("Fork copy parent changed after planning") from error
        if anchor == root:
            yield parent
            return

        base = parent
        staging_parent = base
        if self.planned_staging is not None:
            stage_path, stage_fd = self.planned_staging
            if _service_controls_directory_entries(stage_fd):
                try:
                    _require_parent_binding(stage_path, stage_fd)
                except (OSError, ForkDirectoryError) as error:
                    raise ForkDirectoryError("Fork staging parent changed after planning") from error
                staging_parent = stage_fd
        if not _service_controls_directory_entries(staging_parent):
            raise ForkDirectoryError("Fork setup requires a staging parent writable only by the service user")

        # Only the service user may change staging entries. Hold the final
        # directory open across publication into the separate destination.
        first, *remaining = root.relative_to(anchor).parts
        staging = f".fork-parent-{token}"
        parent = staging_parent
        with ExitStack() as handles, ExitStack() as unpublished:
            for part in (staging, *remaining):
                os.mkdir(part, mode=0o700, dir_fd=parent)
                child = handles.enter_context(_directory(part, parent=parent))
                if _mount_id(child) != _mount_id(base):
                    raise ForkDirectoryError("Fork copy parent crossed a mount after planning")
                unpublished.callback(_discard_staged_parent, parent, part, child)
                os.fsync(parent)
                parent = child
            os.fsync(parent)
            try:
                _require_parent_binding(anchor, base)
                if not _service_controls_directory_entries(staging_parent):
                    raise ForkDirectoryError("Fork staging parent permissions changed")
                _move(staging_parent, staging, base, first)
            except (FileExistsError, FileNotFoundError, NotADirectoryError, ForkDirectoryError) as error:
                raise ForkDirectoryError("Fork copy parent changed after planning") from error
            unpublished.pop_all()
            if staging_parent != base:
                os.fsync(staging_parent)
            os.fsync(base)
            try:
                _require_parent_binding(root, parent)
            except (OSError, ForkDirectoryError) as error:
                raise ForkDirectoryError("Fork copy parent changed during installation") from error
            yield parent

    @contextmanager
    def create(self, token: str) -> Iterator[CopyDirectory]:
        """Allocate a bound copy in an owner-only slot before exposing its contents."""
        with self._allocation_parent(token) as parent:
            if _entry_exists(parent, self.path.name):
                raise CopyDestinationExists(str(self.path))
            staging = f".fork-create-{token}"
            os.mkdir(staging, mode=0o700, dir_fd=parent)
            with _directory(staging, parent=parent) as private:
                if _mount_id(private) != _mount_id(parent):
                    raise ForkDirectoryError("Fork allocation slot crossed a mount")
                os.setxattr(private, FORK_CREATION_ATTRIBUTE, token.encode("ascii"))
                os.mkdir("copy", mode=0o700, dir_fd=private)
                with _directory("copy", parent=private) as target:
                    os.setxattr(target, FORK_CREATION_ATTRIBUTE, token.encode("ascii"))
                    os.fsync(target)
                    os.fsync(private)
                    os.fsync(parent)
                    yield CopyDirectory(self, parent, private, target, token)


@dataclass(frozen=True)
class CopyDirectory:
    location: CopyLocation
    parent: int
    private: int
    target: int
    token: str

    @property
    def path(self) -> Path:
        return self.location.path

    def receipt(self) -> dict[str, object]:
        return {"path": str(self.path), "parent": _identity(self.parent),
                "staging": _identity(self.private), "target": _identity(self.target),
                "creation": self.token}

    def copy_from(self, source: Path) -> None:
        _copy_tree(source, self.target)

    def require_staged(self) -> None:
        try:
            if self.path.parent != _absolute(fork_copy_root(self.location.user_id)):
                raise ForkDirectoryError("Fork copy cwd changed after creation")
            _require_parent_binding(self.path.parent, self.parent)
            with (_directory(f".fork-create-{self.token}", parent=self.parent) as private,
                  _directory("copy", parent=private) as target):
                if (_same_directory(private, self.private) and _same_directory(target, self.target)
                        and os.getxattr(target, FORK_CREATION_ATTRIBUTE) == self.token.encode("ascii")
                        and not _entry_exists(self.parent, self.path.name)):
                    return
        except OSError as error:
            raise ForkDirectoryError("Fork copy cwd changed after creation") from error
        raise ForkDirectoryError("Fork copy cwd changed after creation")

    def install(self) -> None:
        self.require_staged()
        try:
            _move(self.private, "copy", self.parent, self.path.name)
        except FileExistsError as error:
            raise ForkDirectoryError("Fork copy cwd changed after creation") from error
        os.fsync(self.private)
        os.rmdir(f".fork-create-{self.token}", dir_fd=self.parent)
        os.fsync(self.parent)

    def require_current(self) -> None:
        try:
            if self.path.parent != _absolute(fork_copy_root(self.location.user_id)):
                raise ForkDirectoryError("Fork copy cwd changed after creation")
            with _parents(self.path.parent) as (_, parent), _directory(self.path.name, parent=parent) as target:
                if (_same_directory(parent, self.parent)
                        and _same_directory(target, self.target)
                        and os.getxattr(target, FORK_CREATION_ATTRIBUTE) == self.token.encode("ascii")):
                    return
        except OSError as error:
            raise ForkDirectoryError("Fork copy cwd changed after creation") from error
        raise ForkDirectoryError("Fork copy cwd changed after creation")


def _copy_tree(source: Path, target: int) -> None:
    """Copy to the bound directory, creating each output entry exclusively."""
    with os.scandir(source) as entries:
        for entry in entries:
            src = Path(entry.path)
            if entry.is_symlink():
                os.symlink(os.readlink(src), entry.name, dir_fd=target)
                info = entry.stat(follow_symlinks=False)
                os.utime(entry.name, ns=(info.st_atime_ns, info.st_mtime_ns), dir_fd=target, follow_symlinks=False)
            elif entry.is_dir(follow_symlinks=False):
                os.mkdir(entry.name, dir_fd=target)
                with _directory(entry.name, parent=target) as child:
                    _copy_tree(src, child)
            else:
                if not entry.is_file(follow_symlinks=False):
                    raise shutil.SpecialFileError(f"Cannot copy special file {src}")
                read_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(read_fd, "rb") as original:
                    if not stat.S_ISREG(os.fstat(read_fd).st_mode):
                        raise shutil.SpecialFileError(f"Cannot copy special file {src}")
                    fd = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=target)
                    with os.fdopen(fd, "wb") as output:
                        shutil.copyfileobj(original, output)
                        output.flush()
                        shutil.copystat(fd_path(read_fd), fd_path(fd))
    # The owner marker belongs to this allocation, including when the
    # source itself was created by an earlier full-copy fork.
    info = source.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    try:
        names = os.listxattr(source)
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.ENODATA, errno.EINVAL}:
            raise
        names = []
    for name in names:
        if name != FORK_CREATION_ATTRIBUTE:
            try:
                os.setxattr(target, name, os.getxattr(source, name))
            except OSError as error:
                if error.errno not in {errno.EPERM, errno.ENOTSUP, errno.ENODATA, errno.EINVAL, errno.EACCES}:
                    raise
    os.fchmod(target, stat.S_IMODE(info.st_mode))


def _empty_directory(target: int, mount: str) -> None:
    try:
        empty_directory(target, mount, make_writable=True)
    except DirectoryBindingError as error:
        raise ForkDirectoryError(str(error)) from error


def _created_target(target: int, receipt: dict[str, Any], token: str) -> bool:
    try:
        return (receipt.get("target") == _identity(target)
                and os.getxattr(target, FORK_CREATION_ATTRIBUTE) == token.encode("ascii"))
    except OSError as error:
        if error.errno == errno.ENODATA:
            return False
        raise


def require_saved_copy(working_dir: str, user_id: str | None, fork_id: str, metadata: dict[str, Any]) -> None:
    """Revalidate a committed copy whose original delivery was interrupted."""
    location = CopyLocation.saved(working_dir, user_id, fork_id, metadata)
    token = _creation_token(metadata.get("copy_creation"))
    receipt = metadata.get("owned_copy_dir")
    try:
        with _parents(location.path.parent) as (_, parent), _directory(location.path.name, parent=parent) as target:
            if (isinstance(receipt, dict) and receipt.get("path") == str(location.path)
                    and receipt.get("creation") == token and receipt.get("parent") == _identity(parent)
                    and _mount_id(target) == _mount_id(parent) and _created_target(target, receipt, token)):
                return
    except OSError as error:
        raise ForkDirectoryError("Undelivered fork copy no longer has its recorded binding") from error
    raise ForkDirectoryError("Undelivered fork copy no longer has its recorded binding")


def remove_copy(working_dir: str, user_id: str | None, fork_id: str, metadata: dict[str, Any]) -> None:
    """Retire a verified, still-initializing copy after native cleanup."""
    if metadata.get("full_copy") is not True:
        return
    location = CopyLocation.saved(working_dir, user_id, fork_id, metadata)
    receipt = metadata.get("owned_copy_dir")
    token = _creation_token(metadata.get("copy_creation")) if location.layout != "legacy" else None
    try:
        with _parents(location.path.parent) as (_, parent):
            _remove_copy_at(location, parent, receipt, token)
    except FileNotFoundError as error:
        raise ForkDirectoryError("Fork copy parent or recovery entry no longer has its recorded binding") from error
    except NotADirectoryError as error:
        raise ForkDirectoryError("Fork copy path was redirected or replaced") from error


def _entry_exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_copy_at(location: CopyLocation, parent: int, receipt: object, token: str | None) -> None:
    path, name = location.path, location.path.name
    staging = f".fork-create-{token}" if token is not None else None
    if token is None or not isinstance(receipt, dict):
        if not _entry_exists(parent, name) and (staging is None or not _entry_exists(parent, staging)):
            return
        raise ForkDirectoryError("Incomplete fork has no confirmed directory creation")
    if (receipt.get("path") != str(path) or receipt.get("parent") != _identity(parent)
            or receipt.get("creation") != token):
        raise ForkDirectoryError("Fork copy receipt does not match the recorded parent")
    if staging is not None and _entry_exists(parent, staging):
        _require_parent_binding(path.parent, parent)
        with _directory(staging, parent=parent) as private:
            _require_private_slot(parent, private, token)
            if _identity(private) != receipt.get("staging"):
                raise ForkDirectoryError("Fork allocation slot does not match its creation receipt")
            if _entry_exists(private, "copy"):
                _remove_confirmed_copy(private, receipt, token)
        os.rmdir(staging, dir_fd=parent)
        os.fsync(parent)

    # Move the directory entry to an owner-only slot, then verify the moved
    # object before removing anything. A retry can finish a prior retirement.
    retirement = f".fork-remove-{token}"
    if _entry_exists(parent, retirement):
        with _directory(retirement, parent=parent) as private:
            _require_private_slot(parent, private, token)
            empty = not _entry_exists(private, "copy")
        if empty:
            os.rmdir(retirement, dir_fd=parent)
    if not _entry_exists(parent, retirement):
        _require_parent_binding(path.parent, parent)
        if not _stage_retirement(parent, name, retirement, receipt, token):
            return
    with _directory(retirement, parent=parent) as private:
        _require_private_slot(parent, private, token)
        _require_parent_binding(path.parent, parent)
        if not _slot_contains_copy(private, receipt, token):
            try:
                _move(private, "copy", parent, name)
            except FileExistsError as error:
                raise ForkDirectoryError("Unverified retirement entry cannot be restored while its cwd is occupied") from error
            os.rmdir(retirement, dir_fd=parent)
            os.fsync(parent)
            raise ForkDirectoryError("Restored a changed retirement entry; original fork binding is unverified")
        if _entry_exists(parent, name):
            raise ForkDirectoryError("Fork cwd was replaced during directory retirement")
        _remove_confirmed_copy(private, receipt, token)
    os.rmdir(retirement, dir_fd=parent)
    os.fsync(parent)


def _require_private_slot(parent: int, private: int, token: str) -> None:
    if (_mount_id(private) != _mount_id(parent)
            or os.getxattr(private, FORK_CREATION_ATTRIBUTE) != token.encode("ascii")):
        raise ForkDirectoryError("Fork private slot has no matching ownership")


def _remove_confirmed_copy(private: int, receipt: dict[str, Any], token: str) -> None:
    with _directory("copy", parent=private) as target:
        mount = _mount_id(target)
        if not _created_target(target, receipt, token) or mount != _mount_id(private):
            raise ForkDirectoryError("Fork private slot does not match its creation receipt")
        _empty_directory(target, mount)
        if not os.path.samestat(os.fstat(target), os.stat("copy", dir_fd=private, follow_symlinks=False)):
            raise ForkDirectoryError("Fork private slot changed during cleanup")
        os.rmdir("copy", dir_fd=private)


def _slot_contains_copy(private: int, receipt: dict[str, Any], token: str) -> bool:
    try:
        with _directory("copy", parent=private) as target:
            return _mount_id(target) == _mount_id(private) and _created_target(target, receipt, token)
    except NotADirectoryError:
        return False


def _stage_retirement(parent: int, name: str, retirement: str, receipt: dict[str, Any], token: str) -> bool:
    with ExitStack() as stack:
        try:
            target = stack.enter_context(_directory(name, parent=parent))
        except FileNotFoundError:
            return False
        if not _created_target(target, receipt, token) or _mount_id(target) != _mount_id(parent):
            raise ForkDirectoryError("Fork copy does not match its creation receipt")
        os.mkdir(retirement, mode=0o700, dir_fd=parent)
        with _directory(retirement, parent=parent) as private:
            if _mount_id(private) != _mount_id(parent):
                raise ForkDirectoryError("Fork retirement slot crossed a mount")
            os.setxattr(private, FORK_CREATION_ATTRIBUTE, token.encode("ascii"))
            identity = os.fstat(target)
            os.setxattr(private, FORK_RETIREMENT_TARGET_ATTRIBUTE,
                        f"{identity.st_dev}:{identity.st_ino}".encode("ascii"))
            os.fsync(private)
            os.fsync(parent)
            _move(parent, name, private, "copy")
            os.fsync(private)
            os.fsync(parent)
    return True
