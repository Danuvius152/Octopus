"""Read-only admission for the account-based storage baseline."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


class RetainedLegacyInstallation(RuntimeError):
    """Preserve the retained installation for diagnosis by its original version."""


def persistent_database_file(db_path: str) -> Path | None:
    """Validate recovery inputs before even a read-only SQLite connection."""
    if db_path == ":memory:":
        return None
    path = Path(db_path).expanduser()
    try:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            if path.is_symlink():
                raise RetainedLegacyInstallation()
            metadata = None
        # SQLite resolves file aliases before naming its recovery files. Use
        # that same absolute location for sidecars and every read-only caller.
        path = path.resolve()
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            raise RetainedLegacyInstallation()
        if metadata is None or metadata.st_size == 0:
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    Path(str(path) + suffix).lstat()
                except FileNotFoundError:
                    continue
                raise RetainedLegacyInstallation()
            return None
        return path
    except OSError as error:
        raise RetainedLegacyInstallation() from error


def _require_account_database(db_path: str) -> bool:
    path = persistent_database_file(db_path)
    if path is None:
        return False
    try:
        # Include committed WAL transactions. Neither immutable mode nor a
        # checkpoint can be used to decide whether retained input is empty.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
            reader.execute("PRAGMA query_only=ON")
            reader.execute("BEGIN")
            tables = {row[0] for row in reader.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT GLOB 'sqlite_*'"
            )}
            if "users" in tables and reader.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return True
            for name in tables:
                quoted = name.replace('"', '""')
                if reader.execute(f'SELECT 1 FROM "{quoted}" LIMIT 1').fetchone():
                    raise RetainedLegacyInstallation()
            return False
    except (OSError, sqlite3.DatabaseError) as error:
        raise RetainedLegacyInstallation() from error


def require_account_database(db_path: str) -> None:
    """Refuse ownerless business inputs before any writer or migration opens."""
    _require_account_database(db_path)


def require_empty_account_resources(config: Settings) -> None:
    """Setup and startup use the same retained-resource boundary."""
    for name in ("users_root", "agents_dir", "applications_dir", "wiki_dir", "attachments_dir",
                 "large_prompts_dir", "codex_home_dir", "research_dir"):
        root = Path(getattr(config, name)).expanduser()
        try:
            retained = any(path.name != ".octopus-installation" for path in root.iterdir())
        except FileNotFoundError:
            if not root.is_symlink():
                continue
            retained = True
        if retained:
            raise RetainedLegacyInstallation()


def _refuse_present(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        if path.parent.is_symlink():
            path.parent.stat()
        return
    raise RetainedLegacyInstallation()


def _entries(root: Path) -> list[Path]:
    try:
        return list(root.iterdir())
    except FileNotFoundError:
        if root.is_symlink():
            raise RetainedLegacyInstallation()
        return []


def _state_directories(root: Path) -> list[Path]:
    result = []
    for path in _entries(root):
        mode = path.lstat().st_mode
        if path.name == ".octopus-installation" and stat.S_ISREG(mode):
            continue
        if not stat.S_ISDIR(mode) or path.name == ".octopus-installation":
            raise RetainedLegacyInstallation()
        result.append(path)
    return result


def _require_no_handoff_inputs(config: Settings) -> None:
    users = Path(config.users_root).expanduser()
    _refuse_present(users / ".first-account-upgrade.json")
    for path in _entries(users):
        if path.name.startswith(".wiki-adoption-"):
            raise RetainedLegacyInstallation()
    _refuse_present(Path(config.wiki_dir).expanduser() / ".upgrade-owner.json")
    for user in _state_directories(users):
        _refuse_present(user / "wiki" / ".upgrade-owner.json")
        for agent in _state_directories(user / "agents"):
            _refuse_present(agent / ".memory-handoff-stage")
    for agent in _state_directories(Path(config.agents_dir).expanduser()):
        # These are protocol positions, not recursive filename matches inside
        # current memory, workspaces or external projects.
        for name in ("memory", ".memory-retirement.json", ".memory-retirement.tmp"):
            _refuse_present(agent / name)


def require_account_installation(config: Settings) -> None:
    """Check the actual installation before preparing its key or directories."""
    try:
        if not _require_account_database(config.db_path):
            require_empty_account_resources(config)
        _require_no_handoff_inputs(config)
    except OSError as error:
        raise RetainedLegacyInstallation() from error
