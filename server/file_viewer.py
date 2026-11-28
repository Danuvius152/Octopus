"""Open viewer files inside a bound directory and read checked objects."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from io import FileIO
from pathlib import Path
from time import monotonic
from typing import Literal

# Hard cap. PDFs and screenshots are the realistic ceiling; 2 MiB
# comfortably covers both without letting the model surface multi-MB
# log files that would freeze the browser.
MAX_FILE_BYTES = 2 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
_WORKING_DIR_UNAVAILABLE = "The session working directory is no longer available."
_SOURCE_UNAVAILABLE = "The file source is no longer available."
_OUTSIDE_SOURCE = "The file path is outside the authorized source."
_DOCUMENT_DIRECTORY_CHANGED = "The document directory changed. Reopen the source document."
_FILE_PATH_CHANGED = "The file path changed. Reopen the file to read it."
_FIND_ENTRY_LIMIT = 5_000
_FIND_RESULT_LIMIT = 100
_FIND_SECONDS = 0.2


# Extension → semantic kind the frontend renderer dispatches on.
# Keys are lowercase, with the leading dot.
FileKind = Literal["markdown", "code", "text", "image", "pdf"]
EXT_KINDS: dict[str, FileKind] = {
    # Markdown
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    # Images
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".bmp": "image",
    ".avif": "image",
    # PDF
    ".pdf": "pdf",
    # Code (rendered with syntax highlighting)
    ".py": "code",
    ".pyi": "code",
    ".js": "code",
    ".jsx": "code",
    ".ts": "code",
    ".tsx": "code",
    ".mjs": "code",
    ".cjs": "code",
    ".rs": "code",
    ".go": "code",
    ".java": "code",
    ".kt": "code",
    ".c": "code",
    ".h": "code",
    ".cpp": "code",
    ".hpp": "code",
    ".cs": "code",
    ".rb": "code",
    ".php": "code",
    ".swift": "code",
    ".scala": "code",
    ".sh": "code",
    ".bash": "code",
    ".zsh": "code",
    ".sql": "code",
    ".html": "code",
    ".css": "code",
    ".scss": "code",
    ".less": "code",
    ".vue": "code",
    ".lua": "code",
    ".r": "code",
    ".dart": "code",
    ".toml": "code",
    ".yaml": "code",
    ".yml": "code",
    ".json": "code",
    ".xml": "code",
    ".ini": "code",
    ".env": "code",
    ".dockerfile": "code",
    ".tf": "code",
    # Plain text
    ".txt": "text",
    ".log": "text",
    ".csv": "text",
    ".tsv": "text",
}


# Files whose *name* (case-insensitive, no extension) we also want
# to render. Treated as code-with-no-language.
ALLOWLISTED_BARE_NAMES = {
    "dockerfile",
    "makefile",
    "readme",
    "license",
    "notice",
    "changelog",
    "authors",
    ".env",
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
    ".prettierrc",
    ".eslintrc",
}


class FileViewerError(Exception):
    """Base. Each subclass maps to a distinct HTTP status."""


class PathRejected(FileViewerError):
    """Path or source did not meet the authorization and input rules."""


class FileNotFound(FileViewerError):
    """Resolved path does not point at a regular file."""

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


class WorkingDirectoryUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__(_WORKING_DIR_UNAVAILABLE)


class FileTooLarge(FileViewerError):
    """File exceeds MAX_FILE_BYTES."""


class UnsupportedType(FileViewerError):
    """Extension is not in the allowlist."""


class DocumentDirectoryChanged(FileViewerError):
    """The directory that supplied the Markdown document has changed."""


class FileChanged(FileViewerError):
    """The requested path binding or the checked byte count changed."""


def _path_id(root: Path, path: Path, directory: os.stat_result) -> str:
    identity = f"{directory.st_dev}:{directory.st_ino}".encode("ascii")
    return hashlib.sha256(b"\0".join((os.fsencode(root), os.fsencode(path), identity))).hexdigest()


@dataclass(frozen=True)
class ResourceDirectory:
    path: str
    path_id: str


@dataclass(frozen=True)
class FileSources:
    cwd: Path | None
    roots: tuple[Path, ...]
    blocked_roots: tuple[Path, ...] = ()

    def select(self, requested: Path, source: str | None, *, bound_to_cwd: bool) -> Path:
        if source is not None:
            for root in self.roots:
                if str(root) == source:
                    return root
            raise PathRejected(_SOURCE_UNAVAILABLE)
        if bound_to_cwd or not requested.is_absolute():
            if self.cwd is None:
                raise PathRejected(_WORKING_DIR_UNAVAILABLE)
            return self.cwd
        if any(requested.is_relative_to(root) for root in self.blocked_roots):
            raise PathRejected(_SOURCE_UNAVAILABLE)
        if self.cwd is not None and requested.is_relative_to(self.cwd):
            return self.cwd
        selected = max((root for root in self.roots if requested.is_relative_to(root)),
                       key=lambda root: len(root.parts), default=None)
        if selected is None:
            raise PathRejected(_OUTSIDE_SOURCE)
        return selected


@dataclass(frozen=True)
class ResolvedFile:
    """Validated, ready-to-serve descriptor.

    `kind` is the renderer hint the frontend uses to pick its
    component (markdown / image / pdf / code / text). `mime_type`
    is the Content-Type for the HTTP response.
    """

    abs_path: Path
    source_root: Path
    relative_path: str
    kind: FileKind
    mime_type: str
    stat_result: os.stat_result
    parent_stat: os.stat_result
    is_cwd_source: bool

    @property
    def size(self) -> int:
        return self.stat_result.st_size

    @property
    def canonical_path_id(self) -> str:
        return _path_id(self.source_root, self.abs_path, self.parent_stat)

    @property
    def canonical_directory(self) -> ResourceDirectory:
        return ResourceDirectory(
            str(Path(self.relative_path).parent),
            _path_id(self.source_root, self.abs_path.parent, self.parent_stat),
        )


def _classify(path: Path) -> FileKind:
    suffix = path.suffix.lower()
    if suffix in EXT_KINDS:
        return EXT_KINDS[suffix]
    if path.name.lower() in ALLOWLISTED_BARE_NAMES:
        return "code"
    raise UnsupportedType(
        f"Unsupported file type: {path.name!r}. "
        "The viewer renders markdown, code, images, PDFs, and plain text."
    )


def _mime_for(path: Path, kind: FileKind) -> str:
    # mimetypes covers the common ones; we override only where its
    # default is wrong for what the browser wants.
    if kind == "markdown":
        return "text/markdown; charset=utf-8"
    if kind == "pdf":
        return "application/pdf"
    if kind == "image":
        guessed, _ = mimetypes.guess_type(path.name)
        if guessed:
            return guessed
        # SVG is sometimes missed depending on the platform mimetypes DB.
        if path.suffix.lower() == ".svg":
            return "image/svg+xml"
        return "application/octet-stream"
    if kind == "code" or kind == "text":
        # Always text/plain so the browser never tries to execute or
        # download. Charset matters — files are read as UTF-8.
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


@contextmanager
def _directory_fd(path: Path, *, root_fd: int | None = None) -> Iterator[int]:
    if root_fd is None:
        fd = os.open(path.anchor, _DIRECTORY_FLAGS)
        parts = path.parts[1:]
    else:
        fd = os.dup(root_fd)
        parts = path.parts
    try:
        for part in parts:
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            previous_fd, fd = fd, child_fd
            os.close(previous_fd)
        yield fd
    finally:
        os.close(fd)


def _fd_path(fd: int) -> str:
    return f"/proc/self/fd/{fd}"


def _has_path(fd: int, path: Path) -> bool:
    return os.readlink(_fd_path(fd)) == str(path)


@contextmanager
def _open_available_directory(path: Path) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute directory path")
    with _directory_fd(path) as fd:
        if not _has_path(fd, path):
            raise OSError("Directory path changed")
        yield fd


def _available_directory(path: Path) -> bool:
    try:
        with _open_available_directory(path):
            return True
    except (OSError, ValueError):
        return False


def _source_blocks_fallback(path: Path) -> bool:
    if not path.is_absolute() or ".." in path.parts:
        return False
    # A symlink may be dangling. Inspect the entries in order before following
    # any component, and keep inaccessible paths out of implicit fallback.
    try:
        return any(stat.S_ISLNK(entry.lstat().st_mode) for entry in (*reversed(path.parents), path))
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return False
    except OSError:
        return True


def _account_root(workspace: Path) -> Path:
    account = workspace.parent
    return account.parent.resolve() / account.name


@contextmanager
def open_session_working_directory(
    working_dir: str, workspace: Path, extra_roots: Sequence[str], *, expected: os.stat_result | None = None,
) -> Iterator[int]:
    """Pin the approved cwd for launch or for validating the later candidate."""
    cwd = Path(working_dir)
    with ExitStack() as stack:
        try:
            if not any(cwd.is_relative_to(root) for root in (_account_root(workspace), *map(Path, extra_roots))):
                raise WorkingDirectoryUnavailable()
            # Every component, including the matching grant, is opened without following symlinks.
            fd = stack.enter_context(_open_available_directory(cwd))
            if expected is not None and not os.path.samestat(os.fstat(fd), expected):
                raise WorkingDirectoryUnavailable()
        except (OSError, ValueError) as error:
            raise WorkingDirectoryUnavailable() from error
        yield fd


def file_sources(
    working_dir: str | Path, *, workspace: Path | None = None, extra_roots: Sequence[str] = (),
) -> FileSources:
    """Identify the usable sources of this session under the current grants."""
    cwd = Path(working_dir)
    if workspace is None:
        # The pre-accounts installation supplied its own (possibly relative) cwd.
        try:
            cwd = cwd.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return FileSources(None, ())
        return FileSources(cwd, (cwd,)) if _available_directory(cwd) else FileSources(None, ())

    # The install's configured users directory is trusted. Its account and
    # workspace entries, and saved extra roots, must still be real directories.
    account_root = _account_root(workspace)
    workspace = account_root / workspace.name
    availability = {root: _available_directory(root) for root in dict.fromkeys((workspace, *map(Path, extra_roots)))}
    cwd_grants = (account_root, *(root for root, available in availability.items() if available))
    if any(cwd.is_relative_to(grant) for grant in cwd_grants) and cwd not in availability:
        availability[cwd] = _available_directory(cwd)
    # An implicit absolute open cannot turn an unavailable source into an
    # alias via an overlapping grant. A new path may name the actual target.
    blocked = tuple(root for root, available in availability.items() if not available and _source_blocks_fallback(root))
    usable_cwd = cwd if availability.get(cwd) else None
    roots = tuple(root for root, available in availability.items() if available)
    return FileSources(usable_cwd, roots, blocked)


@dataclass(frozen=True)
class FilePickerEntry:
    path: str
    name: str
    kind: Literal["directory", "file"]


@dataclass(frozen=True)
class FilePickerResult:
    entries: list[FilePickerEntry]
    skipped: bool
    truncated: bool = False


@contextmanager
def _picker_directory(
    root: Path, root_fd: int, relative: Path, source_directory: ResourceDirectory | None = None,
) -> Iterator[Iterator[os.DirEntry[str]]]:
    with ExitStack() as stack:
        if source_directory is None:
            expected = root / relative
            fd = stack.enter_context(_directory_fd(relative, root_fd=root_fd))
        else:
            expected, fd = stack.enter_context(_document_directory(root, root_fd, source_directory))
        def check_directory() -> None:
            if not _has_path(fd, expected):
                if source_directory is not None:
                    raise DocumentDirectoryChanged(_DOCUMENT_DIRECTORY_CHANGED)
                raise PathRejected(_SOURCE_UNAVAILABLE)

        check_directory()
        # The descriptor was opened without following any directory symlinks.
        with os.scandir(_fd_path(fd)) as entries:
            yield entries
        if not _has_path(root_fd, root):
            raise PathRejected(_SOURCE_UNAVAILABLE)
        check_directory()


def _picker_entry(parent: Path, entry: os.DirEntry[str]) -> FilePickerEntry | None:
    # POSIX names without a UTF-8 representation can't round-trip through the viewer API.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in entry.name):
        return None
    path = str(parent / entry.name)
    if entry.is_dir(follow_symlinks=False):
        return FilePickerEntry(path, entry.name, "directory")
    if entry.is_file(follow_symlinks=False):
        return FilePickerEntry(path, entry.name, "file")
    return None


def browse_files(
    sources: FileSources, source: str, directory: str, check_cancelled: Callable[[], None],
    *, source_directory: ResourceDirectory | None = None,
) -> FilePickerResult:
    root = sources.select(Path(), source, bound_to_cwd=False)
    directory = source_directory.path if source_directory is not None else directory
    relative = Path(directory)
    if relative.is_absolute() or ".." in relative.parts or "\0" in directory:
        raise PathRejected("Invalid directory path.")
    entries = []
    skipped = False
    try:
        with _directory_fd(root) as root_fd, _picker_directory(root, root_fd, relative, source_directory) as children:
            for child in children:
                check_cancelled()
                item = _picker_entry(relative, child)
                if item is None:
                    skipped = True
                else:
                    entries.append(item)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise FileNotFound("Directory not found or unavailable.") from error
    except OSError as error:
        raise PathRejected("The directory could not be listed.") from error
    entries.sort(key=lambda item: (item.kind != "directory", item.name.casefold(), item.name))
    return FilePickerResult(entries, skipped)


def find_files(sources: FileSources, source: str, query: str, check_cancelled: Callable[[], None]) -> FilePickerResult:
    root = sources.select(Path(), source, bound_to_cwd=False)
    if not query:
        raise PathRejected("A filename search term is required.")
    needle = query.casefold()
    deadline = monotonic() + _FIND_SECONDS
    pending = deque([Path()])
    matches: list[FilePickerEntry] = []
    examined = 0
    skipped = truncated = False
    try:
        with _directory_fd(root) as root_fd:
            while pending and not truncated:
                check_cancelled()
                if monotonic() >= deadline:
                    truncated = True
                    break
                parent = pending.popleft()
                children: list[Path] = []
                found: list[FilePickerEntry] = []
                try:
                    with _picker_directory(root, root_fd, parent) as entries:
                        for entry in entries:
                            check_cancelled()
                            if monotonic() >= deadline:
                                truncated = True
                                break
                            examined += 1
                            item = _picker_entry(parent, entry)
                            if item is None:
                                skipped = True
                            elif item.kind == "directory":
                                children.append(Path(item.path))
                            elif needle in item.name.casefold():
                                found.append(item)
                            if examined >= _FIND_ENTRY_LIMIT or len(matches) + len(found) >= _FIND_RESULT_LIMIT:
                                truncated = True
                                break
                except (OSError, PathRejected):
                    if parent == Path():
                        raise
                    skipped = True
                    continue
                pending.extend(children)
                matches.extend(found)
            if not _has_path(root_fd, root):
                raise PathRejected(_SOURCE_UNAVAILABLE)
    except OSError as error:
        raise PathRejected("The source directory could not be searched.") from error
    matches.sort(key=lambda item: (item.name.casefold(), item.path))
    return FilePickerResult(matches, skipped, truncated)


@contextmanager
def _document_directory(
    root: Path, root_fd: int, source_directory: ResourceDirectory,
) -> Iterator[tuple[Path, int]]:
    directory = Path(source_directory.path)
    if directory.is_absolute() or ".." in directory.parts or "\0" in source_directory.path:
        raise PathRejected("Invalid document directory")
    resolved = root / directory
    with ExitStack() as stack:
        try:
            fd = stack.enter_context(_directory_fd(directory, root_fd=root_fd))
            if _path_id(root, resolved, os.fstat(fd)) != source_directory.path_id:
                raise DocumentDirectoryChanged(_DOCUMENT_DIRECTORY_CHANGED)
        except OSError as error:
            raise DocumentDirectoryChanged(_DOCUMENT_DIRECTORY_CHANGED) from error
        yield resolved, fd


@contextmanager
def _open_regular_file(root_fd: int, relative: Path) -> Iterator[tuple[FileIO, os.stat_result, int]]:
    with ExitStack() as stack:
        try:
            parent_fd = stack.enter_context(_directory_fd(relative.parent, root_fd=root_fd))
            object_fd = os.open(relative.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent_fd)
            stack.callback(os.close, object_fd)
        except FileNotFoundError as error:
            raise FileNotFound("File not found.", missing=True) from error
        except NotADirectoryError as error:
            raise FileNotFound("File not found.") from error
        except OSError as error:
            raise PathRejected("The file path could not be opened.") from error

        # Inspect the object before opening it for I/O. A read open on a FIFO
        # could block, and opening a device may have side effects.
        identity = os.fstat(object_fd)
        if stat.S_ISLNK(identity.st_mode):
            raise FileChanged(_FILE_PATH_CHANGED)
        if not stat.S_ISREG(identity.st_mode):
            raise FileNotFound("The target is not a regular file.")
        with FileIO(_fd_path(object_fd), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not os.path.samestat(identity, info):
                raise FileChanged(_FILE_PATH_CHANGED)
            yield stream, info, parent_fd


def load_safe_file(
    sources: FileSources,
    requested: str,
    *,
    source: str | None = None,
    read_content: bool = False,
    canonical: bool = False,
    canonical_path_id: str | None = None,
    source_directory: ResourceDirectory | None = None,
    expected_cwd: os.stat_result | None = None,
) -> tuple[ResolvedFile, bytes]:
    """Inspect a file and optionally snapshot its bytes in one owned operation."""
    if not requested or not requested.strip():
        raise PathRejected("Empty path")
    if "\0" in requested:
        raise PathRejected("Invalid file path")
    if not canonical and canonical_path_id is None and source_directory is None and requested.startswith("~"):
        raise PathRejected("Tilde paths are not allowed; use a working-dir-relative path")

    path = Path(requested)
    if expected_cwd is not None and not path.is_absolute() and sources.cwd is None:
        raise WorkingDirectoryUnavailable()
    root = sources.select(path, source, bound_to_cwd=canonical_path_id is not None or source_directory is not None)
    if path.is_absolute() and (source_directory is not None or not path.is_relative_to(root)):
        raise PathRejected(_OUTSIDE_SOURCE)
    with ExitStack() as stack:
        try:
            root_fd = stack.enter_context(_directory_fd(root))
            if expected_cwd is not None and root == sources.cwd and not os.path.samestat(os.fstat(root_fd), expected_cwd):
                raise WorkingDirectoryUnavailable()
        except OSError as error:
            detail = _WORKING_DIR_UNAVAILABLE if root == sources.cwd and source is None else _SOURCE_UNAVAILABLE
            raise PathRejected(detail) from error

        document = None
        if source_directory is not None:
            document = stack.enter_context(_document_directory(root, root_fd, source_directory))
        candidate = (document[0] if document else root) / path
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise PathRejected("Invalid file path") from error
        if not resolved.is_relative_to(root):
            raise PathRejected(_OUTSIDE_SOURCE)
        if resolved == root:
            raise FileNotFound("The target is not a regular file.")

        relative = resolved.relative_to(root)
        try:
            with _open_regular_file(root_fd, relative) as (stream, info, parent_fd):
                opened_fd = stream.fileno()
                if not _has_path(opened_fd, resolved) or not _has_path(parent_fd, resolved.parent):
                    raise FileChanged(_FILE_PATH_CHANGED)
                if not _has_path(root_fd, root):
                    raise PathRejected(_SOURCE_UNAVAILABLE)
                if document and not _has_path(document[1], document[0]):
                    raise DocumentDirectoryChanged(_DOCUMENT_DIRECTORY_CHANGED)
                parent_stat = os.fstat(parent_fd)
                if canonical_path_id is not None and (
                    str(relative) != requested or _path_id(root, resolved, parent_stat) != canonical_path_id
                ):
                    raise FileChanged(_FILE_PATH_CHANGED)
                if info.st_size > MAX_FILE_BYTES:
                    raise FileTooLarge(f"File is {info.st_size} bytes; viewer limit is {MAX_FILE_BYTES} bytes")
                kind = _classify(resolved)
                metadata = ResolvedFile(
                    resolved, root, str(relative), kind, _mime_for(resolved, kind), info, parent_stat,
                    is_cwd_source=root == sources.cwd,
                )
                if not read_content:
                    return metadata, b""
                return metadata, _read_snapshot(stream, info.st_size)
        except FileNotFound:
            if not _has_path(root_fd, root):
                raise PathRejected(_SOURCE_UNAVAILABLE)
            raise
        except OSError as error:
            raise PathRejected("The file could not be read.") from error


def _read_snapshot(stream: FileIO, size: int) -> bytes:
    buffer = bytearray(size)
    remaining = memoryview(buffer)
    while remaining:
        count = stream.readinto(remaining)
        if not count:
            raise FileChanged("The file size changed. Reopen the file to read it.")
        remaining = remaining[count:]
    return bytes(buffer)


def resolve_safe_path(
    working_dir: str | Path,
    requested: str,
    *,
    canonical: bool = False,
    source_directory: ResourceDirectory | None = None,
) -> ResolvedFile:
    metadata, _ = load_safe_file(
        file_sources(working_dir), requested, canonical=canonical, source_directory=source_directory,
    )
    return metadata
