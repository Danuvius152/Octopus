"""On-disk storage for user-uploaded session attachments.

Files live at:
    <users_root>/<user_id>/attachments/<session_id>/<attachment_id>__<sanitized_filename>

The root comes from the verified session owner's account. Filenames keep the
original extension (so the agent's `Read` tool picks the right handler), prefixed with a short
unique id so two uploads of `screenshot.png` don't collide.

Lifecycle: deleting a session closes its API and removes its rows. Published
file paths can still be held by native transcripts, forks, exports or an
authorized response. Without proof that these references have ended, the
account's original files remain in place. There is no automatic orphan GC.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .workspace import paths_for

MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB / file
MAX_ATTACHMENTS_PER_MESSAGE = 10
# Preserve the existing ASCII limit (12-byte id + '__' + 200-byte name),
# leaving room for the temporary suffix used when preserving fork attachments.
_MAX_DISK_NAME_BYTES = 214

# Strip path separators, NUL, and other control chars from the user-supplied
# filename. We keep the rest (incl. unicode) so the agent sees something
# close to what the user uploaded.
_UNSAFE_CHAR_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")
# `..` segments are removed entirely — collapsing them to `_` would still
# leave a recognisable shape that's easy to misread as path traversal.
_DOTDOT_RE = re.compile(r"\.{2,}")


class AttachmentError(ValueError):
    """Domain error raised by the attachments module.

    The HTTP layer maps these to 400/413 with the message visible to the
    client. Anything raised that isn't an AttachmentError is a bug and
    bubbles up as a 500.
    """


class AttachmentUnavailable(AttachmentError):
    """Reject the complete input without revealing another session's files."""

    code = "attachment_unavailable"

    def __init__(self, message="Some attachments are unavailable. Re-upload the files in this conversation."):
        super().__init__(message)


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    filename: str
    size: int
    mime_type: str
    path: Path  # absolute path on disk; what the agent's Read tool will open


def attachments_root(*, user_id: str) -> Path:
    """Return the verified account's attachment root without creating it.

    Created on demand by `session_dir`; we don't mkdir here so importing
    the module is a no-op.
    """
    if not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", user_id):
        raise AttachmentError("invalid attachment owner")
    root = paths_for(user_id).attachments
    if root.parent.is_symlink() or root.is_symlink():
        raise AttachmentError("invalid attachment account directory")
    return root.resolve()


