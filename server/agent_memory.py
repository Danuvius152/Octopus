"""Per-agent native-memory provisioning (docs/implemented/memory.md).

Memory is each harness's NATIVE, agent-written markdown memory, persisted and
scoped per agent under ``<agents_dir>/<agent_id>/memory/`` — one canonical
directory both harnesses point at:

- **Claude** sets ``CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`` to this dir, which
  relocates only its auto-memory dir. ``CLAUDE_CONFIG_DIR`` is left at the host
  default, so Claude's session transcripts (``--resume`` data) and auth are
  untouched.
- **Codex** gets the dir's absolute path in its ``developer_instructions`` and
  reads/writes it with file tools. ``CODEX_HOME`` is unchanged.

Memory is thus fully decoupled from both harnesses' config/auth dirs. Pure path
helpers + idempotent provisioning; no global state. ``~`` is expanded at call
time so tests that monkeypatch ``$HOME`` see the override.
"""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import directory_cleanup
from .config import settings

_RETIREMENT_PREFIX = ".agent-delete-"
_ORIGIN_PREFIX = ".agent-delete-origin-"


def _agents_root(user_id: str | None = None) -> Path:
    """Where agents' state lives — this account's, or the install's.

    Per account since multi-tenancy.md §6. `user_id=None` answers the legacy
    directory, which is both what a pre-accounts install has and what an
    upgraded one keeps until §9 moves it.
    """
    from .workspace import paths_for

    if user_id is None:
        return Path(os.path.expanduser(settings.agents_dir))
    return paths_for(user_id).agents


def agent_state_dir(agent_id: str, user_id: str | None = None) -> Path:
    """``<agents_root>/<agent_id>/`` — the agent's durable state root."""
    return _agents_root(user_id) / agent_id


def agent_memory_dir(agent_id: str, user_id: str | None = None) -> Path:
    """The canonical per-agent memory dir (native markdown, shared by both
    harnesses)."""
    return agent_state_dir(agent_id, user_id) / "memory"


def ensure_agent_dirs(agent_id: str, user_id: str | None = None) -> None:
    """Create the canonical ``memory/`` dir (idempotent)."""
    agent_memory_dir(agent_id, user_id).mkdir(parents=True, exist_ok=True)


def valid_private_agent_identity(agent_id: str, user_id: str | None) -> bool:
    def component(value: str) -> bool:
        return (isinstance(value, str) and bool(value) and not value.startswith(".")
                and "/" not in value and "\0" not in value and value != "shared-memory")

    return component(agent_id) and (user_id is None or component(user_id))


def retirement_name(agent_id: str) -> str:
    return _RETIREMENT_PREFIX + agent_id


@contextmanager
def _private_directory(name: str, parent: int, mount: str) -> Iterator[int]:
    descriptor = os.open(name, directory_cleanup.OPEN_DIRECTORY, dir_fd=parent)
    try:
        if directory_cleanup.mount_id(descriptor) != mount:
            raise OSError("Private Agent retirement crosses a mount")
        yield descriptor
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _Retirement:
    receipt: str
    proof: str
    moved: bool

    @property
    def verified(self) -> bool:
        return self.receipt.startswith("verified-")


def _require_empty_file(directory: int, name: str, mount: str) -> None:
    descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size or directory_cleanup.mount_id(descriptor) != mount:
            raise OSError("Private Agent retirement proof is invalid")
    finally:
        os.close(descriptor)


def _require_original(directory: int, proof: str, mount: str, *, verified: bool) -> None:
    entries = os.listdir(directory)
    if verified and not entries:
        return
    if entries != [proof]:
        raise OSError("Private Agent retirement does not contain the original directory proof")
    _require_empty_file(directory, proof, mount)


def _is_present(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _read_retirement(parent: int, agent_id: str, private: int, mount: str) -> _Retirement | None:
    info = os.fstat(private)
    if info.st_uid != os.geteuid() or (info.st_mode & 0o777) != 0o700:
        raise OSError("Private Agent retirement address changed")
    entries = set(os.listdir(private))
    if not entries:
        return None
    moved = "directory" in entries
    entries.discard("directory")
    if len(entries) != 1:
        raise OSError("Private Agent retirement contains unverified entries")
    receipt = entries.pop()
    state, _, token = receipt.partition("-")
    if state not in {"planned", "verified"} or len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
        raise OSError("Private Agent retirement contains an invalid receipt")
    _require_empty_file(private, receipt, mount)
    pending = _Retirement(receipt, _ORIGIN_PREFIX + token, moved)
    if moved or pending.verified:
        if _is_present(parent, agent_id):
            raise OSError("Private Agent retirement address is occupied")
        if moved:
            with _private_directory("directory", private, mount) as target:
                _require_original(target, pending.proof, mount, verified=pending.verified)
    else:
        with _private_directory(agent_id, parent, mount) as target:
            _require_original(target, pending.proof, mount, verified=False)
    return pending


def _finish_retirement(parent: int, agent_id: str, private: int, mount: str) -> None:
    pending = _read_retirement(parent, agent_id, private, mount)
    if pending is None:
        return
    if not pending.verified:
        if not pending.moved:
            directory_cleanup.move_unoccupied(parent, agent_id, private, "directory")
        if _is_present(parent, agent_id):
            raise OSError("Private Agent retirement address is occupied")
        with _private_directory("directory", private, mount) as target:
            _require_original(target, pending.proof, mount, verified=False)
        verified = "verified-" + pending.receipt.removeprefix("planned-")
        directory_cleanup.move_unoccupied(private, pending.receipt, private, verified)
        pending = _Retirement(verified, pending.proof, True)
    # Seal the receipt and the rename before consuming the directory proof.
    os.fsync(private)
    os.fsync(parent)
    if pending.moved:
        with _private_directory("directory", private, mount) as target:
            if _is_present(target, pending.proof):
                os.unlink(pending.proof, dir_fd=target)
                os.fsync(target)
        os.rmdir("directory", dir_fd=private)
        os.fsync(private)
    os.unlink(pending.receipt, dir_fd=private)
    os.fsync(private)


def _recover_retirement(parent: int, agent_id: str, mount: str) -> None:
    slot = retirement_name(agent_id)
    if not _is_present(parent, slot):
        return
    with _private_directory(slot, parent, mount) as private:
        _finish_retirement(parent, agent_id, private, mount)
    os.rmdir(slot, dir_fd=parent)
    os.fsync(parent)


def _sync_new_file(directory: int, name: str) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=0o600, dir_fd=directory)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory)


