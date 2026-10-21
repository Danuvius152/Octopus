"""Finish the first-account handoff of committed agents' native memory.

The account transaction is the ownership authority. A destination-filesystem
stage publishes one complete tree; the short-lived receipt keeps the original
tree verifiable while its legacy source is being retired. Callers own the
installation command lock and close old execution before entering this module.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .agent_memory import agent_memory_dir
from .aio import complete_cleanup
from .database import Database
from .service_health import ServiceHealth

_STAGE = ".memory-handoff-stage"
_RECEIPT = ".memory-retirement.json"
_RECEIPT_TEMP = ".memory-retirement.tmp"


class MemoryHandoffConflict(Exception):
    """A verifiable conflict: retain both trees and keep memory use closed."""


@dataclass(frozen=True)
class _Tree:
    directories: tuple[str, ...]
    files: dict[str, tuple[int, str]]

    def record(self) -> dict[str, Any]:
        return {"directories": list(self.directories), "files": {
            path: {"size": size, "sha256": digest}
            for path, (size, digest) in self.files.items()
        }}


def _paths(agent_id: str, owner: str) -> tuple[Path, Path]:
    for value in (agent_id, owner):
        if not value or value in {".", ".."} or Path(value).name != value:
            raise MemoryHandoffConflict("Agent memory has an invalid ownership path; state retained")
    source = agent_memory_dir(agent_id)
    target = agent_memory_dir(agent_id, owner)
    # Configured roots may themselves be links. Per-agent links, in contrast,
    # cannot authorize moving or removing another tree through that agent ID.
    if any(path.is_symlink() for root in (source, target) for path in (root, root.parent)):
        raise MemoryHandoffConflict("Agent memory contains a linked root; state retained")
    source, target = source.resolve(), target.resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise MemoryHandoffConflict("Agent memory source and target overlap; state retained")
    return source, target


def memory_handoff_pending(agent_id: str, owner: str | None) -> bool:
    """A committed owner must not delete its row or provision over old memory."""
    if owner is None:
        return False
    source, target = _paths(agent_id, owner)
    artifacts = (target.parent / _STAGE, source.parent / _RECEIPT, source.parent / _RECEIPT_TEMP)
    return source.exists() or any(path.exists() or path.is_symlink() for path in artifacts)


def _snapshot(root: Path) -> _Tree | None:
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(mode):
        raise MemoryHandoffConflict("Agent memory is not a plain directory; state retained")
    directories: list[str] = []
    files: dict[str, tuple[int, str]] = {}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                directories.append(relative)
                visit(path)
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        size += len(block)
                        digest.update(block)
                files[relative] = (size, digest.hexdigest())
            else:
                raise MemoryHandoffConflict("Agent memory contains a link or special file; state retained")

    visit(root)
    return _Tree(tuple(sorted(directories)), files)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_parents(directory: Path) -> None:
    for parent in (directory, *directory.parents):
        _sync_directory(parent)


def _sync_tree(root: Path, tree: _Tree) -> None:
    for relative in tree.files:
        with (root / relative).open("rb") as stream:
            os.fsync(stream.fileno())
    for relative in sorted(tree.directories, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        _sync_directory(root / relative)
    _sync_directory(root)


def _read_tree(raw: Any) -> _Tree:
    def relative(value: Any) -> bool:
        return (isinstance(value, str) and bool(value) and not value.startswith("/")
                and PurePosixPath(value).as_posix() == value
                and not any(part in {".", ".."} for part in value.split("/")))

    if not isinstance(raw, dict) or set(raw) != {"directories", "files"}:
        raise ValueError("invalid tree")
    directories, files = raw["directories"], raw["files"]
    if (not isinstance(directories, list) or not all(relative(path) for path in directories)
            or directories != sorted(set(directories)) or not isinstance(files, dict)):
        raise ValueError("invalid tree paths")
    parsed: dict[str, tuple[int, str]] = {}
    for path, entry in files.items():
        if (not relative(path) or path in directories or not isinstance(entry, dict)
                or set(entry) != {"size", "sha256"} or type(entry["size"]) is not int or entry["size"] < 0
                or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64
                or any(letter not in "0123456789abcdef" for letter in entry["sha256"])):
            raise ValueError("invalid file evidence")
        parsed[path] = (entry["size"], entry["sha256"])
    for path in [*directories, *files]:
        if any(parent.as_posix() not in directories for parent in PurePosixPath(path).parents if parent != PurePosixPath(".")):
            raise ValueError("missing directory evidence")
    return _Tree(tuple(directories), parsed)


def _read_receipt(path: Path, agent_id: str, owner: str, source: Path, target: Path) -> _Tree:
    if path.is_symlink() or not path.is_file():
        raise MemoryHandoffConflict("Agent memory retirement receipt is not a plain file; state retained")
    try:
        receipt = json.loads(path.read_text())
        binding = {"version": 1, "agent_id": agent_id, "owner": owner, "source": str(source), "target": str(target)}
        if not isinstance(receipt, dict) or set(receipt) != {*binding, "tree"}:
            raise ValueError("invalid receipt")
        if type(receipt["version"]) is not int or any(receipt[key] != value for key, value in binding.items()):
            raise ValueError("retirement binding changed")
        return _read_tree(receipt["tree"])
    except (ValueError, TypeError, KeyError) as error:
        raise MemoryHandoffConflict("Agent memory retirement evidence needs verification; state retained") from error


def _write_receipt(path: Path, agent_id: str, owner: str, source: Path, target: Path, tree: _Tree) -> None:
    temporary = path.parent / _RECEIPT_TEMP
    if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
        raise MemoryHandoffConflict("Agent memory receipt staging conflicts with existing state; retained")
    record = {"version": 1, "agent_id": agent_id, "owner": owner,
              "source": str(source), "target": str(target), "tree": tree.record()}
    if temporary.exists():
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(json.dumps(record, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())
    # Publishing a receipt must never replace evidence from another handoff.
    try:
        os.link(temporary, path)
    except FileExistsError as error:
        raise MemoryHandoffConflict("Agent memory retirement evidence already exists; state retained") from error
    temporary.unlink()
    _sync_parents(path.parent)


def _verify_retirement(source: Path, target: Path, tree: _Tree) -> _Tree | None:
    if _snapshot(target) != tree:
        raise MemoryHandoffConflict("Published agent memory differs from its retirement evidence; both trees retained")
    remaining = _snapshot(source)
    if remaining is not None and (
        not set(remaining.directories).issubset(tree.directories)
        or any(tree.files.get(path) != evidence for path, evidence in remaining.files.items())
    ):
        raise MemoryHandoffConflict("Legacy agent memory changed during retirement; both trees retained")
    return remaining


def _retire_source(source: Path, target: Path, tree: _Tree, receipt: Path) -> None:
    remaining = _verify_retirement(source, target, tree)
    if remaining is not None:
        # The receipt covers every unchanged source subset. The final parent
        # sync commits retirement, so intermediate deletions need no own sync.
        for relative in remaining.files:
            (source / relative).unlink()
        for relative in sorted(remaining.directories, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
            (source / relative).rmdir()
        source.rmdir()
    # Reconfirm deletions even when a previous process lost their sync receipt.
    _sync_parents(source.parent)
    receipt.unlink()
    _sync_parents(receipt.parent)


def _handoff_one(agent_id: str, owner: str) -> bool:
    source, target = _paths(agent_id, owner)
    # Keep retirement evidence beside the retained source agent root. A
    # changed destination must find and reject the old canonical binding,
    # rather than interpret its already-deleted source subset as a new tree.
    receipt = source.parent / _RECEIPT
    stage = target.parent / _STAGE
    if receipt.exists() or receipt.is_symlink():
        tree = _read_receipt(receipt, agent_id, owner, source, target)
        _verify_retirement(source, target, tree)
        _sync_tree(target, tree)
        _sync_parents(target.parent)
        # A crash after receipt publication can leave its disposable temp link.
        temporary = receipt.parent / _RECEIPT_TEMP
        if temporary.exists() or temporary.is_symlink():
            if not stat.S_ISREG(temporary.lstat().st_mode) or temporary.read_bytes() != receipt.read_bytes():
                raise MemoryHandoffConflict("Agent memory receipt staging differs; state retained")
            temporary.unlink()
        # A visible receipt can have lost its publication-directory sync.
        # Confirm its source-filesystem namespace before any source unlink,
        # independently of target durability and eventual deletion syncs.
        _sync_parents(receipt.parent)
        _retire_source(source, target, tree, receipt)
        return True
    original = _snapshot(source)
    if original is None:
        temporary = receipt.parent / _RECEIPT_TEMP
        if stage.exists() or stage.is_symlink() or temporary.exists() or temporary.is_symlink():
            raise MemoryHandoffConflict("Agent memory staging has no legacy source; state retained")
        # An absent receipt can have lost its deletion-directory sync. The
        # retained agent root lets reentry confirm that absence before use;
        # agents that never had a legacy directory need no namespace created.
        if source.parent.exists():
            _sync_parents(source.parent)
        return False
    current = _snapshot(target)
    empty = _Tree((), {})
    if current is not None and current not in (empty, original):
        raise MemoryHandoffConflict("Account agent memory is different or incomplete; both trees retained")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if current != original:
        if stage.is_symlink() or (stage.exists() and not stage.is_dir()):
            raise MemoryHandoffConflict("Agent memory stage is not a private directory; state retained")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(mode=0o700)
        for relative in original.directories:
            (stage / relative).mkdir(mode=0o700)
        for relative in original.files:
            shutil.copyfile(source / relative, stage / relative)
            (stage / relative).chmod(0o600)
        if _snapshot(stage) != original or _snapshot(source) != original:
            raise MemoryHandoffConflict("Agent memory changed while being copied; state retained")
        _sync_tree(stage, original)
        _sync_parents(target.parent)
        # Directory rename cannot overwrite a nonempty directory. Only the
        # explicit empty scaffold can be replaced; no trees are ever merged.
        try:
            stage.rename(target)
        except OSError as error:
            if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise MemoryHandoffConflict("Account agent memory appeared before publication; both trees retained") from error
            raise
        _sync_parents(target.parent)
    else:
        _sync_tree(target, original)
        _sync_parents(target.parent)
        if stage.exists():
            if stage.is_symlink() or not stage.is_dir():
                raise MemoryHandoffConflict("Agent memory staging conflicts with published state; retained")
            shutil.rmtree(stage)
            _sync_directory(target.parent)
    if _snapshot(target) != original or _snapshot(source) != original:
        raise MemoryHandoffConflict("Agent memory changed before retirement; both trees retained")
    _write_receipt(receipt, agent_id, owner, source, target, original)
    _retire_source(source, target, original, receipt)
    return True


def _handoff(agents: list[dict[str, Any]], health: ServiceHealth) -> int:
    moved = 0
    try:
        for agent in agents:
            if owner := agent.get("user_id"):
                moved += _handoff_one(agent["id"], owner)
    except OSError as error:
        # Includes copy, publish, unlink and directory-sync outcomes. These
        # must reach the shared boundary before HTTP/Wiki business wrapping.
        health.fail(error)
    return moved


async def handoff_agent_memory(db: Database, *, user_id: str | None = None) -> int:
    """Complete committed agents' handoff before admitting memory producers.

    The original installation command lock must be held by the caller. NULL
    owners never authorize a handoff, including on old authenticated resumes.
    Cancellation joins the filesystem worker before releasing that lock.
    """
    db.health.require_healthy()
    agents = await db.load_agents(include_archived=True, user_id=user_id)
    return await complete_cleanup(asyncio.to_thread(_handoff, agents, db.health))
