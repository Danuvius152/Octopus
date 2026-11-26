"""Where a user's files live, and where a session is allowed to work.

Single-user Octopus put everything under one set of directories and let a
session name any absolute path on the box as its working directory. Neither
survives accounts (multi-tenancy.md §6):

* every directory becomes a function of a user, so one account's agent memory,
  applications and uploads cannot be another's;
* a working directory has to resolve *inside* that user's workspace, or the
  first thing an agent does with a shell is read someone else's.

Both eras are the same shape. `paths_for(None)` — the pre-accounts install —
answers the directories Octopus has always used, and confinement is off,
because there is no owner to confine to and the one operator's existing
sessions point wherever they already point. From the first account on, every
answer is per-user and confinement is on.

**Symlinks are resolved before the check, not after.** A check against the
literal path is decoration: `ln -s /home/someone-else ~/work/theirs` walks
straight through it.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import settings

FORK_CREATION_ATTRIBUTE = "user.octopus.fork-creation"
FORK_RETIREMENT_TARGET_ATTRIBUTE = "user.octopus.fork-retirement-target"


class WorkspaceError(Exception):
    """A path outside what this account may reach."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class UserPaths:
    """One account's corner of the disk."""

    workspace: Path
    agents: Path
    applications: Path
    attachments: Path
    large_prompts: Path
    codex_home: Path
    research: Path

    def ensure(self) -> UserPaths:
        for path in (
            self.workspace,
            self.agents,
            self.applications,
            self.attachments,
            self.large_prompts,
            self.codex_home,
            self.research,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


def _expand(raw: str) -> Path:
    return Path(raw).expanduser()


def user_root(user_id: str) -> Path:
    return _expand(settings.users_root) / user_id


def account_fork_root(account_root: Path) -> Path:
    return account_root / "fork"


def managed_fork_entries(root: Path) -> set[str]:
    """Identify fork-created entries without classifying preexisting data by name."""
    if root.is_symlink() or not root.is_dir():
        return set()
    entries: set[str] = set()
    for entry in root.iterdir():
        suffix = entry.name.rpartition("-")[2]
        private = len(suffix) == 32 and entry.name.startswith((".fork-create-", ".fork-remove-"))
        if not (
            len(suffix) == 12 and all(char in "0123456789abcdef" for char in suffix)
            or private
        ):
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            token = os.getxattr(entry, FORK_CREATION_ATTRIBUTE, follow_symlinks=False)
        except OSError as error:
            if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                raise
        else:
            if len(token) == 32 and all(byte in b"0123456789abcdef" for byte in token):
                if private and token != suffix.encode("ascii"):
                    continue
                if entry.name.startswith(".fork-remove-"):
                    _require_exportable_retirement(entry, token)
                entries.add(entry.name)
    return entries


def _require_exportable_retirement(slot: Path, token: bytes) -> None:
    """An occupied slot may contain a foreign entry awaiting restoration."""
    names = {entry.name for entry in slot.iterdir()}
    if not names:
        return
    if names == {"copy"}:
        copy = slot / "copy"
        info = copy.lstat()
        if stat.S_ISDIR(info.st_mode):
            try:
                identity = os.getxattr(slot, FORK_RETIREMENT_TARGET_ATTRIBUTE, follow_symlinks=False)
                creation = os.getxattr(copy, FORK_CREATION_ATTRIBUTE, follow_symlinks=False)
            except OSError as error:
                if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                    raise
            else:
                if identity == f"{info.st_dev}:{info.st_ino}".encode("ascii") and creation == token:
                    return
    raise ValueError(f"Account export found an unverified fork retirement at {slot}")


def paths_for(user_id: str | None) -> UserPaths:
    """Where this account's files live — or the install's, before accounts.

    The pre-accounts answer is deliberately the legacy layout rather than a
    `users/None/` directory: an install being upgraded must keep reading the
    agent memory and applications it already has, and moving them is §9's job,
    done once and deliberately, not something path resolution does by surprise.
    """
    if user_id is None:
        return UserPaths(
            workspace=_expand(settings.default_working_dir),
            agents=_expand(settings.agents_dir),
            applications=_expand(settings.applications_dir),
            attachments=_expand(settings.attachments_dir),
            large_prompts=_expand(settings.large_prompts_dir),
            codex_home=_expand(settings.codex_home_dir),
            research=_expand(settings.research_dir),
        )
    root = user_root(user_id)
    return UserPaths(
        workspace=root / "workspace",
        agents=root / "agents",
        applications=root / "applications",
        attachments=root / "attachments",
        large_prompts=root / "large-prompts",
        codex_home=root / "codex",
        research=root / "research",
    )


def normalise_root(raw: str) -> Path:
    """Vet one `extra_roots` entry, or raise `WorkspaceError`.

    An extra root is a hole in the confinement, so the rules are about what the
    hole may be rather than about typos:

    * **Absolute**, because a relative path means something different depending
      on where the server was started.
    * **Real, and a directory** — a path that does not exist yet would be
      allowed to become anything later, including a symlink somewhere else.
    * **Resolved**, so what is stored is what `confine()` will compare against;
      storing the unresolved spelling would let a symlink change the meaning of
      an approved root after the fact.
    * **Not the filesystem root, and not a home directory's parent**: `/` or
      `/home` do not open a repository, they switch confinement off while
      leaving it looking switched on.
    """
    text = (raw or "").strip()
    if not text:
        raise WorkspaceError("An extra root cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise WorkspaceError(f"{text} is not an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"{text} does not exist") from exc
    if not resolved.is_dir():
        raise WorkspaceError(f"{text} is not a directory")
    if resolved == Path(resolved.root) or resolved in (Path("/home"), Path("/Users")):
        raise WorkspaceError(
            f"{resolved} is too broad to be an extra root — name the directory "
            "you actually work in"
        )
    return resolved


def _within(candidate: Path, root: Path) -> bool:
    """Is the resolved candidate inside the captured canonical root?

    `Path.is_relative_to` rather than string prefixes, which say yes to
    `/home/archer-evil` for a root of `/home/archer`.
    """
    return candidate == root or candidate.is_relative_to(root)


def decode_extra_roots(raw: str | None) -> list[str]:
    """Read the directory grants already stored with an account."""
    if not raw:
        return []
    try:
        roots = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(root) for root in roots] if isinstance(roots, list) else []


