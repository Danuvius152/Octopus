"""Classify a `/showme` argument and interpret descriptions using recent context."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .file_viewer import ALLOWLISTED_BARE_NAMES
from .harness import HarnessCredential, OneShotContext
from .harness.native_output import recorded_file_change_paths
from .models import (
    FileHeadingPosition,
    FileLinePosition,
    FilePosition,
    InvalidFilePosition,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShowMeResolution:
    path: str | None
    message: str | None = None


# Path-like heuristic for the bare-token fallback: a filename with an
# extension, or a relative path made of segments like that. Deliberately
# narrow so prose like "the README" doesn't get treated as a path.
_PATH_LIKE_RE = re.compile(r"^[\w./\-]+\.[A-Za-z0-9]{1,8}$")
_FILENAME_SUFFIX = re.compile(r"\.[A-Za-z][A-Za-z0-9_-]*$")
_COLON_LINE = re.compile(r":(-?[0-9]+)\Z")
_HASH_LINES = re.compile(r"#L(-?[0-9]+)(?:-L(-?[0-9]+))?\Z")
_MAX_SAFE_LINE = 2**53 - 1


@dataclass(frozen=True)
class ShowMeTarget:
    path: str
    position: FilePosition | None = None
    quoted: bool = False
    source: str | None = None
    ambiguous_filename: bool = False

    @property
    def requested_path(self) -> str:
        return f"./{self.path}" if self.quoted and self.path.startswith("~") else self.path


def _path_like(text: str) -> bool:
    return "/" in text or text.lower() in ALLOWLISTED_BARE_NAMES or bool(_FILENAME_SUFFIX.search(text))


def _bare_target(path: str, position: FilePosition | None = None) -> ShowMeTarget:
    return ShowMeTarget(path, position, ambiguous_filename="/" not in path and any(char.isspace() for char in path))


def _positive_line(value: str) -> int:
    digits = value.lstrip("0")
    if value.startswith("-") or len(digits) > 16:
        return 0
    return int(digits or "0")


def _file_position(suffix: str) -> FilePosition:
    match = _COLON_LINE.fullmatch(suffix) or _HASH_LINES.fullmatch(suffix)
    if match:
        start_text, *end_text = match.groups()
        start = _positive_line(start_text)
        end = _positive_line(end_text[0]) if end_text and end_text[0] is not None else None
        if 1 <= start <= _MAX_SAFE_LINE and (end is None or start <= end <= _MAX_SAFE_LINE):
            if end is None:
                return FileLinePosition(kind="lines", start=start)
            return FileLinePosition(kind="lines", start=start, end=end)
    elif suffix.startswith("#") and len(suffix) > 1:
        return FileHeadingPosition(kind="heading", slug=suffix[1:])
    return InvalidFilePosition(kind="invalid", suffix=suffix)


def explicit_showme_target(text: str) -> ShowMeTarget | None:
    """Interpret an explicit file target without consulting the filesystem."""
    target = text.strip()
    target.encode("utf-8")
    if target.startswith('"'):
        decoder = json.JSONDecoder()
        try:
            path, end = decoder.raw_decode(target)
            path.encode("utf-8")
        except ValueError as error:
            raise ValueError("Enter a file path in double quotes.") from error
        suffix = target[end:].lstrip()
        source = None
        option = suffix.split(maxsplit=1)
        if option and option[0] == "--source":
            value = option[1] if len(option) == 2 else ""
            try:
                if not value.startswith('"') or not path.startswith("/"):
                    raise ValueError("Expected quoted absolute paths")
                source, end = decoder.raw_decode(value)
                if not source.startswith("/"):
                    raise ValueError("Expected an absolute source")
                source.encode("utf-8")
            except ValueError as error:
                raise ValueError("Use double-quoted absolute paths for the file and its source.") from error
            suffix = value[end:].lstrip()
        if suffix and not suffix.startswith(("#", ":")):
            raise ValueError("Put the file position after the closing quote.")
        return ShowMeTarget(path, _file_position(suffix) if suffix else None, quoted=True, source=source)

    line_fragment = _HASH_LINES.search(target)
    if line_fragment and _path_like(path := target[:line_fragment.start()]):
        return _bare_target(path, _file_position(line_fragment[0]))
    match = _COLON_LINE.search(target)
    candidate = target[:match.start()] if match else target
    path, marker, fragment = candidate.rpartition("#")
    # A bare hash followed by a filename tail can be part of a legacy path.
    # Dots are stripped from generated heading slugs.
    if marker and _path_like(path) and "/" not in fragment and not _FILENAME_SUFFIX.search(fragment):
        return _bare_target(path, _file_position(target[len(path):]))
    if match and _path_like(candidate):
        return _bare_target(candidate, _file_position(match[0]))
    if _path_like(target):
        return _bare_target(target)
    return None


def extract_json(model_text: str) -> dict | None:
    """Pull a JSON object out of the model's reply, tolerating ```json
    fences and surrounding prose. Returns None if no object is parseable."""
    s = (model_text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _bare_path_fallback(model_text: str) -> str | None:
    """If the model replied with a single path-shaped token (very common —
    `claude --print` often just answers `README.md`), use it as a path."""
    s = (model_text or "").strip().strip("`'\"")
    # First non-empty line only — the model sometimes adds a trailing
    # confirmation sentence after the path.
    first = next((ln.strip().strip("`'\"") for ln in s.splitlines() if ln.strip()), "")
    return first if first and _PATH_LIKE_RE.match(first) else None


def _format_messages(messages: list[dict[str, Any]], limit: int = 12) -> str:
    lines: list[str] = []
    for msg in messages[-limit:]:
        role = msg.get("role")
        typ = msg.get("type")
        text = msg.get("content")
        if role == "user" and typ == "text" and isinstance(text, str):
            lines.append(f"User: {text}")
        elif role == "assistant" and typ == "text" and isinstance(text, str):
            lines.append(f"Assistant: {text}")
        elif typ in {"tool_use", "tool_result"}:
            tool = msg.get("tool_name") or ""
            paths = recorded_file_change_paths(msg) if tool == "fileChange" else []
            tool_input = msg.get("tool_input")
            if not paths and typ == "tool_use" and isinstance(tool_input, dict):
                paths = [path for key in ("path", "file_path") if isinstance(path := tool_input.get(key), str) and path]
            lines.extend(f"Assistant used {tool} with path={path!r}" for path in dict.fromkeys(paths))
    return "\n".join(lines)


def _build_prompt(text: str, working_dir: str, session_name: str | None, messages_blurb: str) -> str:
    return (
        "Resolve the user's `/showme` reference to a concrete file path.\n"
        "\n"
        "Respond with a valid JSON object ONLY — no prose before or after, "
        "no markdown fences. Choose exactly ONE shape:\n"
        '  {"path": "<working-directory-relative path or an absolute path mentioned in the conversation>"}\n'
        '  {"message": "<short clarifying question if too ambiguous>"}\n'
        "\n"
        "Rules:\n"
        "- Prefer the single most likely file the user means, based on the "
        "reference and recent conversation.\n"
        '- Common conventions: "the readme" → README.md; "the changelog" → '
        "CHANGELOG.md; etc.\n"
        "- Use a path relative to the working directory or copy an absolute path from the recent conversation.\n"
        "- Before returning a path, double-check it against the recent "
        "conversation (tool calls, file reads, mentions) to make sure the "
        "file actually exists. If you are not confident it exists, ask for "
        "clarification instead.\n"
        '- Only return {"message": ...} when the reference is genuinely '
        "ambiguous or you cannot confirm the file exists — don't ask when "
        "a confident, verifiable path is available.\n"
        "\n"
        f"Working directory: {working_dir}\n"
        f"Session: {session_name or '(unnamed)'}\n"
        f"User reference: {text}\n"
        "\n"
        "Recent conversation:\n"
        f"{messages_blurb or '(none — this is the first interaction)'}\n"
        "\n"
        "Your JSON response:"
    )


async def resolve_showme_reference(
    text: str,
    *,
    harness,
    model: str | None,
    credential: HarnessCredential | None,
    working_dir: str,
    messages: list[dict[str, Any]],
    session_name: str | None = None,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
    runner=None,
) -> ShowMeResolution:
    """Resolve a description using the session's model and recent context."""
    prompt = _build_prompt(text, working_dir, session_name, _format_messages(messages))
    ctx = OneShotContext(
        prompt=prompt, model=model, credential=credential, working_dir=working_dir,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
    )
    out = await (runner or harness.run_oneshot)(ctx)

    obj = extract_json(out)
    if obj is not None:
        path = obj.get("path")
        message = obj.get("message")
        if isinstance(path, str) and path.strip():
            return ShowMeResolution(path=path)
        if isinstance(message, str) and message.strip():
            return ShowMeResolution(path=None, message=message.strip())

    # The model can reply with a bare path instead of the requested JSON.
    bare = _bare_path_fallback(out)
    if bare is not None:
        return ShowMeResolution(path=bare)

    logger.warning(
        "showme: couldn't parse model output (text=%r, len=%d): %r",
        text,
        len(out or ""),
        (out or "")[:400],
    )
    return ShowMeResolution(
        path=None,
        message="Couldn't pin down a file from that reference. Try giving the path directly.",
    )