def session_dir(session_id: str, *, user_id: str) -> Path:
    """Per-session subdirectory. Created if missing."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        # Defensive — session ids are uuid4 hex prefixes, never user input,
        # but a wrong id slipping in shouldn't be able to scribble outside
        # the root.
        raise AttachmentError(f"invalid session_id: {session_id!r}")
    d = attachments_root(user_id=user_id) / session_id
    if d.is_symlink():
        raise AttachmentError("invalid attachment directory")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_filename(name: str, byte_budget: int) -> str:
    """Reduce a user-supplied filename to something safe to put on disk.

    Reserve the actual id prefix before truncating UTF-8 at a whole character.
    Short extensions retain their existing meaning (at most 16 characters).
    """
    cleaned = _UNSAFE_CHAR_RE.sub("_", name or "").strip()
    cleaned = _DOTDOT_RE.sub("_", cleaned)
    cleaned = cleaned.strip("._") or "file"
    try:
        encoded = cleaned.encode("utf-8")
    except UnicodeError as error:
        raise AttachmentError("attachment filename must be valid UTF-8") from error
    if len(encoded) > byte_budget:
        # Preserve the extension when truncating.
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 16:
            suffix = "." + ext
            available = byte_budget - len(suffix.encode("utf-8"))
            cleaned = stem.encode("utf-8")[:available].decode("utf-8", errors="ignore") + suffix
        else:
            cleaned = encoded[:byte_budget].decode("utf-8", errors="ignore")
    return cleaned


def _detect_mime(filename: str, declared: str | None) -> str:
    """Trust the client's declared MIME if it looks plausible; otherwise
    fall back to the extension. application/octet-stream as a last resort.
    """
    if declared and "/" in declared and len(declared) < 200:
        return declared
    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def save_upload(
    session_id: str,
    filename: str,
    content: bytes,
    declared_mime: str | None = None,
    *,
    user_id: str,
) -> AttachmentRecord:
    """Persist `content` under the session's attachment dir and return its
    metadata. Raises AttachmentError on size cap or empty payload.
    """
    if not content:
        raise AttachmentError("attachment is empty")
    if len(content) > MAX_FILE_BYTES:
        raise AttachmentError(
            f"attachment exceeds {MAX_FILE_BYTES} bytes "
            f"({len(content)} given)"
        )

    attachment_id = uuid.uuid4().hex[:12]
    prefix = f"{attachment_id}__"
    sanitized = _sanitize_filename(filename, _MAX_DISK_NAME_BYTES - len(prefix.encode("utf-8")))
    # `<id>__<name>` so listing the dir is human-readable AND `id` alone
    # is enough to recover the file (we glob for `<id>__*`).
    on_disk_name = prefix + sanitized
    dest = session_dir(session_id, user_id=user_id) / on_disk_name
    dest.write_bytes(content)

    return AttachmentRecord(
        id=attachment_id,
        filename=sanitized,
        size=len(content),
        mime_type=_detect_mime(sanitized, declared_mime),
        path=dest.resolve(),
    )


def get_path(session_id: str, attachment_id: str, *, user_id: str) -> Path | None:
    """Resolve an attachment id back to its on-disk path. Returns None if
    the file isn't there (deleted, wrong id, etc.).

    Only the id is needed because the on-disk name is `<id>__<filename>`
    — we glob for the prefix so callers don't need to remember the
    sanitized filename.
    """
    root = attachments_root(user_id=user_id)
    if (not isinstance(attachment_id, str) or not re.fullmatch(r"[0-9a-f]{12}", attachment_id)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id)):
        return None
    d = root / session_id
    if not d.is_dir() or d.is_symlink():
        return None
    matches = list(d.glob(f"{attachment_id}__*"))
    if len(matches) != 1 or matches[0].is_symlink() or not matches[0].is_file():
        return None
    return matches[0].resolve()


def validate_message_attachments(session_id: str, identifiers: list[str], *, user_id: str) -> list[AttachmentRecord]:
    """Validate every required file at admission and again before execution.

    Files are retained for their owning session, including while requests are
    queued. No global lookup or fork fallback may broaden a new submission.
    Uploads accept any regular file type; the existing nonempty/size contract
    applies to the current stored bytes as well as the original upload.
    """
    attachments_root(user_id=user_id)
    if not isinstance(identifiers, list):
        raise AttachmentUnavailable()
    if len(identifiers) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise AttachmentUnavailable(f"too many attachments: max {MAX_ATTACHMENTS_PER_MESSAGE}")
    result = []
    for identifier in identifiers:
        try:
            path = get_path(session_id, identifier, user_id=user_id)
            if path is None:
                raise AttachmentUnavailable()
            observed = path.stat()
            if not stat.S_ISREG(observed.st_mode) or not 0 < observed.st_size <= MAX_FILE_BYTES:
                raise AttachmentUnavailable()
            filename = path.name.split("__", 1)[1]
            result.append(AttachmentRecord(identifier, filename, observed.st_size,
                                           _detect_mime(filename, None), path))
        except OSError as exc:
            raise AttachmentUnavailable() from exc
    return result


def blit_attachment(dest_session_id: str, src_path: Path, *, user_id: str) -> None:
    """Copy or confirm `src_path` in `dest_session_id`'s own attachment dir, preserving
    the `<id>__<filename>` on-disk name (session-rewind.md §5.5). Used on
    parent-delete to materialize files a descendant fork only referenced by
    metadata, before the parent's row is removed. An existing copy must
    contain the exact same bytes. A fork's own file can be its source when its
    ancestors are gone; it still receives file and directory durability checks.
    Failure keeps the source session retryable.
    """
    root = attachments_root(user_id=user_id)
    if (src_path.is_symlink() or src_path.parent.is_symlink()
            or src_path.parent.parent.resolve() != root):
        raise AttachmentError("invalid attachment source directory")
    dest = session_dir(dest_session_id, user_id=user_id) / src_path.name
    if dest.is_symlink() or not src_path.is_file():
        raise AttachmentError("invalid attachment destination")

    def digest(path: Path) -> bytes:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").digest()

    expected = digest(src_path)
    if dest.exists():
        if not dest.is_file() or digest(dest) != expected:
            raise AttachmentError("The descendant attachment differs from its source")
        with dest.open("rb") as retained:
            os.fsync(retained.fileno())
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{src_path.name}.", suffix=".tmp", dir=dest.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "rb") as output:
                shutil.copy2(src_path, temporary)
                if digest(temporary) != expected or digest(src_path) != expected:
                    raise AttachmentError("The attachment changed while preserving its descendant")
                os.fsync(output.fileno())
            try:
                os.link(temporary, dest)
            except FileExistsError:
                if dest.is_symlink() or not dest.is_file() or digest(dest) != expected:
                    raise AttachmentError("The descendant attachment differs from its source") from None
                with dest.open("rb") as retained:
                    os.fsync(retained.fileno())
        finally:
            temporary.unlink()
    # A successful return authorizes parent removal, including after a crash.
    # Persist the new file name and its session directory before that removal.
    for directory in (dest.parent, dest.parent.parent):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