def _remove_empty_root(parent: int, agent_id: str, original: int, mount: str) -> None:
    # Persist a matching plan and origin proof before the name can move.
    # The finalizer keeps an unverified replacement intact across failures.
    token = uuid.uuid4().hex
    slot = retirement_name(agent_id)
    os.mkdir(slot, mode=0o700, dir_fd=parent)
    with _private_directory(slot, parent, mount) as private:
        os.fchmod(private, 0o700)
        _sync_new_file(original, _ORIGIN_PREFIX + token)
        _sync_new_file(private, "planned-" + token)
        os.fsync(parent)
    _recover_retirement(parent, agent_id, mount)


def remove_agent_dir(agent_id: str, user_id: str | None = None) -> None:
    """Remove the private tree through its verified account directory."""
    if not valid_private_agent_identity(agent_id, user_id):
        raise OSError("Invalid private Agent directory identity")
    root = _agents_root(user_id)
    configured_root = root if user_id is None else root.parent.parent
    trusted_root = configured_root.resolve()
    components = (os.fspath(trusted_root),) if user_id is None else (os.fspath(trusted_root), user_id, root.name)
    opened: list[tuple[str, int | None, int]] = []

    def verify_parents() -> None:
        if configured_root.resolve() != trusted_root:
            raise OSError("Configured Agent directory root changed during removal")
        for name, parent, fd in opened:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not os.path.samestat(current, os.fstat(fd)):
                raise OSError("Agent directory parent changed during removal")

    with ExitStack() as stack:
        parent: int | None = None
        mount: str | None = None
        for name in components:
            try:
                fd = os.open(name, directory_cleanup.OPEN_DIRECTORY, dir_fd=parent)
            except FileNotFoundError:
                verify_parents()
                return
            stack.callback(os.close, fd)
            if mount is None:
                mount = directory_cleanup.mount_id(fd)
            elif directory_cleanup.mount_id(fd) != mount:
                raise OSError("Agent directory parent crosses a mount")
            opened.append((name, parent, fd))
            parent = fd
        assert parent is not None and mount is not None
        verify_parents()
        _recover_retirement(parent, agent_id, mount)
        try:
            target = os.open(agent_id, directory_cleanup.OPEN_DIRECTORY, dir_fd=parent)
        except FileNotFoundError:
            pass
        else:
            stack.callback(os.close, target)
            if directory_cleanup.mount_id(target) != mount:
                raise OSError("Private Agent directory crosses a mount")
            opened.append((agent_id, parent, target))
            verify_parents()
            # Hold the original private root even if its name is exchanged.
            directory_cleanup.empty_directory(target, mount)
            verify_parents()
            opened.pop()
            _remove_empty_root(parent, agent_id, target, mount)
        verify_parents()
        try:
            os.stat(agent_id, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            os.fsync(parent)
            verify_parents()
        else:
            raise OSError("Agent directory still exists after removal")


def verify_pending_agent_paths(users_root: Path, user_id: str, agent_ids: list[str]) -> None:
    """Check offline copies using the same mount and proof rules as deletion."""
    with ExitStack() as stack:
        def open_directory(name: str, parent: int | None = None) -> int:
            fd = os.open(name, directory_cleanup.OPEN_DIRECTORY, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        root = open_directory(os.fspath(users_root))
        mount = directory_cleanup.mount_id(root)
        try:
            account = open_directory(user_id, root)
            if directory_cleanup.mount_id(account) != mount:
                raise directory_cleanup.DirectoryBindingError("Pending Agent account crosses a mount")
            agents = open_directory("agents", account)
            if directory_cleanup.mount_id(agents) != mount:
                raise directory_cleanup.DirectoryBindingError("Pending Agent directory parent crosses a mount")
        except FileNotFoundError:
            return
        for agent_id in agent_ids:
            for name in (agent_id, retirement_name(agent_id)):
                try:
                    fd = os.open(name, directory_cleanup.OPEN_DIRECTORY, dir_fd=agents)
                except FileNotFoundError:
                    continue
                try:
                    if directory_cleanup.mount_id(fd) != mount:
                        raise directory_cleanup.DirectoryBindingError("Pending Agent address crosses a mount")
                    if name == agent_id:
                        directory_cleanup.verify_tree_mounts(fd, mount)
                    else:
                        _read_retirement(agents, agent_id, fd, mount)
                finally:
                    os.close(fd)
