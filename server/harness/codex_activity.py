"""Read-only Codex liveness metadata, bound to one captured subprocess.

Neither tracing lines nor native item contents leave this adapter. A current
process turn UUID, its stdout thread UUID and the rollout's own session UUID
must agree before a fresh item completion can touch an attempt clock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)
_DIAGNOSTIC_ENV = "error,codex_exec=info,opentelemetry_sdk=off,opentelemetry_otlp=off"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TRACE = re.compile(
    r'^\S+\s+(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+'
    r'(?:codex\.exec\{otel\.kind="internal"(?: thread\.id="(?P<thread>[0-9a-f-]{36})")?\}:\s*)?'
    r'codex_exec:\s*(?P<message>.*)$'
)
_TRACE_LEVEL = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+(?:TRACE|DEBUG|INFO|WARN|ERROR)\b", re.I)
_TURN = re.compile(r"^Sent prompt with event ID: (?P<id>[0-9a-f-]{36})$")
# Restrict decoding to envelopes known to contain activity metadata. Native
# response_item bodies (including encrypted/raw reasoning) are never decoded.
_METADATA = re.compile(rb'^\s*\{\s*(?:"timestamp"\s*:\s*"[^"\\]*"\s*,\s*)?(?:"ordinal"\s*:\s*\d+\s*,\s*)?"type"\s*:\s*"(?P<type>session_meta|event_msg)"')
_ITEM_COMPLETION = re.compile(rb'\s*,\s*"payload"\s*:\s*\{\s*"type"\s*:\s*"item_completed"')
_MAX_LINE = 256 * 1024
_MAX_READ = 1024 * 1024
_MAX_DISCOVERY = 1024
_MAX_ITEMS = 4096
_MAX_DELAY = 30.0


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = str(UUID(value))
    except ValueError:
        return None
    return normalized if normalized == value else None


class CodexActivityObserver:
    """An owned incremental cursor; polling alone never produces activity."""

    def __init__(
        self, env: Mapping[str, str], notify: Callable[[float], None], *,
        resume_id: str | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = Path(env.get("CODEX_HOME") or Path.home() / ".codex").resolve() / "sessions"
        self._notify = notify
        self._wall = wall_clock
        self._monotonic = monotonic
        self._started = wall_clock()
        self._thread = _uuid(resume_id)
        self._stdout_thread: str | None = None
        self._diagnostic_thread: str | None = None
        self._turn: str | None = None
        self._path: Path | None = None
        self._inode: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._skipping_line = False
        self._file_verified = False
        self._seen: set[str] = set()
        self._closed = False
        self._disabled = False
        self._warning: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[list[float]] | None = None
        self._stderr_continuation = False
        self.matched_records = 0

    @property
    def identity_confirmed(self) -> bool:
        return bool(self._file_verified and self._turn and self._stdout_thread == self._thread)

    def _disable(self, code: str) -> None:
        self._disabled = True
        if self._warning is None:
            self._warning = code
            # Codes only: no thread, pathname, diagnostic or native body.
            logger.warning("Codex supplementary activity unavailable (%s); using stdout activity", code)

    def consume_stderr(self, line: str) -> bool:
        """Consume every newly enabled exec diagnostic before any retention."""
        clean = _ANSI.sub("", line)
        if _TRACE_LEVEL.match(clean):
            self._stderr_continuation = False
        match = _TRACE.fullmatch(clean)
        if match and match["level"] in ("INFO", "WARN", "DEBUG", "TRACE"):
            self._stderr_continuation = match["level"] != "INFO"
            if match["level"] == "INFO":
                identity = _TURN.fullmatch(match["message"])
                if identity:
                    turn = _uuid(identity["id"])
                    diagnostic_thread = _uuid(match["thread"]) if match["thread"] else None
                    if match["thread"] and diagnostic_thread is None:
                        self._disable("thread_identity_conflict")
                    if diagnostic_thread is not None:
                        if self._thread is not None and self._thread != diagnostic_thread:
                            self._disable("thread_identity_conflict")
                        self._diagnostic_thread = diagnostic_thread
                    if turn is None or (self._turn is not None and self._turn != turn):
                        self._disable("turn_identity_conflict")
                    elif not self._closed:
                        self._turn = turn
                elif not match["message"].startswith(("Codex initialized with event:", "Sent review request with event ID:")):
                    self._stderr_continuation = True
            return True
        # A formatter change must not accidentally expose the new diagnostics.
        if "codex_exec" in clean and re.search(r"\b(?:INFO|WARN|DEBUG|TRACE)\b", clean, re.I):
            self._stderr_continuation = True
            if "Sent prompt with event ID:" in clean:
                self._disable("diagnostic_format")
            return True
        if "Sent prompt with event ID:" in clean or "Codex initialized with event:" in clean:
            self._disable("diagnostic_format")
            self._stderr_continuation = True
            return True
        # Display-formatted WARN errors may contain raw newlines. Unmarked
        # continuation lines remain diagnostic data; the next structured
        # record (including existing ERROR) restores normal framing.
        return self._stderr_continuation

    def observe_stdout(self, obj: dict[str, Any]) -> None:
        if self._closed or self._disabled or obj.get("type") != "thread.started":
            return
        thread = _uuid(obj.get("thread_id"))
        if (thread is None or (self._thread is not None and self._thread != thread)
                or (self._diagnostic_thread is not None and self._diagnostic_thread != thread)):
            self._disable("thread_identity_conflict")
            return
        self._thread = self._stdout_thread = thread

    def _discover(self) -> Path | None:
        if self._root.is_symlink():
            self._disable("rollout_root_symlink")
            return None
        if self._thread is None or not self._root.is_dir():
            return None
        # UUIDv7 carries the native thread's creation date. It is only a
        # bounded directory hint; the file/header/turn checks still establish
        # ownership. Adjacent UTC dates cover the CLI's local-date partition.
        identifier = UUID(self._thread)
        if identifier.version == 7:
            born = datetime.fromtimestamp((identifier.int >> 80) / 1000, UTC)
            candidates = [self._root / (born + timedelta(days=offset)).strftime("%Y/%m/%d")
                          for offset in (-1, 0, 1)]
            matches = []
            remaining = _MAX_DISCOVERY
            for directory in candidates:
                if not directory.exists():
                    continue
                if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents
                                                  if parent != self._root.parent):
                    self._disable("rollout_directory_symlink")
                    return None
                with os.scandir(directory) as entries:
                    for entry in entries:
                        remaining -= 1
                        if remaining < 0:
                            self._disable("discovery_limit")
                            return None
                        if (not entry.is_symlink() and entry.is_file(follow_symlinks=False)
                                and entry.name.endswith(f"-{self._thread}.jsonl")):
                            matches.append(Path(entry.path))
            if matches:
                if len(matches) > 1:
                    self._disable("rollout_identity_ambiguous")
                    return None
                return matches[0]
        remaining = _MAX_DISCOVERY
        pending = [(self._root, 0)]
        matches = []
        while pending and remaining:
            directory, depth = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    remaining -= 1
                    if remaining < 0:
                        self._disable("discovery_limit")
                        return None
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False) and depth < 3:
                        pending.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(f"-{self._thread}.jsonl"):
                        matches.append(Path(entry.path))
        if pending:
            self._disable("discovery_limit")
            return None
        if len(matches) > 1:
            self._disable("rollout_identity_ambiguous")
            return None
        return matches[0] if matches else None

    def _open(self) -> int | None:
        if self._path is None:
            self._path = self._discover()
        if self._path is None:
            return None
        # Open each component relative to captured directory descriptors. This
        # also rejects a parent replaced by a symlink between discovery/read.
        directory_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            relative = self._path.relative_to(self._root)
            for component in relative.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            self._disable("rollout_not_regular")
            return None
        inode = (info.st_dev, info.st_ino)
        if self._inode is not None and (inode != self._inode or info.st_size < self._offset):
            os.close(fd)
            self._disable("rollout_replaced")
            return None
        if self._inode is None:
            raw_header = os.read(fd, _MAX_LINE)
            if b"\n" not in raw_header:
                os.close(fd)
                return None
            header = raw_header.split(b"\n", 1)[0]
            obj = self._decode_metadata(header)
            payload = obj.get("payload") if obj else None
            if not obj or obj.get("type") != "session_meta" or not isinstance(payload, dict) or payload.get("id") != self._thread:
                os.close(fd)
                self._disable("rollout_identity_mismatch")
                return None
            self._inode = inode
            self._file_verified = True
        os.lseek(fd, self._offset, os.SEEK_SET)
        return fd

    def _decode_metadata(self, line: bytes) -> dict[str, Any] | None:
        envelope = _METADATA.match(line)
        if envelope is None:
            if re.search(rb'"type"\s*:\s*"item_completed"', line):
                self._disable("activity_envelope")
            return None
        if envelope["type"] == b"event_msg":
            # Only this event has the lifecycle metadata we consume. Avoid
            # decoding chat-like agent_message/reasoning-content events.
            suffix = line[envelope.end():]
            if not _ITEM_COMPLETION.match(suffix):
                if re.search(rb'"type"\s*:\s*"item_completed"', suffix):
                    self._disable("activity_envelope")
                return None
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeError):
            self._disable("activity_json")
            return None
        return obj if isinstance(obj, dict) else None

    def _completion(self, obj: dict[str, Any]) -> float | None:
        payload = obj.get("payload")
        if obj.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "item_completed":
            return None
        if not self.identity_confirmed:
            return None
        if _uuid(payload.get("thread_id")) is None or _uuid(payload.get("turn_id")) is None:
            self._disable("activity_schema")
            return None
        if payload["thread_id"] != self._thread or payload["turn_id"] != self._turn:
            return None
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "Reasoning":
            return None
        identifier = item.get("id")
        started, completed = payload.get("started_at_ms"), payload.get("completed_at_ms")
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 256
                or type(started) is not int or type(completed) is not int):
            self._disable("activity_schema")
            return None
        if identifier in self._seen:
            return None
        age = self._wall() - completed / 1000
        if started / 1000 < self._started or completed < started or not -2 <= age <= _MAX_DELAY:
            return None
        if len(self._seen) >= _MAX_ITEMS:
            self._disable("dedup_limit")
            return None
        self._seen.add(identifier)
        return self._monotonic() - max(0.0, age)

    def _discard_large_line(self, prefix: bytes) -> None:
        envelope = _METADATA.match(prefix)
        if (envelope is not None and envelope["type"] == b"event_msg"
                and _ITEM_COMPLETION.match(prefix[envelope.end():])):
            self._disable("activity_line_limit")

    def _read(self, *, baseline: bool = False) -> list[float]:
        if self._closed or self._disabled:
            return []
        fd = None
        try:
            fd = self._open()
            if fd is None:
                return []
            if baseline:
                self._offset = os.lseek(fd, 0, os.SEEK_END)
                return []
            data = os.read(fd, _MAX_READ)
            self._offset += len(data)
            pieces = (self._partial + data).split(b"\n")
            self._partial = pieces.pop()
            ticks = []
            for line in pieces:
                if self._skipping_line:
                    self._skipping_line = False
                    continue
                if len(line) > _MAX_LINE:
                    self._discard_large_line(line[:512])
                    continue
                obj = self._decode_metadata(line)
                tick = self._completion(obj) if obj else None
                if tick is not None:
                    ticks.append(tick)
            if len(self._partial) > _MAX_LINE:
                self._discard_large_line(self._partial[:512])
                self._partial = b""
                self._skipping_line = True
            return ticks
        except (OSError, ValueError):
            self._disable("rollout_read")
            return []
        finally:
            if fd is not None:
                os.close(fd)

    async def _read_owned(self, *, baseline: bool = False) -> list[float]:
        task = asyncio.create_task(asyncio.to_thread(self._read, baseline=baseline))
        self._read_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._read_task is task:
                self._read_task = None

    async def start(self) -> None:
        # Existing threads start at a captured EOF before the child is spawned.
        if self._thread is not None:
            await self._read_owned(baseline=True)
        if not self._closed and not self._disabled:
            self._task = asyncio.create_task(self._watch(), name="codex-native-activity")

    async def poll_once(self) -> None:
        try:
            ticks = await self._read_owned()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._disable("observer_failed")
            return
        for tick in ticks:
            if self._closed or self._disabled:
                return
            self.matched_records += 1
            self._notify(tick)

    async def _watch(self) -> None:
        while not self._closed and not self._disabled:
            await self.poll_once()
            if self._wall() - self._started > 30 and not self.identity_confirmed:
                self._disable("identity_unconfirmed")
                return
            await asyncio.sleep(0.25)

    def invalidate(self) -> None:
        self._closed = True

    async def close(self) -> None:
        self.invalidate()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._read_task is not None:
            await asyncio.shield(self._read_task)
            self._read_task = None


def diagnostic_environment(env: dict[str, str]) -> None:
    """A per-child override only; never write Codex or shell configuration."""
    env["RUST_LOG"] = _DIAGNOSTIC_ENV