def allowed_roots(user_id: str | None, extra_roots: list[str] | None = None) -> list[Path]:
    """Everywhere this account may put a working directory.

    The account's **root**, not just its workspace. The workspace is where a
    person works and stays the default, but the account owns more than that —
    an application's build session legitimately works inside that application's
    directory, which lives beside the workspace rather than in it.

    This does not weaken anything across accounts, which is the boundary that
    matters: one account's root contains only that account's files. It does
    mean an agent can read its own account's stored credentials, which it could
    anyway — the CLI it is already running holds them.
    """
    if user_id is None:
        return []
    # Extra roots were resolved when granted. Resolving them again would
    # approve a new target if a saved path or one of its ancestors became a
    # symlink. Only the requested access follows today's filesystem links.
    return [user_root(user_id).resolve(), *(Path(raw) for raw in extra_roots or [])]


def confine(
    requested: str | None,
    *,
    user_id: str | None,
    extra_roots: list[str] | None = None,
) -> str:
    """The absolute working directory a session may use, or raise.

    Resolved with symlinks followed *before* the comparison, because that is
    the only version of this check that means anything. A path that does not
    exist yet still resolves — `Path.resolve()` does not require existence —
    so creating a session in a directory the agent will make is still allowed,
    while `..` and symlinks are already collapsed by the time we compare.
    """
    paths = paths_for(user_id)
    if not requested:
        return str(paths.workspace.resolve())

    expanded = _expand(requested)
    if user_id is None:
        # Without an account, resolve against the process cwd; no account
        # workspace or allowed-root check applies.
        return str(expanded.resolve())

    # Account-relative paths start in the account workspace. Absolute and
    # expanded `~` paths are taken literally; both forms must pass the allowed
    # roots below. This selects a cwd, not an OS filesystem sandbox.
    if expanded.is_absolute():
        candidate = expanded.resolve()
    else:
        candidate = (paths.workspace / expanded).resolve()

    roots = allowed_roots(user_id, extra_roots)
    if any(_within(candidate, root) for root in roots):
        return str(candidate)
    raise WorkspaceError(
        f"{requested} is outside your workspace. Sessions work inside "
        f"{paths.workspace}."
    )

